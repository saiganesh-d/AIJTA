"""Guards that keep Copilot honest and cheap: citation check, scope guard, broken-test check,
fetch + drift before fixing, already-fixed skip, monthly budget, conservative savings."""
import json
from datetime import date

import pytest
from conftest import PARSER_V1, TRACE, git, ticket
from test_pipeline_e2e import ANALYSIS, FIXER, NEW_TEST, decide

from forge import copilot, metrics, pipeline
from forge.store import Store

ONE = {**ANALYSIS, "groups": [{**ANALYSIS["groups"][0], "tickets": ["SUP-1"]}]}


def analyze_and_approve(env, analysis=ONE, fixer=FIXER, before_fix=None):
    env.add_ticket(ticket("SUP-1", "Order import crashes", TRACE))
    env.script(**{"forge-analyst": [{"json": analysis}], "forge-fixer": [fixer]})
    pipeline.run_once(force=True)
    rid = next(m for m in env.outbox().values() if m["kind"] == "approval")["request_id"]
    if before_fix:
        before_fix()
    decide(env, "SUP-1", rid, "approve")
    res = pipeline.run_once(force=True)
    assert res["errors"] == {}, res
    return Store(env.cfg.db_path)


def events(st, key="SUP-1"):
    return [r["event"] for r in st.con.execute("SELECT event FROM events WHERE key=?", (key,))]


def push_to_main(env, path, content, msg):
    (env.repo / path).write_text(content, encoding="utf-8")
    git(env.repo, "add", "-A")
    git(env.repo, "commit", "-qm", msg)
    git(env.repo, "push", "-q", "origin", "main")


def test_invented_citations_are_flagged_on_the_card(env):
    bad = {**ONE, "groups": [{**ONE["groups"][0], "evidence": [{"ref": "app/parser.py:1-4", "why": "real"},
                                                                {"ref": "app/ghost.py:10-20", "why": "invented"},
                                                                {"ref": "app/parser.py:400-420", "why": "past EOF"}],
                              "affected_symbols": ["app/parser.py::parse_price", "app/parser.py::no_such_fn"]}]}
    env.add_ticket(ticket("SUP-1", "Order import crashes", TRACE))
    env.script(**{"forge-analyst": [{"json": bad}]})
    pipeline.run_once(force=True)
    card = json.dumps(next(m for m in env.outbox().values() if m["kind"] == "approval")["card"])
    assert "Not found in the code" in card and "app/ghost.py:10-20" in card and "app/parser.py:400-420" in card
    assert "no_such_fn" in card and "app/parser.py:1-4" not in card.split("Not found in the code")[1]
    assert "quality:unverified_citations" in events(Store(env.cfg.db_path))


def test_fixer_touching_files_outside_the_plan_is_rejected(env):
    sneaky = {**FIXER, "edits": {**FIXER["edits"], "app/__init__.py": "print('drive-by change')\n"}}
    st = analyze_and_approve(env, fixer=sneaky)
    assert st.ticket("SUP-1")["status"] == "fix_failed"
    assert "quality:scope_violation" in events(st)
    assert any("outside the approved plan" in m.get("text", "") for m in env.outbox().values())
    assert git(env.repo, "branch", "--list", "forge/SUP-1") == "" or \
        git(env.repo, "rev-list", "--count", "origin/main..forge/SUP-1") == "0"  # nothing committed


def test_declared_deviation_is_delivered_but_flagged(env):
    ok = {"edits": {**FIXER["edits"], "app/__init__.py": "# needed for import\n"},
          "json": {**FIXER["json"], "deviations": ["app/__init__.py: package marker needed by the test"]}}
    st = analyze_and_approve(env, fixer=ok)
    assert st.ticket("SUP-1")["status"] == "pr_open"
    gh = [json.loads(l) for l in (env.tmp / "gh.calls.jsonl").read_text(encoding="utf-8").splitlines()]
    create = next(c for c in gh if c[:2] == ["pr", "create"])
    assert "deviates from plan" in create[create.index("--title") + 1]


def test_fix_that_breaks_existing_tests_is_not_committed(env):
    # new test passes, but "12.50" becomes 1250.0 and the existing test_load_order fails
    breaking = PARSER_V1.replace("float(value)", "float(value.replace('.', '').replace(',', '.'))")
    fixer = {"edits": {"app/parser.py": breaking, "tests/test_comma_decimal.py": NEW_TEST}, "json": FIXER["json"]}
    st = analyze_and_approve(env, fixer=fixer)
    assert st.ticket("SUP-1")["status"] == "fix_failed"
    assert "quality:broke_existing_tests" in events(st)
    assert any("breaks tests that pass" in m.get("text", "") for m in env.outbox().values())


def test_ticket_fixed_upstream_meanwhile_skips_the_fixer(env):
    st = analyze_and_approve(env, before_fix=lambda: push_to_main(
        env, "app/parser.py", PARSER_V1.replace("float(value)", "float(value.replace(',', '.'))"),
        "SUP-1: accept comma decimals (fixed by hand)"))
    assert st.ticket("SUP-1")["status"] == "resolved"
    assert [c["agent"] for c in env.calls()] == ["forge-analyst"]  # no fixer tokens spent
    assert any("already fixed" in m.get("text", "") for m in env.outbox().values())


def test_fixer_gets_latest_code_drift_and_existing_tests(env):
    st = analyze_and_approve(env, before_fix=lambda: push_to_main(
        env, "app/parser.py", "# header added later\n" + PARSER_V1, "refactor: header"))
    assert st.ticket("SUP-1")["status"] == "pr_open"
    plan = json.loads((env.cfg.worktree_root / "fix-SUP-1" / ".forge" / "plan.json").read_text(encoding="utf-8"))
    assert plan["code_changed_since_analysis"]["files"] == ["app/parser.py"]
    assert any("refactor: header" in c for c in plan["code_changed_since_analysis"]["commits"])
    assert plan["existing_tests"] == ["tests/test_parser.py"] and plan["allowed_files"] == ["app/parser.py"]
    # the fix branch starts from the freshly fetched main, not from the analyzed commit
    assert git(env.repo, "merge-base", "--is-ancestor", "origin/main", "forge/SUP-1") == ""


def test_monthly_budget_stops_calls(env, tmp_path):
    env.cfg.team["budget"] = {"daily_tokens": 0, "monthly_tokens": 100}
    con = copilot._ledger()
    con.execute("INSERT INTO calls(day, agent, est_input_tokens) VALUES (?,?,?)",
                (date.today().replace(day=1).isoformat(), "x", 150))
    con.commit()
    with pytest.raises(copilot.BudgetExceeded, match="monthly"):
        copilot.run_agent(env.cfg, "forge-analyst", "x", tmp_path, [])


def test_savings_count_only_accepted_work():
    a = {"minutes_triage_per_ticket": 10, "minutes_root_cause_per_code_bug": 100,
         "minutes_resolution_per_non_code": 30, "minutes_fix_per_pr": 60}
    c = {k: 0 for k in metrics._zero()}
    c.update(analyzed=2, approval_cards=2, approved=1, rejected=1, skipped_by_rules=50, prs=2, prs_test_verified=1)
    s = metrics.savings(c, a)
    assert s["hours_saved"] == round((2 * 10 + 1 * 100 + 1 * 60) / 60, 1)  # skipped, rejected, unverified earn 0
    assert s["copilot_calls_avoided"] == 0
