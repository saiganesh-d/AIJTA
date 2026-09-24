"""Test harness: an isolated AI_FORGE_HOME, a real git repo with a bare 'origin', a shared folder,
file-mode Jira, and fake copilot/gh executables on PATH. No network, no real Copilot calls."""
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from forge import config as C
from forge import copilot, gitwt, setup_wizard, shared

HERE = Path(__file__).parent

PARSER_V1 = '''def parse_price(text):
    """Parse '12.50 EUR' into 12.5."""
    value, currency = text.split(" ")
    return float(value)


def load_order(line):
    parts = line.split(";")
    return {"id": parts[0], "price": parse_price(parts[1])}
'''
TEST_V1 = '''from app.parser import load_order


def test_load_order():
    assert load_order("A1;12.50 EUR") == {"id": "A1", "price": 12.5}
'''
APP_YAML = "server:\n  tls_cert_path: /etc/ssl/app.pem\n  timeout_seconds: 5\n"


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def make_repo(tmp: Path) -> tuple[Path, Path]:
    origin = tmp / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
    repo = tmp / "repo"
    subprocess.run(["git", "clone", str(origin), str(repo)], check=True, capture_output=True)
    git(repo, "checkout", "-b", "main")
    files = {"app/__init__.py": "", "app/parser.py": PARSER_V1, "tests/__init__.py": "",
             "tests/test_parser.py": TEST_V1, "config/app.yaml": APP_YAML}
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "initial")
    git(repo, "push", "-u", "origin", "main")
    return repo, origin


def ticket(key, summary, description, **kw) -> dict:
    return {"key": key, "summary": summary, "description": description, "type": kw.pop("type", "Bug"),
            "assignee": kw.pop("assignee", "sai@x.test"), "updated": kw.pop("updated", "2026-09-24T10:00:00.000+0000"),
            "component": kw.pop("component", "orders"), "priority": kw.pop("priority", "Medium"),
            "reporter": "customer@x.test", "labels": [], "attachments": [], "comments": [], **kw}


TRACE = '''Order import fails for European price format.
Traceback (most recent call last):
  File "/srv/app/app/parser.py", line 9, in load_order
    return {"id": parts[0], "price": parse_price(parts[1])}
  File "/srv/app/app/parser.py", line 4, in parse_price
    return float(value)
ValueError: could not convert string to float: '12,50'
'''


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    for mod in (C, shared, copilot):
        monkeypatch.setattr(mod, "HOME", home)
    monkeypatch.setattr(C, "LOCAL_CONFIG", home / "config.json")
    monkeypatch.setattr(copilot, "AGENTS_DIR", tmp_path / "no-agents")
    monkeypatch.setattr(setup_wizard, "COPILOT_HOME", tmp_path / ".copilot")
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x.test", "GIT_COMMITTER_NAME": "t",
                 "GIT_COMMITTER_EMAIL": "t@x.test"}.items():
        monkeypatch.setenv(k, v)

    repo, origin = make_repo(tmp_path)
    sh = tmp_path / "shared"
    shared.ensure_layout(sh)
    (sh / "jira-export").mkdir()
    monkeypatch.setattr(copilot, "COPILOT_BIN", str(HERE / "fake_copilot.py"))
    monkeypatch.setattr(gitwt, "GH_BIN", str(HERE / "fake_gh.py"))
    scenario = tmp_path / "scenario.json"
    scenario.write_text("{}")
    monkeypatch.setenv("FAKE_COPILOT_SCENARIO", str(scenario))
    monkeypatch.setenv("FAKE_GH_DIR", str(tmp_path))

    team = json.loads((C.ASSETS / "team.example.json").read_text())
    team["jira"] = {"mode": "file", "base_url": "https://jira.x.test", "scope_jql": "project = SUP",
                    "support_issue_types": ["Bug", "Support"], "post_comments": True}
    team["lead"] = "lead@x.test"
    team["approvers"] = ["lead@x.test", "sai@x.test", "ravi@x.test"]
    team["members"] = {"sai": {"name": "Sai", "email": "sai@x.test", "github": "sai-gh", "daily_token_budget": 400000},
                       "ravi": {"name": "Ravi", "email": "ravi@x.test", "github": "ravi-gh", "daily_token_budget": 400000}}
    team["models"] = {k: "test-model" for k in team["models"]}
    team["models"]["escalation"] = "strong-model"
    team["thresholds"]["new_ticket_cooldown_minutes"] = 0
    team["delivery"] = {"push": True, "pull_request": True}  # tests of the local-only default switch this off
    py = f'"{sys.executable}"'  # quoted: Windows paths often contain spaces
    team["test"] = {"full": f"{py} -m pytest -q -p no:cacheprovider",
                    "targeted": f"{py} -m pytest -q -p no:cacheprovider {{test_path}}"}
    (sh / "config" / "team.json").write_text(json.dumps(team))
    cfg = C.Config(user_id="sai", user_email="sai@x.test", repo_path=str(repo), shared_dir=str(sh), mcp_mode="off")
    C.save(cfg)
    cfg = C.load()

    ns = SimpleNamespace(cfg=cfg, repo=repo, origin=origin, shared=sh, home=home, tmp=tmp_path, scenario=scenario)

    def script(**agents):
        scenario.write_text(json.dumps(agents))
    ns.script = script

    def add_ticket(t):
        (sh / "jira-export" / f"{t['key']}.json").write_text(json.dumps(t))
    ns.add_ticket = add_ticket

    def calls():
        p = scenario.with_suffix(".calls.jsonl")
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []
    ns.calls = calls

    def outbox():
        return {p.name: json.loads(p.read_text()) for p in (sh / "outbox").glob("*.json")}
    ns.outbox = outbox
    return ns
