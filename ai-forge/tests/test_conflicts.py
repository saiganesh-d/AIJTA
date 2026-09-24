"""PLAN §5.15 acceptance: two branches editing the same function, one merged → conflict card first,
then automatic rebase with a correct adapter result (or a clear needs_human)."""
import json
import subprocess

from conftest import PARSER_V1, TRACE, git, ticket
from test_pipeline_e2e import ANALYSIS, FIXED_PARSER, FIXER, decide

from forge import pipeline
from forge.conflicts import detect
from forge.index.store import Index
from forge.store import Store

TEAMMATE_PARSER = PARSER_V1.replace("return float(value)", "return float(value.strip())")
MERGED_BOTH = FIXED_PARSER.replace('float(value.replace(",", "."))', 'float(value.strip().replace(",", "."))')
SINGLE = {**ANALYSIS, "groups": [{**ANALYSIS["groups"][0], "tickets": ["SUP-1"]}]}


def teammate_merges(env, content, msg="SUP-7: strip whitespace"):
    other = env.tmp / "ravi"
    if not other.exists():
        subprocess.run(["git", "clone", "-q", str(env.origin), str(other)], check=True)
    git(other, "pull", "-q")
    (other / "app" / "parser.py").write_text(content)
    git(other, "commit", "-qam", msg)
    git(other, "push", "-q", "origin", "HEAD:main")
    return git(other, "rev-parse", "HEAD")


def inflight(env, key, **kw):
    obj = {"schema": 1, "ticket_key": key, "owner": "ravi", "status": "pr_open", "branch": f"forge/{key}",
           "planned_symbols": [], "changed_files": ["app/parser.py"],
           "changed_symbols": ["app/parser.py::parse_price"], "updated": "2026-09-24T09:00:00", **kw}
    (env.shared / "inflight" / f"{key}.json").write_text(json.dumps(obj))


def approved_ticket(env):
    env.add_ticket(ticket("SUP-1", "Order import crashes", TRACE))
    pipeline.run_once(force=True)
    rid = next(m for m in env.outbox().values() if m["kind"] == "approval")["request_id"]
    for f in (env.shared / "outbox").glob("*.json"):
        f.unlink()
    decide(env, "SUP-1", rid, "approve")


def test_detect_kinds(env):
    idx = Index(env.cfg.index_db, env.cfg.repo_path)
    pipeline.run_once(force=True)  # builds the index
    inflight(env, "SUP-7")
    inflight(env, "SUP-8", changed_symbols=["app/parser.py::load_order"])
    kinds = {c["with"]: c["kind"] for c in detect(env.cfg, idx, "SUP-1", ["app/parser.py::parse_price"], ["app/parser.py"])}
    assert kinds == {"SUP-7": "direct", "SUP-8": "dependency"}  # load_order calls parse_price
    inflight(env, "SUP-8", changed_symbols=["app/other.py::f"], changed_files=["app/parser.py"])
    kinds = {c["with"]: c["kind"] for c in detect(env.cfg, idx, "SUP-1", ["app/parser.py::parse_price"], ["app/parser.py"])}
    assert kinds["SUP-8"] == "same_file"


def test_conflict_card_then_wait_then_fix_on_merged_base(env):
    env.script(**{"forge-analyst": [{"json": SINGLE}],
                  "forge-fixer": [{**FIXER, "edits": {**FIXER["edits"], "app/parser.py": MERGED_BOTH}}]})
    inflight(env, "SUP-7")  # ravi is changing parse_price right now
    approved_ticket(env)
    pipeline.run_once(force=True)
    st = Store(env.cfg.db_path)
    assert st.ticket("SUP-1")["status"] == "conflict_wait"
    card = next(m for m in env.outbox().values() if m["kind"] == "conflict")
    assert "SUP-7" in json.dumps(card["card"]) and "direct" in json.dumps(card["card"])
    decide(env, "SUP-1", card["request_id"], "wait", responder="sai@x.test")
    pipeline.run_once(force=True)
    assert st.ticket("SUP-1")["status"] == "conflict_wait" and st.ticket("SUP-1")["blocked_on"] == "SUP-7"

    sha = teammate_merges(env, TEAMMATE_PARSER)
    inflight(env, "SUP-7", status="merged", merge_commit=sha)
    pipeline.run_once(force=True)  # unblocked → fix on top of the merged change
    t = st.ticket("SUP-1")
    assert t["status"] == "pr_open", t
    wt = env.cfg.worktree_root / "fix-SUP-1"
    assert git(wt, "merge-base", "--is-ancestor", sha, "HEAD") == ""


def test_revalidation_rebase_conflict_adapted(env):
    env.script(**{"forge-analyst": [{"json": SINGLE}], "forge-fixer": [FIXER],
                  "forge-adapter": [{"edits": {"app/parser.py": MERGED_BOTH},
                                     "json": {"status": "adapted", "changed_files": ["app/parser.py"],
                                              "summary": "kept strip() and comma handling",
                                              "risk_note": "check both behaviours", "targeted_test_result": "pass"}}]})
    approved_ticket(env)
    pipeline.run_once(force=True)
    st = Store(env.cfg.db_path)
    assert st.ticket("SUP-1")["status"] == "pr_open"

    sha = teammate_merges(env, TEAMMATE_PARSER)  # same line as my fix → rebase conflict
    inflight(env, "SUP-7", status="merged", merge_commit=sha)
    res = pipeline.run_once(force=True)
    assert res["results"]["merge_watch"]["adapted"] == 1, res
    wt = env.cfg.worktree_root / "fix-SUP-1"
    assert "value.strip().replace" in (wt / "app" / "parser.py").read_text()
    assert git(wt, "rev-parse", "HEAD") == git(env.origin, "rev-parse", "forge/SUP-1")  # force-pushed
    assert any("adapted to SUP-7" in m.get("text", "") for m in env.outbox().values())
    # handled once: the next run does not revalidate against SUP-7 again
    res = pipeline.run_once(force=True)
    assert res["results"]["merge_watch"]["adapted"] == 0


def test_revalidation_needs_human_leaves_branch_unchanged(env):
    env.script(**{"forge-analyst": [{"json": SINGLE}], "forge-fixer": [FIXER],
                  "forge-adapter": [{"json": {"status": "needs_human", "summary": "behaviours contradict"}}]})
    approved_ticket(env)
    pipeline.run_once(force=True)
    wt = env.cfg.worktree_root / "fix-SUP-1"
    before = git(wt, "rev-parse", "HEAD")
    sha = teammate_merges(env, TEAMMATE_PARSER)
    inflight(env, "SUP-7", status="merged", merge_commit=sha)
    res = pipeline.run_once(force=True)
    assert res["results"]["merge_watch"]["needs_human"] == 1
    assert git(wt, "rev-parse", "HEAD") == before and git(wt, "status", "--porcelain") == ""
    assert any("needs a human" in m.get("text", "") for m in env.outbox().values())


def test_revalidation_clean_rebase(env):
    env.script(**{"forge-analyst": [{"json": SINGLE}], "forge-fixer": [FIXER]})
    approved_ticket(env)
    pipeline.run_once(force=True)
    sha = teammate_merges(env, PARSER_V1.replace('parts = line.split(";")', 'parts = line.strip().split(";")'))
    inflight(env, "SUP-7", status="merged", merge_commit=sha, changed_symbols=["app/parser.py::load_order"])
    res = pipeline.run_once(force=True)
    assert res["results"]["merge_watch"]["revalidated"] == 1, res
    assert any("still valid after SUP-7" in m.get("text", "") for m in env.outbox().values())
