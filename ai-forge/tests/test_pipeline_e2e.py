"""End-to-end: Jira export → rules → grouping → one analyst call → Teams card → approval →
fixer → fails-before/passes-after verification → push → draft PR → merge detection."""
import json

from conftest import TRACE, git, ticket

from forge import pipeline
from forge.store import Store

ANALYSIS = {"schema": 1, "groups": [{
    "tickets": ["SUP-1", "SUP-2"], "classification": "code_bug", "same_root_cause": True,
    "root_cause": "parse_price uses float() on the raw value; a comma decimal separator ('12,50') raises ValueError.",
    "evidence": [{"ref": "app/parser.py:1-4", "why": "float(value) without normalising ','"}],
    "affected_symbols": ["app/parser.py::parse_price"], "affected_files": ["app/parser.py"],
    "proposed_fix": {"summary": "Accept comma decimals", "steps": ["Replace ',' with '.' before float()"],
                     "test_plan": "load_order('A1;12,50 EUR') returns 12.5"},
    "non_code_resolution": None, "hardening_suggestion": None, "regression_of": None, "conflicts": [],
    "risk": "low", "confidence": 0.9, "questions_for_reporter": []}]}
CONFIG_ANALYSIS = {"schema": 1, "groups": [{
    "tickets": ["SUP-5"], "classification": "environment", "same_root_cause": True,
    "root_cause": "The TLS certificate at /etc/ssl/app.pem expired on the staging host.",
    "evidence": [{"ref": "config/app.yaml:1-3", "why": "tls_cert_path"}], "affected_symbols": [],
    "affected_files": ["config/app.yaml"], "proposed_fix": None,
    "non_code_resolution": {"what": "Expired certificate", "where": "staging host /etc/ssl/app.pem",
                            "owner": "ops", "steps": ["Renew the certificate", "Restart the service"]},
    "risk": "low", "confidence": 0.8, "questions_for_reporter": [], "escalate_to_analyst": False}]}
FIXED_PARSER = '''def parse_price(text):
    """Parse '12.50 EUR' (or '12,50 EUR') into 12.5."""
    value, currency = text.split(" ")
    return float(value.replace(",", "."))


def load_order(line):
    parts = line.split(";")
    return {"id": parts[0], "price": parse_price(parts[1])}
'''
NEW_TEST = '''from app.parser import load_order


def test_comma_decimal():
    assert load_order("A1;12,50 EUR")["price"] == 12.5
'''
FIXER = {"edits": {"app/parser.py": FIXED_PARSER, "tests/test_comma_decimal.py": NEW_TEST},
         "json": {"status": "done", "changed_files": ["app/parser.py"], "test_files": ["tests/test_comma_decimal.py"],
                  "test_names": ["test_comma_decimal"], "targeted_test_result": "pass",
                  "summary": "Normalise comma decimal separator in parse_price", "deviations": [],
                  "notes_for_reviewer": "none"}}


def seed(env):
    env.add_ticket(ticket("SUP-1", "Order import crashes", TRACE))
    env.add_ticket(ticket("SUP-2", "Import of order file fails", "Customer B, same problem.\n" + TRACE))
    env.add_ticket(ticket("SUP-3", "Broken", "doesn't work"))
    env.add_ticket(ticket("SUP-4", "Roadmap item", "A long enough description " * 5, type="Epic"))
    env.add_ticket(ticket("SUP-5", "Cannot connect after deploy",
                          "Since the deployment on staging every call fails with certificate expired. "
                          "SSL handshake error, the TLS certificate seems wrong in this environment.", component="ops"))


def decide(env, key, rid, action, responder="lead@x.test", comment=""):
    (env.shared / "decisions" / f"{key}__{rid}.json").write_text(json.dumps({
        "schema": 1, "ticket_key": key, "request_id": rid, "action": action, "responder": responder,
        "comment": comment, "responded_at": "2026-09-24T11:00:00"}))


