import json
import subprocess
from datetime import date

import httpx
import pytest
from conftest import PARSER_V1, TRACE, git, ticket
from test_pipeline_e2e import ANALYSIS

from forge import cards, copilot, metrics, pipeline, triage
from forge import config as C
from forge.index.store import Index
from forge.jira import Jira, adf_to_text, history_jql, sync
from forge.store import Store


# ---------------- config ----------------
def test_validate_team_flags_placeholders():
    team = json.loads((C.ASSETS / "team.example.json").read_text())
    team["models"]["forge-analyst"] = "REPLACE_WITH_MID_MODEL_ID"
    probs = C.validate_team(team, "nobody")
    assert any("forge-analyst is a placeholder" in p for p in probs)
    assert any("jira.base_url" in p for p in probs)
    assert any("'nobody'" in p for p in probs)


def test_run_refuses_invalid_team(env):
    team = json.loads((env.shared / "config" / "team.json").read_text())
    team["models"]["forge-fixer"] = "REPLACE_WITH_MID_MODEL_ID"
    (env.shared / "config" / "team.json").write_text(json.dumps(team))
    res = pipeline.run_once(force=True)
    assert res["skipped"] == "team.json invalid" and any("forge-fixer" in p for p in res["problems"])


# ---------------- jira ----------------
def _cfg(env, **jira):
    env.cfg.team["jira"] = {"base_url": "https://jira.x.test", "scope_jql": "project = SUP AND statusCategory != Done",
                            "sprint_field": "customfield_1", **jira}
    return env.cfg


def _issue(key, updated="2026-09-24T10:00:00.000+0000", desc="plain text", **f):
    return {"key": key, "fields": {"summary": f"s {key}", "description": desc, "issuetype": {"name": "Bug"},
                                   "updated": updated, "status": {"name": "Open", "statusCategory": {"key": "new"}},
                                   "reporter": {"name": "cust"}, "components": [{"name": "orders"}],
                                   "customfield_1": [{"name": "Sprint 41"}, {"name": "Sprint 42"}], **f}}


def test_history_jql_strips_status():
    assert history_jql("project = SUP AND statusCategory != Done") == "project = SUP"
    assert history_jql('status in ("Open", "In Progress") AND project = SUP') == "project = SUP"
    assert history_jql("project = SUP") == "project = SUP"


def test_jira_server_pagination_and_normalize(env):
    pages = {0: [_issue("SUP-1"), _issue("SUP-2")], 2: [_issue("SUP-3")]}
    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        assert req.headers["Authorization"] == "Bearer tok"
        start = int(req.url.params["startAt"])
        return httpx.Response(200, json={"issues": pages.get(start, []), "total": 3})

    j = Jira(_cfg(env), "tok", transport=httpx.MockTransport(handler))
    issues = [j.normalize(i) for i in j.search("project = SUP")]
    assert [i["key"] for i in issues] == ["SUP-1", "SUP-2", "SUP-3"] and len(seen) == 2
    assert issues[0]["sprint"] == "Sprint 42" and issues[0]["component"] == "orders"


def test_jira_cloud_next_page_token_and_adf(env):
    adf = {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Crash on import"}]},
                                      {"type": "codeBlock", "content": [{"type": "text", "text": "ValueError: x"}]}]}
    calls = []

    def handler(req):
        calls.append(req)
        assert req.url.path == "/rest/api/3/search/jql" and req.headers["Authorization"].startswith("Basic ")
        if "nextPageToken" not in req.url.params:
            return httpx.Response(200, json={"issues": [_issue("SUP-1", desc=adf)], "nextPageToken": "p2"})
        return httpx.Response(200, json={"issues": [_issue("SUP-2")], "isLast": True})

    j = Jira(_cfg(env, api_version="3"), "tok", transport=httpx.MockTransport(handler))
    issues = [j.normalize(i) for i in j.search("x")]
    assert [i["key"] for i in issues] == ["SUP-1", "SUP-2"]
    assert "Crash on import" in issues[0]["description"] and "```\nValueError: x" in issues[0]["description"]
    assert adf_to_text(None) == ""


def test_sync_unchanged_second_run_is_cheap(env):
    cfg = _cfg(env, support_issue_types=["Bug"])
    data = {"issues": [_issue("SUP-1", desc=TRACE)], "total": 1}
    reqs = []

    def handler(req):
        reqs.append(str(req.url))
        return httpx.Response(200, json=data)

    j = Jira(cfg, "tok", transport=httpx.MockTransport(handler))
    st = Store(env.home / "t.db")
    first = sync(cfg, st, j, log=lambda *_: None)
    assert first["new"] == 1 and st.ticket("SUP-1")["signature"]
    events = st.con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    reqs.clear()
    second = sync(cfg, st, j, log=lambda *_: None)
    assert second["new"] == 0 and len(reqs) == 1  # mine only; history is hourly
    assert 'updated >= "' in reqs[0].replace("%22", '"').replace("+", " ").replace("%3E%3D", ">=")
    assert st.con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == events


