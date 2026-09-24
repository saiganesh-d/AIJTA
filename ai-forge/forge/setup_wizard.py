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


def _ask(label: str, default: str = "") -> str:
    v = input(f"{label}{f' [{default}]' if default else ''}: ").strip()
    return v or default


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


def seed_team_folder(cfg: C.Config) -> None:
    team = cfg.shared / "config" / "team.json"
    if not team.exists():
        shutil.copy2(C.ASSETS / "team.example.json", team)
        print(f"! Created {team} from the example. The team lead must fill models, members and approvers.")
    agents = cfg.shared / "agents"
    if not any(agents.glob("forge-*.agent.md")):
        for f in (C.ASSETS / "agents").glob("forge-*.agent.md"):
            shutil.copy2(f, agents / f.name)
        print(f"! Seeded shared agents into {agents} (edit there to update the whole team).")


def run() -> None:
    print("AI Forge setup\n")
    existing = json.loads(C.LOCAL_CONFIG.read_text()) if C.LOCAL_CONFIG.exists() else {}
    shared = _ask("Shared folder (synced SharePoint 'AI-Forge-Shared')", existing.get("shared_dir") or find_shared_folder())
    if not Path(shared).is_dir():
        raise SystemExit("Shared folder not found. Sync the team SharePoint library first, then rerun.")
    user_id = _ask("Your short id (e.g. sai)", existing.get("user_id", getpass.getuser().lower()))
    email = _ask("Your work email", existing.get("user_email", ""))
    repo = _ask("Path to your local clone of the support project", existing.get("repo_path", ""))
    if not (Path(repo) / ".git").exists():
        raise SystemExit("That path is not a git repository.")

    cfg = C.Config(user_id=user_id, user_email=email, repo_path=repo, shared_dir=shared,
                   mcp_mode=existing.get("mcp_mode", "agent"))
    ensure_layout(cfg.shared)
    seed_team_folder(cfg)
    C.save(cfg)
    cfg = C.load()

    file_mode = (cfg.team.get("jira") or {}).get("mode") == "file"
    if not file_mode and (not C.jira_token(email) or _ask("Update Jira token? (y/N)", "n").lower() == "y"):
        C.set_jira_token(email, getpass.getpass("Jira personal access token (stored in OS keychain): "))

    print("• MCP config:", write_mcp_config())
    print("• agents updated:", sync_agents(cfg))
    trust_worktrees(cfg)

    from .index.indexer import build_repo_map, index_repo
    print("• building code index (first run can take a few minutes)...")
    index_repo(cfg.repo_path, cfg.base_ref, cfg.index_db)
    build_repo_map(cfg.index_db, cfg.repo_map)
    print("•", schedule_task())

    problems = C.validate_team(cfg.team, cfg.user_id)
    if problems:
        print("! team.json needs attention (the lead fixes this once for everyone):")
        for pr in problems:
            print("   -", pr)
    atomic_write(cfg.shared / "runners" / f"{cfg.user_id}.json",
                 {"schema": 1, "user": cfg.user_id, "status": "installed"})
    from .doctor import run as doctor
    doctor(live=False)
    print("\nNext: `forge doctor --live` (one tiny Copilot call) to verify agent + MCP wiring.")