def test_full_cycle(env):
    seed(env)
    env.script(**{"forge-analyst": [{"json": ANALYSIS}], "forge-config": [{"json": CONFIG_ANALYSIS}],
                  "forge-fixer": [FIXER]})
    res = pipeline.run_once(force=True)
    assert res["errors"] == {}, res

    st = Store(env.cfg.db_path)
    status = {t["key"]: t["status"] for t in st.tickets()}
    assert status == {"SUP-1": "awaiting_decision", "SUP-2": "awaiting_decision", "SUP-3": "needs_info",
                      "SUP-4": "skipped", "SUP-5": "info_sent"}
    # grouping: 2 tickets → 1 analyst call; config ticket → cheap agent; rules → no calls
    agents = [c["agent"] for c in env.calls()]
    assert sorted(agents) == ["forge-analyst", "forge-config"]
    analyst = next(c for c in env.calls() if c["agent"] == "forge-analyst")
    assert {"job.json", "context.md", "REPO_MAP.md"} <= set(analyst["forge_files"])
    assert "--deny-tool" in analyst["argv"] and "shell(git push)" in analyst["argv"]

    # cards: approval for the group (with @mention), info for SUP-5; needs-info comment posted to Jira
    box = env.outbox()
    kinds = sorted(m["kind"] for m in box.values())
    assert kinds == ["approval", "info"]
    approval = next(m for m in box.values() if m["kind"] == "approval")
    text = json.dumps(approval["card"])
    assert "Group of 2" in text and "<at>Sai</at>" in text and "regression_banner" not in text
    assert "SUP-3" in (env.shared / "jira-export" / "comments.log").read_text()
    a1 = json.loads((env.shared / "analyses" / "SUP-1.json").read_text())
    assert a1["group_tickets"] == ["SUP-1", "SUP-2"] and a1["classification"] == "code_bug"
    assert (env.shared / "inflight" / "SUP-1.json").exists()
    # worktrees cleaned up after analysis
    assert not list(env.cfg.worktree_root.glob("analyze-*"))

    # a non-approver click is ignored and the card stays pending
    decide(env, "SUP-1", approval["request_id"], "approve", responder="intruder@x.test")
    for f in (env.shared / "outbox").glob("*.json"):
        f.unlink()  # Power Automate consumed the cards
    pipeline.run_once(force=True)
    assert st.ticket("SUP-1")["status"] == "awaiting_decision"
    assert any("not an approver" in m.get("text", "") for m in env.outbox().values())

    # the lead approves → fixer → verified test → push → draft PR
    decide(env, "SUP-1", approval["request_id"], "approve", comment="keep it minimal")
    res = pipeline.run_once(force=True)
    assert res["errors"] == {}, res
    t1 = st.ticket("SUP-1")
    assert t1["status"] == "pr_open" and t1["pr_url"].endswith("/pull/7")
    assert st.ticket("SUP-2")["status"] == "pr_open"
    plan = json.loads((env.cfg.worktree_root / "fix-SUP-1" / ".forge" / "plan.json").read_text())
    assert plan["reviewer_note"] == "keep it minimal"
    assert git(env.origin, "branch", "--list", "forge/SUP-1")  # pushed
    gh = [json.loads(l) for l in (env.tmp / "gh.calls.jsonl").read_text().splitlines()]
    create = next(c for c in gh if c[:2] == ["pr", "create"])
    title = create[create.index("--title") + 1]
    body = create[create.index("--body") + 1]
    assert "⚠" not in title, title  # test verified: failed before, passes after
    assert "Fails without the fix: yes" in body and "ravi-gh" in create
    inflight = json.loads((env.shared / "inflight" / "SUP-1.json").read_text())
    assert inflight["status"] == "pr_open" and "app/parser.py::parse_price" in inflight["changed_symbols"]
    assert any("Draft PR ready" in m.get("text", "") for m in env.outbox().values())

    # the PR is merged on GitHub → merged, fix commit recorded, worktree removed
    (env.tmp / "pr_state.json").write_text(json.dumps({"state": "MERGED", "mergeCommit": {"oid": "abc1234"}}))
    pipeline.run_once(force=True)
    assert st.ticket("SUP-1")["status"] == "merged"
    assert json.loads((env.shared / "analyses" / "SUP-1.json").read_text())["fix_commit"] == "abc1234"
    assert not (env.cfg.worktree_root / "fix-SUP-1").exists()

    hb = json.loads((env.shared / "runners" / "sai.json").read_text())
    assert hb["counts"]["total"]["analyzed"] == 3 and hb["counts"]["total"]["grouped"] == 1


