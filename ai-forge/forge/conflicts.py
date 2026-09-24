"""Teammate overlap detection and post-merge revalidation (PLAN §5.15)."""
import json
import subprocess
from datetime import datetime
from pathlib import Path

from . import config as C
from . import gitwt
from .cards import notify, update_analysis
from .copilot import BudgetExceeded, extract_json, run_agent
from .index.store import Index
from .shared import TICKET_FILE, atomic_write, read_dir, read_json


def _short(sym: str) -> str:
    return sym.split("::")[-1]


def write_inflight(cfg: C.Config, key: str, **fields) -> dict:
    path = cfg.shared / "inflight" / f"{key}.json"
    old = read_json(path) or {}
    obj = {**old, "schema": 1, "ticket_key": key, "owner": cfg.user_id,
           "updated": datetime.now().isoformat(timespec="seconds"), **fields}
    for k in ("planned_symbols", "changed_files", "changed_symbols"):
        obj.setdefault(k, [])
    atomic_write(path, obj)
    return obj


def others(cfg: C.Config) -> dict[str, dict]:
    return {k: v for k, v in read_dir(cfg.shared / "inflight", TICKET_FILE).items()
            if v.get("owner") != cfg.user_id and v.get("status") != "abandoned"}


def on_base(cfg: C.Config, sha: str | None) -> bool:
    return bool(sha) and subprocess.run(["git", "merge-base", "--is-ancestor", sha, cfg.base_ref],
                                        cwd=cfg.repo_path, capture_output=True).returncode == 0


def detect(cfg: C.Config, idx: Index, key: str, my_symbols: list[str], my_files: list[str]) -> list[dict]:
    """direct (same symbol) | dependency (calls/called-by within depth 2) | same_file, vs others' in-flight work."""
    mine, my_files_s = set(my_symbols), set(my_files) | {s.split("::")[0] for s in my_symbols}
    out = []
    for other, f in others(cfg).items():
        if other == key or (f.get("status") == "merged" and on_base(cfg, f.get("merge_commit"))):
            continue  # already in base: the analysis/fix sees it anyway
        theirs = set(f.get("changed_symbols") or []) | set(f.get("planned_symbols") or [])
        their_files = set(f.get("changed_files") or []) | {s.split("::")[0] for s in theirs}
        base = {"with": other, "owner": f.get("owner"), "status": f.get("status"), "branch": f.get("branch"),
                "pr_url": f.get("pr_url")}
        direct = mine & theirs
        if direct:
            out.append({**base, "kind": "direct", "symbols": sorted(direct),
                        "recommendation": "wait" if f.get("status") == "pr_open" else "build_on",
                        "note": f"Both change {', '.join(sorted(_short(s) for s in direct))}."})
            continue
        dep = (idx.impact([_short(s) for s in theirs]) & mine) | (idx.impact([_short(s) for s in mine]) & theirs)
        if dep:
            out.append({**base, "kind": "dependency", "symbols": sorted(dep), "recommendation": "proceed",
                        "note": f"Call-graph dependency via {', '.join(sorted(_short(s) for s in dep))[:200]}."})
            continue
        same = my_files_s & their_files
        if same:
            out.append({**base, "kind": "same_file", "symbols": sorted(same), "recommendation": "proceed",
                        "note": f"Same file(s): {', '.join(sorted(same))[:200]} (different functions)."})
    return out


def lines(conflicts: list[dict]) -> list[str]:
    return [f"{c['with']} by {c['owner']} [{c['status']}] {c['kind']}: {', '.join(c['symbols'])[:200]}"
            for c in conflicts]


# ---------------- merge watch ----------------
def _gh_pr_state(url: str, cwd: str) -> dict:
    r = subprocess.run(["gh", "pr", "view", url, "--json", "state,mergeCommit"], cwd=cwd, capture_output=True,
                       text=True, errors="replace", timeout=60)
    if r.returncode != 0:
        return {}
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {}


def fix_worktree(cfg: C.Config, key: str) -> Path:
    return cfg.worktree_root / f"fix-{key}"