# ---------------- triage ----------------
def test_git_facts_detect_revert_as_regression(env):
    repo = env.repo
    (repo / "app" / "parser.py").write_text(PARSER_V1.replace("float(value)", "float(value.replace(',', '.'))"))
    git(repo, "commit", "-qam", "SUP-880: accept comma decimals")
    fix = git(repo, "rev-parse", "HEAD")
    git(repo, "push", "-q", "origin", "main")
    facts = triage.git_facts(str(repo), "origin/main", "SUP-880", {})
    assert facts["fix_commit"] == fix[:10] and facts["present"] and not facts["reverted_by"]
    assert not triage.is_regression(facts)
    assert triage.git_facts(str(repo), "origin/main", "SUP-88", {})["fix_commit"] is None  # no prefix match
    git(repo, "revert", "--no-edit", "HEAD")
    git(repo, "push", "-q", "origin", "main")
    facts = triage.git_facts(str(repo), "origin/main", "SUP-880", {})
    assert facts["reverted_by"] and triage.is_regression(facts)
    assert "REVERTED" in triage.fact_line("SUP-880", {"sprint": "Sprint 42"}, facts)


def test_known_signature_becomes_duplicate_without_copilot(env):
    repo = env.repo
    (repo / "app" / "parser.py").write_text(PARSER_V1.replace("float(value)", "float(value.replace(',', '.'))"))
    git(repo, "commit", "-qam", "SUP-880: accept comma decimals")
    git(repo, "push", "-q", "origin", "main")
    from forge.signals import error_signature
    (env.shared / "analyses" / "SUP-880.json").write_text(json.dumps({
        "schema": 1, "ticket_key": "SUP-880", "owner": "ravi", "group_id": "SUP-880", "classification": "code_bug",
        "root_cause": "comma decimals", "confidence": 0.9, "analyzed_at": "x", "base_commit": "y",
        "error_signature": error_signature(TRACE), "affected_files": ["app/parser.py"]}))
    env.add_ticket(ticket("SUP-1", "Order import crashes", TRACE))
    pipeline.run_once(force=True)
    st = Store(env.cfg.db_path)
    assert st.ticket("SUP-1")["status"] == "duplicate" and env.calls() == []
    card = next(iter(env.outbox().values()))
    assert card["kind"] == "info" and "duplicate of SUP-880" in json.dumps(card["card"])


def test_grouping_respects_cap_and_signature(env):
    pipeline.run_once(force=True)  # index
    st = Store(env.cfg.db_path)
    idx = Index(env.cfg.index_db, env.cfg.repo_path)
    env.cfg.team["thresholds"]["max_group_size"] = 2
    for i in range(1, 4):
        st.upsert_ticket({**ticket(f"SUP-{i}", "crash", TRACE), "signature": "same"})
        st.set_status(f"SUP-{i}", "ready", route="forge-analyst")
    groups = triage.group_ready(env.cfg, st, idx)
    assert sorted(len(g["tickets"]) for g in groups) == [1, 2]
    assert any("same error signature" in r for g in groups for r in g["reasons"])


def test_route_rules(env):
    pipeline.run_once(force=True)
    idx = Index(env.cfg.index_db, env.cfg.repo_path)
    r = lambda **kw: triage.route(env.cfg, idx, {**ticket("SUP-1", "x", ""), **kw}, [])[:2]
    assert r(type="Epic", description="x" * 100) == ("skipped", "")
    assert r(description="broken") == ("needs_info", "")
    assert r(description="How do I change the timeout_seconds in config? The request times out after deploy " * 2) \
        == ("ready", "forge-config")
    assert r(description=TRACE) == ("ready", "forge-analyst")


def test_lessons_are_per_writer_and_recent_first(env):
    triage.add_lesson(env.cfg, "Orders", "SUP-1", "first")
    (env.shared / "lessons" / "orders.md").write_text("- 2020-01-01 SUP-0 (lead): old lead note\n")
    triage.add_lesson(env.cfg, "Orders", "SUP-2", "second")
    got = triage.lessons(env.cfg, "Orders", n=2)
    assert len(got) == 2 and "old lead note" not in " ".join(got)
    assert (env.shared / "lessons" / "orders__sai.md").exists()


