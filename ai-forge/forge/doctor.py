"""`forge doctor [--live]`: verify everything a teammate needs, and pick the MCP mode that works."""
import shutil
import subprocess
from pathlib import Path

import httpx

from . import config as C
from .copilot import copilot_exe, mcp_config_path
from .index.store import Index


def _check(name: str, ok: bool, hint: str = "") -> bool:
    print(f"{'✓' if ok else '✗'} {name}{'' if ok else f'  → {hint}'}")
    return ok


def _cmd_ok(*cmd) -> bool:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def live_mcp_test(cfg: C.Config) -> str:
    """One tiny call with the cheapest model. Known CLI issue: MCP tools may not reach custom
    agents in -p mode on some versions, so we try agent mode, then global, else fall back to off."""
    idx = Index(cfg.index_db, cfg.repo_path)
    expected = f"forge-index-ok:{idx.meta('commit')[:10]}"
    model = cfg.model_for("forge-doctor")
    base = [copilot_exe(), "-p", "Call the forge-index ping tool and reply with its exact output only.",
            "--model", model, "--no-ask-user", "--additional-mcp-config", f"@{mcp_config_path()}",
            "--allow-tool", "forge-index", "--deny-tool", "write", "--deny-tool", "shell"]
    for mode, extra in (("agent", ["--agent", "forge-doctor"]), ("global", [])):
        r = subprocess.run(base + extra, cwd=str(cfg.worktree_root), capture_output=True, text=True, timeout=300)
        if expected in r.stdout:
            return mode
    return "off"


def run(live: bool = False) -> None:
    ok = True
    ok &= _check("git", shutil.which("git") is not None, "install Git for Windows")
    ok &= _check("gh (GitHub CLI) logged in", _cmd_ok("gh", "auth", "status"), "gh auth login")
    ok &= _check("copilot CLI installed", _cmd_ok(copilot_exe(), "--version"), "npm install -g @github/copilot, then run `copilot` once to log in")
    try:
        cfg = C.load()
    except SystemExit as e:
        _check("forge config", False, str(e))
        return
    ok &= _check("shared folder reachable", cfg.shared.is_dir(), "sync the SharePoint library")
    ok &= _check("team.json present", bool(cfg.team), "team lead must create config/team.json")
    problems = C.validate_team(cfg.team, cfg.user_id)
    ok &= _check("team.json valid", not problems, "; ".join(problems[:6]))
    ok &= _check("shared folder writable", _writable(cfg.shared / "runners"), "check SharePoint permissions")
    print("  ! Ensure the shared folder is set to 'Always keep on this device' in OneDrive.")
    j = cfg.team.get("jira") or {}
    if j.get("mode") == "file":
        from .jira import FileJira
        ok &= _check(f"Jira file mode ({FileJira(cfg).dir})", FileJira(cfg).dir.is_dir(), "create the export folder")
    else:
        token = C.jira_token(cfg.user_email)
        ok &= _check("Jira token in keychain", bool(token), "forge setup")
        if token and j.get("base_url"):
            from .jira import Jira
            try:
                Jira(cfg, token).myself()
                ok &= _check("Jira reachable + token valid", True)
            except httpx.HTTPStatusError as e:
                ok &= _check("Jira reachable + token valid", False, f"HTTP {e.response.status_code}")
            except httpx.HTTPError as e:
                ok &= _check("Jira reachable", False, str(e)[:120])
    ok &= _check("code index built", cfg.index_db.exists() and cfg.repo_map.exists(), "forge index")
    ok &= _check("agents installed", any((Path.home() / ".copilot" / "agents").glob("forge-*.agent.md")), "forge setup")
    ok &= _check("MCP config written", mcp_config_path().exists(), "forge setup")
    if live:
        mode = live_mcp_test(cfg)
        cfg.mcp_mode = mode
        C.save(cfg)
        _check(f"Copilot + forge-index MCP (mode={mode})", mode != "off",
               "MCP not reachable in -p mode; running with context pack only (still works, slightly more tokens)")
    print("\nAll good." if ok else "\nFix the ✗ items and rerun `forge doctor`.")


def _writable(folder: Path) -> bool:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        p = folder / ".write-test"
        p.write_text("x")
        p.unlink()
        return True
    except OSError:
        return False