def watch(cfg: C.Config, store, idx: Index, log=print) -> dict:
    stats = {"merged": 0, "closed": 0, "unblocked": 0, "revalidated": 0, "adapted": 0, "needs_human": 0}
    # 1) my own PRs: merged or closed?
    for t in store.tickets("pr_open"):
        if not t.get("pr_url"):
            continue
        st = _gh_pr_state(t["pr_url"], cfg.repo_path)
        if st.get("state") == "MERGED":
            sha = (st.get("mergeCommit") or {}).get("oid")
            store.set_status(t["key"], "merged", f"PR merged {sha or ''}")
            write_inflight(cfg, t["key"], status="merged", merge_commit=sha)
            update_analysis(cfg, t["key"], status="fixed", fix_commit=sha)
            gitwt.worktree_remove(cfg.repo_path, fix_worktree(cfg, t["key"]))
            stats["merged"] += 1
        elif st.get("state") == "CLOSED":
            store.set_status(t["key"], "rejected", "PR closed without merge")
            write_inflight(cfg, t["key"], status="abandoned")
            gitwt.worktree_remove(cfg.repo_path, fix_worktree(cfg, t["key"]))
            stats["closed"] += 1

    inflight = read_dir(cfg.shared / "inflight", TICKET_FILE)
    # 2) tickets waiting for a teammate's merge
    for t in store.tickets("conflict_wait"):
        other = t.get("blocked_on") or ""
        if other.startswith("?"):
            continue  # conflict card not answered yet
        f = inflight.get(other)
        if not f or f.get("status") in ("merged", "abandoned") or on_base(cfg, f.get("merge_commit")):
            store.set_status(t["key"], "approved", f"{other} merged/abandoned, continuing", blocked_on=f"ack:{other}")
            stats["unblocked"] += 1

    # 3) revalidate my open PRs after overlapping teammate merges
    for t in store.tickets("pr_open"):
        wt = fix_worktree(cfg, t["key"])
        if not wt.exists():
            continue
        mine = read_json(cfg.shared / "inflight" / f"{t['key']}.json") or {}
        done = set(t.get("revalidated") or [])
        for other, f in inflight.items():
            if other in done or f.get("owner") == cfg.user_id or not f.get("merge_commit"):
                continue
            if f.get("status") != "merged" and not on_base(cfg, f.get("merge_commit")):
                continue
            ov = set(f.get("changed_symbols") or []) & set(mine.get("changed_symbols") or []) or \
                set(f.get("changed_files") or []) & set(mine.get("changed_files") or [])
            if not ov:
                continue
            result = revalidate(cfg, store, idx, t, wt, other, f, log)
            stats[result] = stats.get(result, 0) + 1
            store.update(t["key"], revalidated=sorted(done | {other}))
            done.add(other)
    return stats


def revalidate(cfg: C.Config, store, idx: Index, t: dict, wt: Path, other: str, f: dict, log=print) -> str:
    key = t["key"]
    orig_head = gitwt.out(wt, "rev-parse", "HEAD")
    old_base = gitwt.out(wt, "merge-base", "HEAD", cfg.base_ref)
    my_diff = gitwt.git(wt, "diff", f"{old_base}..HEAD").stdout[-15000:]
    full = (cfg.team.get("test") or {}).get("full", "")
    r = gitwt.git(wt, "rebase", cfg.base_ref, check=False)
    if r.returncode == 0:
        rc, output = gitwt.run_cmd(full, wt) if full else (0, "")
        if rc == 0:
            gitwt.git(wt, "push", "--force-with-lease", "origin", "HEAD", check=False)
            notify(cfg, store, key, f"{key} is still valid after {other} was merged ✅ (rebased, tests pass)")
            log(f"revalidate {key} vs {other}: clean")
            return "revalidated"
        failure = {"type": "test_failure", "details": output[-3000:]}
    else:
        conflicted = gitwt.out(wt, "diff", "--name-only", "--diff-filter=U")
        failure = {"type": "rebase_conflict", "details": f"conflicted files:\n{conflicted}"}

    merged_diff = gitwt.git(cfg.repo_path, "show", "--format=%s", f["merge_commit"], check=False).stdout[-15000:]
    adapt = {"my_plan": t.get("plan"), "my_diff": my_diff,
             "merged_change": {"ticket": other, "owner": f.get("owner"), "diff": merged_diff},
             "failure": failure, "targeted_test_cmd": (cfg.team.get("test") or {}).get("targeted")}
    (wt / ".forge").mkdir(exist_ok=True)
    (wt / ".forge" / "adapt.json").write_text(json.dumps(adapt, indent=2), encoding="utf-8")
    test_exe = ((cfg.team.get("test") or {}).get("targeted") or "").split()[:1]
    try:
        res = run_agent(cfg, "forge-adapter", "Adapt this branch as described in .forge/adapt.json.", wt, [key],
                        extra_allow=[f"shell({test_exe[0]})"] if test_exe else None,
                        context_chars=len(json.dumps(adapt)))
        out = extract_json(res.stdout)
    except (BudgetExceeded, ValueError) as e:
        out = {"status": "needs_human", "summary": f"adapter unavailable: {e}"}
    status = out.get("status")
    if status == "adapted":
        if failure["type"] == "rebase_conflict":
            gitwt.git(wt, "add", "-A")
            cont = gitwt.git(wt, "-c", "core.editor=true", "rebase", "--continue", check=False)
            if cont.returncode != 0:
                status = "needs_human"
        else:
            gitwt.git(wt, "add", "-A")
            gitwt.git(wt, "commit", "-m", f"fix({key}): adapt to {other}\n\n{out.get('summary', '')}", check=False)
    if status == "adapted":
        rc, output = gitwt.run_cmd(full, wt) if full else (0, "")
        if rc == 0:
            gitwt.git(wt, "push", "--force-with-lease", "origin", "HEAD", check=False)
            notify(cfg, store, key, f"{key} adapted to {other} and still valid ✅. Reviewer check: {out.get('risk_note', '-')}")
            log(f"revalidate {key} vs {other}: adapted")
            return "adapted"
        out["summary"] = f"tests still fail after adapting: {output[-300:]}"
    gitwt.git(wt, "rebase", "--abort", check=False)  # no-op when no rebase is in progress
    gitwt.git(wt, "reset", "--hard", orig_head, check=False)
    gitwt.git(wt, "clean", "-fd", check=False)  # .forge/ is excluded, so it survives
    notify(cfg, store, key, f"⚠ {key} needs a human after {other} merged ({failure['type']}): "
                           f"{out.get('summary', '')[:300]}. Branch left unchanged.")
    log(f"revalidate {key} vs {other}: needs_human")
    return "needs_human"
