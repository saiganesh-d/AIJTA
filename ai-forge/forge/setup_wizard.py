"""`forge setup`: one-time, mostly automatic onboarding for each team member."""
import getpass
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import config as C
from .copilot import write_mcp_config
from .shared import atomic_write, ensure_layout

COPILOT_HOME = Path.home() / ".copilot"


def _clean_input(value: str) -> str:
    """Windows 'Copy as path' pastes "C:\\path" with quotes; strip them."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1].strip()
    return value


def _ask(label: str, default: str = "") -> str:
    v = _clean_input(input(f"{label}{f' [{default}]' if default else ''}: "))
    return v or default


def _team_defaults(shared: str) -> dict:
    p = Path(shared) / "config" / "team.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        return {}


def _is_placeholder(url: str) -> bool:
    url = (url or "").strip().lower()
    return not url or "yourorg" in url


def find_shared_folder() -> str:
    roots = [os.environ.get(k) for k in ("OneDriveCommercial", "OneDrive")] + [str(Path.home())]
    for root in filter(None, roots):
        base = Path(root)
        for depth in ("*", "*/*", "*/*/*"):
            for p in base.glob(f"{depth}/AI-Forge-Shared"):
                if p.is_dir():
                    return str(p)
    return ""


def sync_agents(cfg: C.Config) -> int:
    """Team-wide agent updates: agents in the shared folder win over the bundled ones."""
    src = cfg.shared / "agents"
    if not any(src.glob("forge-*.agent.md")):
        src = C.ASSETS / "agents"
    dst = COPILOT_HOME / "agents"
    dst.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in src.glob("forge-*.agent.md"):
        target = dst / f.name
        if not target.exists() or target.read_bytes() != f.read_bytes():
            shutil.copy2(f, target)
            n += 1
    return n


def trust_worktrees(cfg: C.Config) -> None:
    """Pre-trust the worktree folder so programmatic runs don't stop at the folder-trust prompt.
    Key name per current Copilot CLI config; `forge doctor --live` verifies it works."""
    p = COPILOT_HOME / "config.json"
    data = {}
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            return
    folders = data.setdefault("trusted_folders", [])
    for f in (str(cfg.worktree_root), str(C.HOME)):
        if f not in folders:
            folders.append(f)
    COPILOT_HOME.mkdir(exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def schedule_task() -> str:
    exe = Path(sys.executable).with_name("forge.exe" if sys.platform == "win32" else "forge")
    if sys.platform == "win32":
        # Hidden launcher: a console app started by Task Scheduler would flash a window every 10 minutes.
        vbs = C.HOME / "forge-run.vbs"
        vbs.write_text(f'CreateObject("WScript.Shell").Run """{exe}"" run", 0, False\r\n', encoding="utf-8")
        cmd = ["schtasks", "/Create", "/TN", "AI-Forge", "/TR", f'wscript.exe "{vbs}"', "/SC", "MINUTE", "/MO", "10", "/F"]
        r = subprocess.run(cmd, capture_output=True, text=True)
        return "scheduled every 10 min (Task Scheduler: AI-Forge)" if r.returncode == 0 else f"schtasks failed: {r.stderr}"
    return f"add to crontab:  */10 * * * * {exe} run >> {C.HOME}/run.log 2>&1"


def seed_team_folder(cfg: C.Config, jira_base_url: str = "") -> None:
    """Create team.json from the example on first run; fill in the Jira URL (if still a placeholder) and
    your display name/email. Existing values are never overwritten."""
    team = cfg.shared / "config" / "team.json"
    created = not team.exists()
    data = json.loads((C.ASSETS / "team.example.json" if created else team).read_text(encoding="utf-8"))
    changed = created
    jira = data.setdefault("jira", {})
    if jira_base_url and _is_placeholder(jira.get("base_url", "")):
        jira["base_url"] = jira_base_url.rstrip("/")
        if ".atlassian.net" in jira_base_url:
            jira["api_version"] = "3"
        changed = True
    me = data.setdefault("members", {}).setdefault(cfg.user_id, {})
    for k, v in (("name", cfg.user_id), ("email", cfg.user_email)):
        if v and not me.get(k):
            me[k] = v
            changed = True
    if changed:
        team.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"! {'Created' if created else 'Updated'} {team}. Check jira.scope_jql, models and the test commands.")
    agents = cfg.shared / "agents"
    if not any(agents.glob("forge-*.agent.md")):
        for f in (C.ASSETS / "agents").glob("forge-*.agent.md"):
            shutil.copy2(f, agents / f.name)
        print(f"! Seeded shared agents into {agents} (edit there to update the whole team).")


def run() -> None:
    print("AI Forge setup\n")
    existing = json.loads(C.LOCAL_CONFIG.read_text(encoding="utf-8")) if C.LOCAL_CONFIG.exists() else {}
    shared = _ask("Shared folder ('AI-Forge-Shared' inside your work OneDrive)",
                  existing.get("shared_dir") or find_shared_folder())
    if not Path(shared).is_dir():
        raise SystemExit("Shared folder not found. Create AI-Forge-Shared in your work OneDrive, let it sync, then rerun.")
    defaults = _team_defaults(shared)
    user_id = _ask("Your short id (e.g. sai)", existing.get("user_id", getpass.getuser().lower()))
    email = _ask("Your work email (the one you sign in to Teams with)", existing.get("user_email", ""))
    url = os.environ.get("JIRA_BASE_URL") or (defaults.get("jira") or {}).get("base_url", "")
    jira_base_url = _ask("Jira base URL, e.g. https://yourcompany.atlassian.net (Enter to skip)",
                         "" if _is_placeholder(url) else url)
    repo = _ask("Path to your local clone of the support project", existing.get("repo_path", ""))
    if not (Path(repo) / ".git").exists():
        raise SystemExit("That path is not a git repository.")

    cfg = C.Config(user_id=user_id, user_email=email, repo_path=repo, shared_dir=shared,
                   mcp_mode=existing.get("mcp_mode", "agent"))
    ensure_layout(cfg.shared)
    seed_team_folder(cfg, jira_base_url)
    C.save(cfg)
    cfg = C.load()

    file_mode = (cfg.team.get("jira") or {}).get("mode") == "file"
    if not file_mode and (not C.jira_token(email) or _ask("Update Jira token? (y/N)", "n").lower() == "y"):
        cloud = str((cfg.team.get("jira") or {}).get("api_version", "2")) == "3"
        label = ("Jira API token (create at id.atlassian.com → Security → API tokens)" if cloud
                 else "Jira personal access token")
        C.set_jira_token(email, getpass.getpass(f"{label} (stored in OS keychain): "))

    print("• MCP config:", write_mcp_config())
    print("• agents updated:", sync_agents(cfg))
    trust_worktrees(cfg)

    from .index.indexer import build_repo_map, index_repo
    print("• building code index (first run can take a few minutes)...")
    index_repo(cfg.repo_path, cfg.base_ref, cfg.index_db, ext_map=cfg.index_ext())
    build_repo_map(cfg.index_db, cfg.repo_map)
    print("•", schedule_task())

    problems = C.validate_team(cfg.team, cfg.user_id)
    if problems:
        print("! team.json needs attention:")
        for pr in problems:
            print("   -", pr)
    atomic_write(cfg.shared / "runners" / f"{cfg.user_id}.json",
                 {"schema": 1, "user": cfg.user_id, "status": "installed"})
    from .doctor import run as doctor
    doctor(live=False)
    print("\nNext: `forge doctor --live` (one tiny Copilot call) to verify agent + MCP wiring.")
