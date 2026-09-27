"""Context sources: tiered past-ticket retrieval, git ticket map, recent changes, all comments,
config switches, the free retrieval check, and the function-level flag on fixes."""
import json

from conftest import PARSER_V1, TRACE, git, ticket
from test_guards import ONE, analyze_and_approve
from test_pipeline_e2e import FIXED_PARSER, FIXER

from forge import analyze, pipeline, triage
from forge.context_pack import build as build_pack
from forge.evaluate import check_retrieval
from forge.index.store import Index
from forge.store import Store


def commit(env, content, msg):
    (env.repo / "app" / "parser.py").write_text(content, encoding="utf-8")
    git(env.repo, "commit", "-qam", msg)
    git(env.repo, "push", "-q", "origin", "main")


def analysis(env, key, **fields):
    (env.shared / "analyses" / f"{key}.json").write_text(json.dumps({
        "schema": 1, "ticket_key": key, "owner": "sai", "group_id": key, "classification": "code_bug",
        "confidence": 0.9, "analyzed_at": "x", "base_commit": "y", **fields}), encoding="utf-8")


def ready(env):
    pipeline.run_once(force=True)  # fetch, index, ticket map
    return Index(env.cfg.index_db, env.cfg.repo_path), Store(env.cfg.db_path)


def test_past_tickets_rank_code_matches_above_wording(env):
    commit(env, PARSER_V1 + "\n# rounding\n", "SUP-700: round prices in parse_price")
    analysis(env, "SUP-701", summary="Totals off by a cent", affected_symbols=["app/parser.py::parse_price"],
             affected_files=["app/parser.py"], root_cause="parse_price truncated instead of rounding",
             fix_tests=["test_rounding"])
    analysis(env, "SUP-702", summary="Order import crashes for european price format could not convert string",
             root_cause="unrelated wording twin", affected_files=["app/other.py"])
    idx, st = ready(env)
    got = triage.past_matches(env.cfg, st, idx, [ticket("SUP-1", "Order import crashes", TRACE)])
    keys = [m["key"] for m in got]
    assert keys[:2] == ["SUP-701", "SUP-700"], [(m["key"], m["score"], m["why"]) for m in got]
    top = got[0]["line"]
    assert "past fix touched app/parser.py::parse_price" in top and "truncated instead of rounding" in top
    assert "covered by test test_rounding" in top
    assert "fix commit" in got[1]["why"] and "round prices" in got[1]["line"]


def test_past_ticket_limits_and_switches_come_from_config(env):
    analysis(env, "SUP-701", summary="x", affected_symbols=["app/parser.py::parse_price"], root_cause="r")
    idx, st = ready(env)
    t = ticket("SUP-1", "Order import crashes", TRACE)
    env.cfg.team["context"] = {"min_past_score": 0.95}
    assert triage.past_matches(env.cfg, st, idx, [t]) == []  # a function match (0.7–0.9) is below the floor
    env.cfg.team["context"] = {"max_past_tickets": 1, "git_ticket_map": False}
    assert len(triage.past_matches(env.cfg, st, idx, [t])) == 1


def test_recent_changes_of_the_relevant_code_go_into_the_pack(env):
    commit(env, FIXED_PARSER.replace("12.5.", "12.5 ."), "tweak parse_price docstring")
    idx, _ = ready(env)
    recent = analyze.recent_for(env.cfg, idx, TRACE)
    assert "tweak parse_price docstring" in recent[0] and "```diff" in recent[0]  # newest first
    assert "initial" in recent[-1]  # the test repo's first commit is also from today
    pack, stats = build_pack(idx, [analyze.pack_ticket(ticket("SUP-1", "x", TRACE))], {"recent_changes": recent})
    assert "## Recent changes to this code" in pack and stats["files"][0] == "app/parser.py"
    env.cfg.team["context"] = {"recent_changes_days": 0}
    assert analyze.recent_for(env.cfg, idx, TRACE) == []
    env.cfg.team["context"] = {"recent_changes_diff_lines": 0}
    assert "```" not in analyze.recent_for(env.cfg, idx, TRACE)[0]


def test_all_comments_are_included_by_role_only(env):
    t = ticket("SUP-1", "x", "y", comments=[{"author": "Cust", "created": "2026-09-01", "by_reporter": True, "body": "A"},
                                            {"author": "Engineer Name", "created": "2026-09-02", "by_reporter": False,
                                             "body": "repro: import file B"}])
    both = analyze.pack_ticket(t)["description"]
    assert "[reporter comment 2026-09-01] A" in both and "[other comment 2026-09-02] repro" in both
    assert "Engineer Name" not in both
    assert "repro" not in analyze.pack_ticket(t, all_comments=False)["description"]


def test_check_retrieval_is_free_and_reports_hits(env, tmp_path):
    commit(env, PARSER_V1 + "\n# fix\n", "SUP-700: comma decimals in parse_price")
    folder = tmp_path / "closed"
    folder.mkdir()
    (folder / "SUP-900.json").write_text(json.dumps({**ticket("SUP-900", "Order import crashes", TRACE),
                                                     "expected": {"files": ["app/parser.py"], "related": ["SUP-700"]}}))
    (folder / "SUP-901.json").write_text(json.dumps({**ticket("SUP-901", "Totals wrong", "Invoice totals look strange"),
                                                     "expected": {"files": ["app/billing.py"]}}))
    md = check_retrieval(env.cfg, folder, log=lambda *_: None).read_text(encoding="utf-8")
    assert "| SUP-900 | ✓ | 1 | 1/1 | 1/1 |" in md and "| SUP-901 | ✗ | - | 0/1 | - |" in md
    assert "**Fixed file in top 3:** 1/2" in md and "**Related found:** 1/1" in md
    assert env.calls() == []  # no Copilot


def test_fix_touching_an_unplanned_function_is_flagged(env):
    touches_both = FIXED_PARSER.replace('parts = line.split(";")', 'parts = line.strip().split(";")')
    fixer = {**FIXER, "edits": {**FIXER["edits"], "app/parser.py": touches_both}}
    st = analyze_and_approve(env, fixer=fixer)
    assert st.ticket("SUP-1")["status"] == "pr_open"  # same file: allowed, but shown to you
    msg = next(m["text"] for m in env.outbox().values() if "PR ready" in m.get("text", ""))
    assert "not named in the plan: app/parser.py::load_order" in msg
    a = json.loads((env.shared / "analyses" / "SUP-1.json").read_text(encoding="utf-8"))
    assert a["fix_tests"] == ["test_comma_decimal"]  # remembered for future tickets


def test_planned_function_only_is_not_flagged(env):
    analyze_and_approve(env)
    msg = next(m["text"] for m in env.outbox().values() if "PR ready" in m.get("text", ""))
    assert "not named in the plan" not in msg