# ---------------- cards ----------------
def _pending_approval(env):
    st = Store(env.cfg.db_path)
    st.upsert_ticket(ticket("SUP-1", "crash", TRACE))
    g = {**ANALYSIS["groups"][0], "tickets": ["SUP-1"]}
    rid = cards.send_approval(env.cfg, st, g, 1234, {"key": "SUP-880", "note": "reverted"}, None)
    return st, rid


def test_card_banners_and_escaping(env):
    st, rid = _pending_approval(env)
    card = env.outbox()[f"SUP-1__{rid}.json"]["card"]
    ids = [b.get("id") for b in card["body"]]
    assert "regression_banner" in ids and "conflict_banner" not in ids
    assert card["actions"][0]["data"]["request_id"] == rid and "1,234 tokens" in json.dumps(card)


def test_decision_with_wrong_action_or_stale_is_ignored(env):
    st, rid = _pending_approval(env)
    d = {"schema": 1, "ticket_key": "SUP-1", "request_id": rid, "action": "build_on", "responder": "lead@x.test"}
    (env.shared / "decisions" / f"SUP-1__{rid}.json").write_text(json.dumps(d))
    assert cards.process_decisions(env.cfg, st, log=lambda *_: None) == []
    assert st.pending(rid)  # still pending
    ravi = {**d, "action": "approve", "responder": "ravi@x.test"}  # approver, but neither assignee nor lead
    (env.shared / "decisions" / f"SUP-1__{rid}.json").write_text(json.dumps(ravi))
    assert cards.process_decisions(env.cfg, st, log=lambda *_: None) == []


def test_stale_cards_are_reposted_and_old_clicks_ignored(env):
    st, rid = _pending_approval(env)
    st.con.execute("UPDATE pending_requests SET created='2026-09-01T09:00:00'")
    assert cards.repost_stale(env.cfg, st, today=date(2026, 9, 24)) == ["SUP-1"]
    new = [p["request_id"] for p in st.pending()]
    assert rid not in new and len(new) == 1
    assert new[0] in json.dumps(env.outbox()[f"SUP-1__{new[0]}.json"]["card"])
    d = {"schema": 1, "ticket_key": "SUP-1", "request_id": rid, "action": "approve", "responder": "lead@x.test"}
    (env.shared / "decisions" / f"SUP-1__{rid}.json").write_text(json.dumps(d))
    assert cards.process_decisions(env.cfg, st, log=lambda *_: None) == []
    assert st.ticket("SUP-1")["status"] != "approved"


def test_working_days():
    assert cards.working_days_between(date(2026, 9, 18), date(2026, 9, 25)) == 5  # Fri → Fri


# ---------------- copilot ----------------
def test_global_mode_uses_prefix_file_and_denies(env, tmp_path):
    env.cfg.mcp_mode = "global"
    env.script(**{"global": [{"json": {"ok": 1}}]})
    wt = tmp_path / "wt"
    wt.mkdir()
    res = copilot.run_agent(env.cfg, "forge-analyst", "Analyze.", wt, ["SUP-1"])
    call = env.calls()[-1]
    assert "--agent" not in call["argv"] and "AGENT.md" in call["argv"][call["argv"].index("-p") + 1]
    assert "root-cause analyst" in (wt / ".forge" / "AGENT.md").read_text()
    assert "--additional-mcp-config" in call["argv"] and res.usage["input"] == 4200


def test_budget_stops_calls(env, tmp_path):
    env.cfg.team["members"]["sai"]["daily_token_budget"] = 1
    con = copilot._ledger()
    con.execute("INSERT INTO calls(day, agent, est_input_tokens) VALUES (?,?,?)", (date.today().isoformat(), "x", 10))
    con.commit()
    with pytest.raises(copilot.BudgetExceeded):
        copilot.run_agent(env.cfg, "forge-analyst", "x", tmp_path, [])


def test_extract_json_repairs_trailing_commas():
    assert copilot.extract_json('noise ```json\n{"a": [1, 2,],}\n``` tail') == {"a": [1, 2]}


# ---------------- store / pipeline guards ----------------
def test_recover_interrupted(env):
    st = Store(env.cfg.db_path)
    st.upsert_ticket(ticket("SUP-1", "x", "y"))
    st.set_status("SUP-1", "analyzing")
    assert st.recover_interrupted() == ["SUP-1"] and st.ticket("SUP-1")["status"] == "ready"


def test_single_instance_lock(env):
    with pipeline.single_instance() as a:
        assert a
        assert pipeline.run_once(force=True) == {"skipped": "locked"}


