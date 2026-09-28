"""`forge run`: one scheduled cycle.

guards (lock, work hours, team.json) → self-update → agent sync → index refresh → recover →
sync Jira → rules → decisions → stale cards → group + analyze → merge watch → fixes → heartbeat.
Each stage is isolated: one failing stage is logged and the others still run."""
import os
import shutil
import subprocess
import sys
import traceback
from contextlib import contextmanager
from datetime import datetime, timedelta

from . import __version__
from . import config as C
from . import gitwt
from .copilot import tokens_this_month, tokens_today
from .history import sync_ticket_map
from .index.indexer import build_repo_map, index_repo
from .shared import atomic_write


def within_work_hours(cfg: C.Config) -> bool:
    wh = cfg.team.get("work_hours") or {}
    now = datetime.now()
    if wh.get("days") and now.isoweekday() not in wh["days"]:
        return False
    start, end = wh.get("start", "00:00"), wh.get("end", "23:59")
    return start <= now.strftime("%H:%M") <= end


def logger():
    d = C.HOME / "logs"
    d.mkdir(parents=True, exist_ok=True)
    for old in d.glob("forge-*.log"):
        if old.stat().st_mtime < (datetime.now() - timedelta(days=14)).timestamp():
            old.unlink(missing_ok=True)
    path = d / f"forge-{datetime.now():%Y-%m-%d}.log"

    def log(*parts):
        line = f"{datetime.now():%H:%M:%S} " + " ".join(str(p) for p in parts)
        print(line)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    return log


@contextmanager
def single_instance():
    """OS file lock held for the whole run; released automatically if the process dies."""
    C.HOME.mkdir(parents=True, exist_ok=True)
    fh = open(C.HOME / "run.lock", "a+")
    try:
        if sys.platform == "win32":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        yield False
        return
    try:
        yield True
    finally:
        fh.close()


def _ver(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else 0 for x in v.strip().split("."))


def self_update(cfg: C.Config, log) -> bool:
    """PLAN §5.1: <shared>/tool/VERSION newer than ~/.ai-forge/installed_version → pip install it.
    Copies the tool locally first: an in-tree pip build would write build/ into the synced folder."""
    src, inst = cfg.shared / "tool", C.HOME / "installed_version"
    if not (src / "VERSION").exists():
        return False
    new = (src / "VERSION").read_text(encoding="utf-8").strip()
    cur = inst.read_text(encoding="utf-8").strip() if inst.exists() else __version__
    if _ver(new) <= _ver(cur):
        return False
    local = C.HOME / "tool-src"
    shutil.rmtree(local, ignore_errors=True)
    shutil.copytree(src, local, ignore=shutil.ignore_patterns("build", "*.egg-info", "__pycache__", ".git"))
    r = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", str(local)], capture_output=True, text=True)
    if r.returncode != 0:
        log(f"self-update to {new} failed: {r.stderr[-300:]}")
        return False
    inst.write_text(new, encoding="utf-8")
    log(f"self-updated {cur} → {new} (active from the next run)")
    return True


def heartbeat(cfg: C.Config, extra: dict) -> None:
    atomic_write(cfg.shared / "runners" / f"{cfg.user_id}.json", {
        "schema": 1, "user": cfg.user_id, "last_run": datetime.now().isoformat(timespec="seconds"),
        "tool_version": (C.HOME / "installed_version").read_text(encoding="utf-8").strip()
        if (C.HOME / "installed_version").exists() else __version__,
        "mcp_mode": cfg.mcp_mode, "tokens_today_est": tokens_today(), "tokens_month_est": tokens_this_month(),
        **extra})


def run_once(force: bool = False) -> dict:
    with single_instance() as got:
        if not got:
            print("another forge run is still in progress; skipping this cycle")
            return {"skipped": "locked"}
        return _run(force)