def test_unverified_test_is_flagged(env):
    env.add_ticket(ticket("SUP-1", "Order import crashes", TRACE))
    bad = json.loads(json.dumps(FIXER))
    bad["edits"]["tests/test_comma_decimal.py"] = "def test_trivial():\n    assert True\n"  # passes before the fix too
    env.script(**{"forge-analyst": [{"json": {**ANALYSIS, "groups": [{**ANALYSIS["groups"][0], "tickets": ["SUP-1"]}]}}],
                  "forge-fixer": [bad]})
    pipeline.run_once(force=True)
    rid = next(m for m in env.outbox().values() if m["kind"] == "approval")["request_id"]
    decide(env, "SUP-1", rid, "approve")
    pipeline.run_once(force=True)
    gh = [json.loads(l) for l in (env.tmp / "gh.calls.jsonl").read_text().splitlines()]
    create = next(c for c in gh if c[:2] == ["pr", "create"])
    assert create[create.index("--title") + 1].startswith("⚠ test not verified")


def test_reject_writes_lesson_and_invalid_output_is_not_retried(env):
    env.add_ticket(ticket("SUP-1", "Order import crashes", TRACE))
    env.script(**{"forge-analyst": [{"json": {**ANALYSIS, "groups": [{**ANALYSIS["groups"][0], "tickets": ["SUP-1"]}]}}]})
    pipeline.run_once(force=True)
    rid = next(m for m in env.outbox().values() if m["kind"] == "approval")["request_id"]
    decide(env, "SUP-1", rid, "reject", responder="sai@x.test", comment="Prices come from the ERP, fix the export")
    pipeline.run_once(force=True)
    st = Store(env.cfg.db_path)
    assert st.ticket("SUP-1")["status"] == "rejected"
    lesson = (env.shared / "lessons" / "orders__sai.md").read_text()
    assert "fix the export" in lesson
    assert json.loads((env.shared / "inflight" / "SUP-1.json").read_text())["status"] == "abandoned"

    env.add_ticket(ticket("SUP-9", "Another crash", TRACE.replace("12,50", "7,10")))
    env.script(**{"forge-analyst": [{"stdout": "I could not produce JSON, sorry"}]})
    pipeline.run_once(force=True)
    assert st.ticket("SUP-9")["status"] == "analyze_failed"
    assert [c["agent"] for c in env.calls()].count("forge-analyst") == 2  # 1 for SUP-1 + 1 for SUP-9, no re-ask


def test_second_run_without_changes_is_quiet(env):
    seed(env)
    env.script(**{"forge-analyst": [{"json": ANALYSIS}], "forge-config": [{"json": CONFIG_ANALYSIS}]})
    pipeline.run_once(force=True)
    st = Store(env.cfg.db_path)
    before = st.con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    n_calls = len(env.calls())
    pipeline.run_once(force=True)
    assert st.con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before
    assert len(env.calls()) == n_calls


def test_reporter_update_reenters_needs_info(env):
    env.add_ticket(ticket("SUP-3", "Broken", "doesn't work"))
    pipeline.run_once(force=True)
    st = Store(env.cfg.db_path)
    assert st.ticket("SUP-3")["status"] == "needs_info"
    # reporter adds the log later → re-enters triage and gets analyzed
    env.add_ticket(ticket("SUP-3", "Broken", "doesn't work", updated="2026-09-24T12:00:00.000+0000",
                          comments=[{"author": "Customer", "created": "2099-01-01T00:00:00", "by_reporter": True,
                                     "body": TRACE}],
                          last_reporter_activity="2099-01-01T00:00:00"))
    env.script(**{"forge-analyst": [{"json": {**ANALYSIS, "groups": [{**ANALYSIS["groups"][0], "tickets": ["SUP-3"]}]}}]})
    pipeline.run_once(force=True)
    assert st.ticket("SUP-3")["status"] == "awaiting_decision"