def test_self_update_installs_newer_version(env, monkeypatch):
    (env.shared / "tool").mkdir()
    (env.shared / "tool" / "VERSION").write_text("0.2.0")
    (env.home / "installed_version").write_text("0.1.0")
    ran = []
    monkeypatch.setattr(pipeline.subprocess, "run", lambda cmd, **kw: ran.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    assert pipeline.self_update(env.cfg, log=lambda *_: None)
    assert "tool-src" in ran[0][-1] and (env.home / "installed_version").read_text() == "0.2.0"
    assert not pipeline.self_update(env.cfg, log=lambda *_: None)


# ---------------- metrics ----------------
def test_report_and_baseline(env):
    env.add_ticket(ticket("SUP-1", "Order import crashes", TRACE))
    env.add_ticket(ticket("SUP-3", "Broken", "doesn't work"))
    env.script(**{"forge-analyst": [{"json": {**ANALYSIS, "groups": [{**ANALYSIS["groups"][0], "tickets": ["SUP-1"]}]}}]})
    pipeline.run_once(force=True)
    out = metrics.report(env.cfg, Store(env.cfg.db_path))
    html = out.read_text()
    assert "Engineer hours saved" in html and "prefers-color-scheme" in html
    team = metrics.report(env.cfg, Store(env.cfg.db_path), team=True)
    assert "Team" in team.read_text()

    bdir = env.tmp / "baseline"
    bdir.mkdir()
    (bdir / "SUP-1.json").write_text(json.dumps({**ticket("SUP-1", "Order import crashes", TRACE),
                                                 "expected": {"classification": "code_bug", "files": ["app/parser.py"]}}))
    env.script(**{"baseline": [{"edits": {"app/parser.py": "x = 1\n"}, "stdout": "done", "input": "40k", "output": 3000}],
                  "forge-analyst": [{"json": {**ANALYSIS, "groups": [{**ANALYSIS["groups"][0], "tickets": ["SUP-1"]}]}}],
                  "forge-fixer": [{"json": {"status": "done"}}]})
    md = metrics.baseline(env.cfg, bdir).read_text()
    assert "| SUP-1 | plain copilot | 1 | 43,000 |" in md and "| SUP-1 | forge | 2 |" in md
    assert "code_bug | ✓ | ✓" in md


def test_unexpected_analysis_error_fails_once(env, monkeypatch):
    from forge import analyze
    env.add_ticket(ticket("SUP-1", "Order import crashes", TRACE))
    env.script(**{"forge-analyst": [{"json": ANALYSIS}]})
    monkeypatch.setattr(analyze, "build_pack", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    pipeline.run_once(force=True)
    pipeline.run_once(force=True)
    st = Store(env.cfg.db_path)
    assert st.ticket("SUP-1")["status"] == "analyze_failed" and env.calls() == []
    assert any("boom" in m.get("text", "") for m in env.outbox().values())


def test_auto_model_omits_model_flag(env, tmp_path):
    env.cfg.team["models"]["forge-analyst"] = "auto"
    assert C.validate_team(env.cfg.team, "sai") == []
    env.script(**{"forge-analyst": [{"json": {"ok": 1}}]})
    copilot.run_agent(env.cfg, "forge-analyst", "x", tmp_path, [])
    assert "--model" not in env.calls()[-1]["argv"]


def test_read_only_jira_sends_questions_to_teams(env):
    env.cfg.team["jira"]["post_comments"] = False
    (env.shared / "config" / "team.json").write_text(json.dumps(env.cfg.team))
    env.add_ticket(ticket("SUP-3", "Broken", "doesn't work"))
    pipeline.run_once(force=True)
    assert not (env.shared / "jira-export" / "comments.log").exists()  # nothing written to Jira
    msg = next(m for m in env.outbox().values() if m["kind"] == "notify")
    assert "SUP-3 needs more information" in msg["text"] and "steps to reproduce" in msg["text"]


def test_assigned_to_me_false_takes_all_scope_tickets(env):
    cfg = _cfg(env, assigned_to_me=False, support_issue_types=[])
    seen = []

    def handler(req):
        seen.append(req.url.params["jql"])
        return httpx.Response(200, json={"issues": [_issue("ABC-1", desc=TRACE)], "total": 1})

    st = Store(env.home / "t.db")
    sync(cfg, st, Jira(cfg, "tok", transport=httpx.MockTransport(handler)), log=lambda *_: None)
    assert "currentUser" not in seen[0] and st.ticket("ABC-1")
    idx = Index(env.cfg.index_db, env.cfg.repo_path)
    assert triage.route(cfg, idx, {**ticket("ABC-1", "x", TRACE), "type": "Story"}, [])[0] == "ready"  # no type filter