def _run(force: bool) -> dict:
    cfg = C.load()
    log = logger()
    if not force and not within_work_hours(cfg):
        return {"skipped": "outside work hours"}
    problems = C.validate_team(cfg.team, cfg.user_id)
    if problems:
        log("team.json has problems, not running:\n  - " + "\n  - ".join(problems))
        return {"skipped": "team.json invalid", "problems": problems}

    from . import analyze, cards, conflicts, fixer, jira, metrics, triage
    from .index.store import Index
    from .setup_wizard import sync_agents
    from .store import Store

    results, errors, done = {}, {}, []

    def stage(name, fn, needs=()):
        """Isolate every step: log, report in the heartbeat, keep going. Skip steps whose inputs failed."""
        if any(n not in done for n in needs):
            log(f"stage {name} skipped: needs {', '.join(n for n in needs if n not in done)}")
            return None
        log(f"stage {name} starting")
        try:
            results[name] = fn()
            done.append(name)
            if name not in ("open_index", "jira_client"):  # objects, not results
                log(f"stage {name} done: {results[name]}")
            return results[name]
        except Exception as e:
            errors[name] = f"{e.__class__.__name__}: {e}"[:300]
            log(f"stage {name} failed:\n{traceback.format_exc()}")
            return None

    store = Store(cfg.db_path)
    idx = client = None
    try:
        stage("self_update", lambda: self_update(cfg, log))
        stage("sync_agents", lambda: sync_agents(cfg))
        stage("recover", lambda: store.recover_interrupted())
        if results.get("recover"):
            log(f"recovered after an interrupted run: {results['recover']}")
        # fetch every branch so analysis and fixes start from the latest remote state; a failed fetch
        # is reported and the run continues on the last fetched origin/main
        stage("fetch", lambda: gitwt.fetch(cfg.repo_path))
        log(f"indexing {cfg.repo_path} @ {cfg.base_ref} ...")
        stats = stage("index", lambda: index_repo(cfg.repo_path, cfg.base_ref, cfg.index_db, fetch=False,
                                                  log=lambda *_: None, ext_map=cfg.index_ext()))
        if stats:
            log(f"index ready: {stats['files_reindexed']}/{stats['files_total']} files re-parsed, "
                f"{stats['symbols_added']} symbols")
        if stats and (stats["files_reindexed"] or not cfg.repo_map.exists()):
            build_repo_map(cfg.index_db, cfg.repo_map)
            log("repo map updated")
        if cfg.ctx("git_ticket_map"):  # ticket → files from commit messages; only new commits are read
            stage("ticket_map", lambda: sync_ticket_map(cfg.repo_path, cfg.base_ref, cfg.index_db), needs=("index",))
        if stats or cfg.index_db.exists():  # a stale index is still better than no run
            idx = stage("open_index", lambda: Index(cfg.index_db, cfg.repo_path))
        client = stage("jira_client", lambda: jira.client(cfg))

        stage("sync_jira", lambda: jira.sync(cfg, store, client, log), needs=("jira_client",))
        stage("preclassify", lambda: triage.preclassify(cfg, store, idx, client, log), needs=("open_index", "jira_client"))
        stage("process_decisions", lambda: cards.process_decisions(cfg, store, log))
        stage("repost_stale", lambda: cards.repost_stale(cfg, store))
        stage("analyze_groups", lambda: analyze.analyze_groups(cfg, store, idx, client, log),
              needs=("open_index", "jira_client"))
        stage("merge_watch", lambda: conflicts.watch(cfg, store, idx, log), needs=("open_index",))  # unblocks waiting fixes
        stage("run_fixes", lambda: fixer.run_fixes(cfg, store, idx, log), needs=("open_index",))
    finally:  # always report, so a failure is visible in runners/<user>.json
        heartbeat(cfg, {"index_commit": (results.get("index") or {}).get("commit"), "stages_ok": done,
                        "errors": errors, "counts": metrics.heartbeat_counts(store),
                        "jira_requests": getattr(client, "requests", 0), "pid": os.getpid()})
    for handle in ("open_index", "jira_client"):  # objects, not results
        results.pop(handle, None)
    log(f"run done: {results} errors={list(errors)}")
    return {"results": results, "errors": errors}
