"""Reusing past investigations and learning from what Copilot needed:
past evidence as current code, lookup logging, gaps, coverage, and the related_tickets MCP tool."""
import importlib
import json

import pytest
from conftest import TRACE, git, ticket
from test_pipeline_e2e import ANALYSIS

from forge import analyze, copilot, metrics, pipeline, triage
from forge.context_pack import build as build_pack
from forge.index.store import Index
from forge.store import Store

TAX = "def apply_vat(amount, rate):\n    return round(amount * (1 + rate), 2)\n"
ONE = {**ANALYSIS, "groups": [{**ANALYSIS["groups"][0], "tickets": ["SUP-1"]}]}


def add_tax_module(env):
    (env.repo / "app" / "tax.py").write_text(TAX, encoding="utf-8")
    git(env.repo, "add", "-A")
    git(env.repo, "commit", "-qm", "add tax")
    git(env.repo, "push", "-q", "origin", "main")


def past(env, **fields):
    (env.shared / "analyses" / "SUP-701.json").write_text(json.dumps({
        "schema": 1, "ticket_key": "SUP-701", "owner": "sai", "group_id": "SUP-701", "classification": "code_bug",
        "confidence": 0.9, "analyzed_at": "x", "base_commit": "y", "root_cause": "rounding in parse_price", **fields}))


def test_strong_match_brings_its_evidence_as_current_code(env):
    add_tax_module(env)
    past(env, affected_symbols=["app/parser.py::parse_price"],
         evidence=[{"ref": "app/tax.py:1-2", "why": "totals rounded here"}, {"ref": "app/ghost.py:1-3", "why": "gone"}])
    pipeline.run_once(force=True)
    idx, st = Index(env.cfg.index_db, env.cfg.repo_path), Store(env.cfg.db_path)
    t = ticket("SUP-1", "Order import crashes", TRACE)
    matches = triage.past_matches(env.cfg, st, idx, [t])
    ev = analyze.past_evidence(env.cfg, matches)
    assert ev and ev[0]["key"] == "SUP-701"
    pack, stats = build_pack(idx, [analyze.pack_ticket(t)], {"past_evidence": ev})
    assert "## Code that proved the cause of related past tickets" in pack
    assert "apply_vat  | " in pack and "(cited by SUP-701)" in pack and "round(amount" in pack
    assert "ghost" not in pack  # pointers to code that no longer exists are dropped
    assert pack.count("(cited by SUP-701)") == 1  # parse_price is already shown as a stack frame: not repeated
    assert stats["past_evidence_blocks"] == 1 and "app/tax.py::apply_vat" in stats["symbols"]

    env.cfg.team["context"] = {"past_evidence": False}
    assert analyze.past_evidence(env.cfg, matches) == []
    env.cfg.team["context"] = {"past_evidence_min_score": 0.95}  # a function match (≤0.9) is not strong enough
    assert analyze.past_evidence(env.cfg, matches) == []


def test_gaps_record_what_copilot_needed_beyond_the_pack(env):
    env.add_ticket(ticket("SUP-1", "Order import crashes", TRACE))
    lookups = [{"tool": "get_symbol", "arg": "apply_vat", "symbols": ["app/tax.py::apply_vat"], "read": True},
               {"tool": "search_code", "arg": "price", "symbols": ["app/other.py::x"], "read": False}]
    env.script(**{"forge-analyst": [{"json": ONE, "lookups": lookups}]})
    pipeline.run_once(force=True)
    st = Store(env.cfg.db_path)
    gaps = {g["symbol"]: g for g in st.gaps()}
    assert "app/tax.py::apply_vat" in gaps, [dict(r) for r in st.con.execute("SELECT event, detail FROM events")]
    assert gaps["app/tax.py::apply_vat"]["sources"] == "lookup"
    assert "app/other.py::x" not in gaps  # only listed in search results, never read: not a need
    assert st.coverage() == (1, 1)  # the cited evidence (parse_price) was already in the pack
    assert not (env.home / "lookups.jsonl").read_text(encoding="utf-8").strip()  # collected and cleared
    assert not (env.home / "current-run.json").exists()
    html = metrics.report(env.cfg, st).read_text(encoding="utf-8")
    assert "Context gaps" in html and "app/tax.py::apply_vat" in html and "Evidence already in context" in html


def test_lookup_log_ignores_calls_outside_a_forge_run(env):
    copilot.log_lookup(env.home, "get_symbol", "x", ["a.py::x"], True)  # e.g. forge doctor
    assert not (env.home / "lookups.jsonl").exists()


def test_learning_switch_off_records_nothing(env):
    env.cfg.team["context"] = {"learn_gaps": False}
    (env.shared / "config" / "team.json").write_text(json.dumps(env.cfg.team))
    env.add_ticket(ticket("SUP-1", "Order import crashes", TRACE))
    env.script(**{"forge-analyst": [{"json": ONE}]})
    pipeline.run_once(force=True)
    assert Store(env.cfg.db_path).coverage() == (0, 0)


def test_related_tickets_tool_matches_on_code(env):
    pytest.importorskip("mcp")  # the MCP SDK is a runtime dependency; skip where it isn't installed
    past(env, affected_symbols=["app/parser.py::parse_price"])
    pipeline.run_once(force=True)
    from forge import mcp_server
    importlib.reload(mcp_server)  # module-level config belongs to this test's environment
    out = mcp_server.related_tickets(TRACE)
    assert out.startswith("SUP-701") and "past fix touched app/parser.py::parse_price" in out
    assert "rounding in parse_price" in out
