"""Changes from the pilot test: more file types, all attachments (local files for Copilot on demand),
label repair, 'code in another repo' cards, closed-ticket evaluation, setup conveniences, all comments."""
import json

import pytest
from conftest import TRACE, git, ticket
from test_pipeline_e2e import ANALYSIS

from forge import analyze, cards, jira, pipeline, setup_wizard, triage
from forge.index.indexer import _indexable, index_repo
from forge.index.store import Index
from forge.store import Store

CAPL = """variables { int gCount = 0; }

on message EngineData {
  gCount++;
}

void checkLimit(int value)
{
  if (value > 100) write("over limit");
}
"""


def commit_files(env, files: dict, msg="add files"):
    for rel, content in files.items():
        p = env.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    git(env.repo, "add", "-A")
    git(env.repo, "commit", "-qm", msg)
    git(env.repo, "push", "-q", "origin", "main")


# ---------------- indexing: CANoe formats + configurable extensions ----------------
def test_capl_is_code_and_canoe_settings_are_config():
    assert _indexable("tests/Engine.can") == "c" and _indexable("lib/Utils.CIN") == "c"
    assert _indexable("cfg/System.vsysvar") == "config" and _indexable("xml/map.xslt") == "config"
    assert _indexable("doc/readme.txt") is None
    assert _indexable("doc/readme.txt", {".txt": "config"}) == "config"  # team.json → index wins


def test_capl_functions_are_searchable_and_type_change_reindexes(env):
    commit_files(env, {"canoe/Engine.can": CAPL, "canoe/Panel.xvp": "<panel name='Engine'/>\n"})
    stats = index_repo(str(env.repo), "origin/main", env.cfg.index_db, fetch=False, log=lambda *_: None)
    idx = Index(env.cfg.index_db, str(env.repo))
    try:
        import tree_sitter_language_pack  # noqa: F401 (installed with ai-forge; without it code is indexed as blocks)
        assert any(s["qualname"] == "checkLimit" and s["path"] == "canoe/Engine.can" for s in idx.search("check limit"))
        assert any(s["qualname"] == "EngineData" for s in idx.search("engine data"))  # 'on message' handler
    except ImportError:
        assert any(s["path"] == "canoe/Engine.can" for s in idx.search("check limit", kinds=("block",)))
    assert idx.search("panel engine", kinds=("config",))[0]["path"] == "canoe/Panel.xvp"
    # moving an extension between code and config re-parses those files although their content is unchanged
    again = index_repo(str(env.repo), "origin/main", env.cfg.index_db, fetch=False, log=lambda *_: None,
                       ext_map={".can": "config"})
    assert again["files_reindexed"] == 1 and stats["files_total"] == again["files_total"]


def test_team_json_index_extensions(env):
    env.cfg.team["index"] = {"code_ext": {".sin": "c"}, "config_ext": [".VARSYS"]}
    assert env.cfg.index_ext() == {".sin": "c", ".varsys": "config"}


# ---------------- attachments ----------------
class FakeJira:
    def __init__(self, blobs):
        self.blobs, self.requests = blobs, 0

    def download(self, url, limit):
        self.requests += 1
        return self.blobs[url][:limit]


def test_all_attachment_types_are_kept_locally_with_scrubbed_text(env, tmp_path):
    fj = FakeJira({"u1": b"ERROR token=abc123 failed\n", "u2": b"\x89PNG....", "u3": b"PK\x03\x04zip"})
    t = {"key": "SUP-9", "attachments": [
        {"name": "app.log", "url": "u1", "size": 30, "mime": "text/plain"},
        {"name": "screen.png", "url": "u2", "size": 8},
        {"name": "dump.zip", "url": "u3", "size": 9, "mime": "application/zip"},
        {"name": "huge.mp4", "url": "u4", "size": 50 * 1024 * 1024, "mime": "video/mp4"}]}
    out = {a["name"]: a for a in jira.fetch_attachments(fj, t, tmp_path, limit=10 * 1024 * 1024)}
    assert "abc123" not in out["app.log"]["text"] and "REDACTED" in out["app.log"]["text"]
    assert out["screen.png"]["mime"] == "image/png" and "image attachment" in out["screen.png"]["text"]
    assert all((tmp_path / "SUP-9" / n).read_bytes() for n in ("app.log", "screen.png", "dump.zip"))
    assert "over the size limit" in out["huge.mp4"]["text"] and "local_path" not in out["huge.mp4"]
    assert fj.requests == 3
    again = jira.fetch_attachments(fj, {"key": "SUP-9", "attachments": list(out.values())}, tmp_path)
    assert fj.requests == 3 and again[0]["text"] == out["app.log"]["text"]  # cached: no second download


def test_pdf_text_is_extracted_or_explained():
    pytest.importorskip("pypdf")
    from io import BytesIO

    from pypdf import PdfWriter
    buf = BytesIO()
    w = PdfWriter()
    w.add_blank_page(width=100, height=100)
    w.write(buf)
    assert "no extractable text" in jira.attachment_text(buf.getvalue(), "scan.pdf", "application/pdf")
    assert "could not be parsed" in jira.attachment_text(b"not a pdf", "broken.pdf", "application/pdf")


def test_attachment_size_limit_from_team_json(env):
    assert jira.max_attachment(env.cfg) == 10 * 1024 * 1024
    env.cfg.team["jira"]["max_attachment_mb"] = 25
    assert jira.max_attachment(env.cfg) == 25 * 1024 * 1024


