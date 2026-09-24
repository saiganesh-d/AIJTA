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
from .copilot import tokens_today
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
        "tool_version": (C.HOME / "installed_version").read_text().strip()
        if (C.HOME / "installed_version").exists() else __version__,
        "mcp_mode": cfg.mcp_mode, "tokens_today_est": tokens_today(), **extra})


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

    self_update(cfg, log)
    sync_agents(cfg)
    stats = index_repo(cfg.repo_path, cfg.base_ref, cfg.index_db, log=lambda *_: None)
    if stats["files_reindexed"] or not cfg.repo_map.exists():
        build_repo_map(cfg.index_db, cfg.repo_map)
    idx = Index(cfg.index_db, cfg.repo_path)
    store = Store(cfg.db_path)
    recovered = store.recover_interrupted()
    if recovered:
        log(f"recovered after an interrupted run: {recovered}")
    client = jira.client(cfg)

    stages = [
        ("sync_jira", lambda: jira.sync(cfg, store, client, log)),
        ("preclassify", lambda: triage.preclassify(cfg, store, idx, client, log)),
        ("process_decisions", lambda: cards.process_decisions(cfg, store, log)),
        ("repost_stale", lambda: cards.repost_stale(cfg, store)),
        ("analyze_groups", lambda: analyze.analyze_groups(cfg, store, idx, client, log)),
        ("merge_watch", lambda: conflicts.watch(cfg, store, idx, log)),  # before fixes: unblocks waiting tickets
        ("run_fixes", lambda: fixer.run_fixes(cfg, store, idx, log)),
    ]
    results, errors = {}, {}
    for name, fn in stages:
        try:
            results[name] = fn()
        except Exception as e:  # isolate stages: log, report in the heartbeat, keep going
            errors[name] = f"{e.__class__.__name__}: {e}"[:300]
            log(f"stage {name} failed:\n{traceback.format_exc()}")
    heartbeat(cfg, {"index_commit": stats["commit"], "stages_ok": [n for n, _ in stages if n not in errors],
                    "errors": errors, "counts": metrics.heartbeat_counts(store), "jira_requests": client.requests,
                    "pid": os.getpid()})
    log(f"run done: {results} errors={list(errors)}")
    return {"results": results, "errors": errors}