def test_raw_attachments_are_staged_for_copilot_on_demand(env, tmp_path):
    raw = tmp_path / "screen.png"
    raw.write_bytes(b"\x89PNG" + b"0" * 100)
    big = tmp_path / "big.bin"
    big.write_bytes(b"0" * (2 * 1024 * 1024))
    wt = tmp_path / "wt"
    (wt / ".forge").mkdir(parents=True)
    t = {"key": "SUP-1", "attachments": [{"name": "screen.png", "mime": "image/png", "size": 104, "local_path": str(raw)},
                                         {"name": "big.bin", "size": big.stat().st_size, "local_path": str(big)},
                                         {"name": "gone.log", "text": "(download failed: ConnectError)"}]}
    env.cfg.team["context"] = {"attachment_files_max_mb": 1}
    listed = {a["name"]: a for a in analyze.stage_attachments(env.cfg, [t], wt)}
    assert listed["screen.png"]["path"] == ".forge/attachments/SUP-1/screen.png"
    assert (wt / listed["screen.png"]["path"]).read_bytes() == raw.read_bytes()
    assert listed["big.bin"]["path"] is None and listed["gone.log"]["path"] is None
    env.cfg.team["context"] = {"attachment_files": False}
    assert analyze.stage_attachments(env.cfg, [t], wt) == []


# ---------------- labels ----------------
def test_label_variants_are_repaired_not_failed():
    out = analyze.normalize_output({"groups": [{"classification": "Usage"}, {"classification": "config"},
                                               {"classification": "code-bug"}, {"classification": "environment"}]})
    assert [g["classification"] for g in out["groups"]] == ["user_error", "configuration", "code_bug", "environment"]


def test_info_subtitles():
    assert cards.info_subtitle({"classification": "configuration"}) == "configuration, no code change"
    assert "not in this repository" in cards.info_subtitle({"classification": "environment", "code_elsewhere": True})
    assert "not in this repository" in cards.info_subtitle(
        {"classification": "user_error", "root_cause": "The parser lives in a different repository (gateway)."})
    assert cards.info_subtitle({"classification": "user_error", "root_cause": "The value belongs to the config"}) \
        == "usage question or expected behaviour"  # ordinary wording does not trigger the repo label


def test_code_bug_in_another_repo_gets_an_info_card_not_a_fix(env):
    elsewhere = {**ANALYSIS, "groups": [{**ANALYSIS["groups"][0], "tickets": ["SUP-1"], "code_elsewhere": True,
                                         "non_code_resolution": {"what": "Gateway parser bug", "where": "gateway repo",
                                                                 "owner": "gateway team", "steps": ["Raise it there"]}}]}
    env.add_ticket(ticket("SUP-1", "Order import crashes", TRACE))
    env.script(**{"forge-analyst": [{"json": elsewhere}]})
    pipeline.run_once(force=True)
    box = list(env.outbox().values())
    assert [m["kind"] for m in box] == ["info"]
    assert "not in this repository" in json.dumps(box[0]["card"])
    assert Store(env.cfg.db_path).ticket("SUP-1")["status"] == "info_sent"


# ---------------- Jira: closed tickets for evaluation ----------------
def test_closed_ticket_evaluation_switch(env, monkeypatch):
    assert not jira.allow_closed_eval(env.cfg)
    monkeypatch.setenv("AI_FORGE_ALLOW_CLOSED_EVAL", "yes")
    assert jira.allow_closed_eval(env.cfg)
    monkeypatch.delenv("AI_FORGE_ALLOW_CLOSED_EVAL")
    env.cfg.team["jira"]["allow_closed_for_eval"] = True
    assert jira.allow_closed_eval(env.cfg)


# ---------------- setup + comments ----------------
def test_setup_strips_quotes_and_fills_team_json(env):
    assert setup_wizard._clean_input('  "C:\\Users\\me\\repo"  ') == "C:\\Users\\me\\repo"
    team = env.shared / "config" / "team.json"
    data = json.loads(team.read_text(encoding="utf-8"))
    data["jira"]["base_url"] = "https://yourorg.atlassian.net"
    data["members"] = {}
    team.write_text(json.dumps(data), encoding="utf-8")
    setup_wizard.seed_team_folder(env.cfg, "https://acme.atlassian.net/")
    data = json.loads(team.read_text(encoding="utf-8"))
    assert data["jira"]["base_url"] == "https://acme.atlassian.net" and data["jira"]["api_version"] == "3"
    assert data["members"]["sai"] == {"name": "sai", "email": "sai@x.test"}
    setup_wizard.seed_team_folder(env.cfg, "https://other.atlassian.net")  # a real URL is never overwritten
    assert json.loads(team.read_text(encoding="utf-8"))["jira"]["base_url"] == "https://acme.atlassian.net"


def test_all_comments_count_for_search_and_the_needs_info_rule(env):
    pipeline.run_once(force=True)
    idx = Index(env.cfg.index_db, env.cfg.repo_path)
    t = ticket("SUP-1", "Broken", "fails", comments=[
        {"author": "Support", "by_reporter": False, "created": "x",
         "body": "Reproduced: importing orders with price 12,50 EUR fails in load_order every time."}])
    assert "load_order" in triage.full_text(t)
    assert triage.route(env.cfg, idx, t, [])[0] == "ready"  # not bounced back as needs_info
