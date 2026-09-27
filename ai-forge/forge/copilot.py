"""Single entry point for every Copilot CLI call: agent, model, permissions, MCP, budget, token ledger."""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .config import HOME, Config

COPILOT_BIN = os.environ.get("AI_FORGE_COPILOT_BIN", "copilot")
AGENTS_DIR = Path.home() / ".copilot" / "agents"


def tool_cmd(name: str) -> list[str]:
    """argv prefix for an external CLI. A `.py` path runs with this Python (test doubles, any OS);
    otherwise resolve it (on Windows npm installs a copilot.cmd shim subprocess can't find by name)."""
    if name.endswith(".py"):
        return [sys.executable, name]
    return [shutil.which(name) or name]


def copilot_cmd() -> list[str]:
    return tool_cmd(COPILOT_BIN)

# Permissions per agent. Deny always wins over allow in Copilot CLI.
ALWAYS_DENY = ["shell(git push)", "shell(git commit)", "shell(git rebase)", "shell(git reset)", "shell(rm)",
               "shell(curl)", "shell(wget)", "shell(ssh)", "shell(pip)", "shell(npm)", "url"]
PROFILES = {
    "forge-analyst": {"allow": ["forge-index"], "deny": ["write", "shell"]},
    "forge-config":  {"allow": ["forge-index"], "deny": ["write", "shell"]},
    "forge-fixer":   {"allow": ["write", "forge-index", "shell(git diff)", "shell(git status)"], "deny": []},
    "forge-adapter": {"allow": ["write", "forge-index", "shell(git diff)", "shell(git status)"], "deny": []},
    "forge-doctor":  {"allow": ["forge-index"], "deny": ["write", "shell"]},
    # plain Copilot for the baseline experiment (§5.16): what an engineer would run by hand
    "baseline":      {"allow": ["write", "shell"], "deny": []},
}

USAGE_RX = {
    "input": re.compile(r"(?i)\b(?:input|prompt)\b[^\n\d]{0,20}([\d.,]+\s*[kKmM]?)"),
    "output": re.compile(r"(?i)\b(?:output|completion)\b[^\n\d]{0,20}([\d.,]+\s*[kKmM]?)"),
    "cached": re.compile(r"(?i)\bcache[d]?\b[^\n\d]{0,20}([\d.,]+\s*[kKmM]?)"),
}


class BudgetExceeded(Exception):
    pass


@dataclass
class RunResult:
    ok: bool
    stdout: str
    stderr: str
    duration_s: int
    usage: dict
    est_input_tokens: int
    lookups: list = field(default_factory=list)  # forge-index calls the agent made (MCP modes only)

    @property
    def tokens(self) -> int:
        """Parsed input+output when the CLI reported usage, else the local estimate."""
        return (self.usage.get("input") or self.est_input_tokens) + (self.usage.get("output") or 0)


def _ledger() -> sqlite3.Connection:
    con = sqlite3.connect(str(HOME / "ledger.db"))
    con.execute("""CREATE TABLE IF NOT EXISTS calls(
        ts TEXT DEFAULT CURRENT_TIMESTAMP, day TEXT, tickets TEXT, agent TEXT, model TEXT, ok INT,
        duration_s INT, est_input_tokens INT, input_tokens INT, output_tokens INT, cached_tokens INT,
        mcp_mode TEXT, raw_tail TEXT)""")
    return con


def _num(s: str | None) -> int | None:
    if not s:
        return None
    s = s.strip().replace(",", "")
    mult = 1000 if s[-1:] in "kK" else 1_000_000 if s[-1:] in "mM" else 1
    try:
        return int(float(s.rstrip("kKmM ")) * mult)
    except ValueError:
        return None


def parse_usage(text: str) -> dict:
    """Best-effort parse of the usage summary Copilot CLI prints at the end of a run.
    The raw tail is stored too, so parsing can be recalibrated against the real format."""
    tail = "\n".join(text.splitlines()[-25:])
    return {k: _num((rx.search(tail) or [None, None])[1]) for k, rx in USAGE_RX.items()}


def _tokens_since(day: str) -> int:
    con = _ledger()
    row = con.execute("SELECT COALESCE(SUM(COALESCE(input_tokens, est_input_tokens) + COALESCE(output_tokens,0)),0) "
                      "FROM calls WHERE day>=?", (day,)).fetchone()
    return int(row[0])


def tokens_today() -> int:
    return _tokens_since(date.today().isoformat())


def tokens_this_month() -> int:
    return _tokens_since(date.today().replace(day=1).isoformat())


def check_budget(cfg: Config) -> None:
    """Hard stop before any Copilot call. Monthly = your Copilot allowance spread over the month."""
    daily, monthly = cfg.budget("daily_tokens", 400_000), cfg.budget("monthly_tokens", 0)
    if daily and tokens_today() >= daily:
        raise BudgetExceeded(f"daily token budget {daily:,} reached")
    if monthly and tokens_this_month() >= monthly:
        raise BudgetExceeded(f"monthly token budget {monthly:,} reached")


def mcp_config_path() -> Path:
    return HOME / "mcp.json"


def agent_instructions(agent: str) -> str:
    """Body of the synced agent file without its YAML front matter (used when mcp_mode=global)."""
    p = AGENTS_DIR / f"{agent}.agent.md"
    if not p.exists():
        from .config import ASSETS
        p = ASSETS / "agents" / f"{agent}.agent.md"
    text = p.read_text(encoding="utf-8")
    if text.startswith("---"):
        text = text.split("---", 2)[2]
    return text.strip()


def write_mcp_config() -> Path:
    exe = Path(sys.executable).with_name("forge.exe" if sys.platform == "win32" else "forge")
    cfg = {"mcpServers": {"forge-index": {
        "type": "local", "command": str(exe), "args": ["mcp"], "tools": ["*"],
        "env": {"AI_FORGE_HOME": str(HOME)}}}}
    p = mcp_config_path()
    p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return p


def run_agent(cfg: Config, agent: str, prompt: str, cwd: Path, tickets: list[str],
              extra_allow: list[str] | None = None, model: str | None = None,
              context_chars: int = 0, timeout: int = 1200) -> RunResult:
    check_budget(cfg)

    prof = PROFILES[agent]
    model = model or cfg.model_for("forge-analyst" if agent == "baseline" else agent)
    exe = copilot_cmd()
    if agent == "baseline":
        cmd = [*exe, "-p", prompt]
    elif cfg.mcp_mode == "global":
        # Some CLI versions only expose MCP tools without --agent: pass the agent's instructions as a
        # prefix file (same bytes every call; a file, because cmd.exe shims mangle multi-line args).
        prefix = Path(cwd) / ".forge" / "AGENT.md"
        prefix.parent.mkdir(parents=True, exist_ok=True)
        prefix.write_text(agent_instructions(agent), encoding="utf-8")
        context_chars += prefix.stat().st_size
        cmd = [*exe, "-p", f"First read .forge/AGENT.md and follow it strictly. Task: {prompt}"]
    else:
        cmd = [*exe, "--agent", agent, "-p", prompt]
    if model != "auto":  # "auto" = let Copilot choose the model (Copilot auto model selection)
        cmd += ["--model", model]
    cmd += ["--no-ask-user"]
    if cfg.mcp_mode in ("agent", "global") and agent != "baseline":
        cmd += ["--additional-mcp-config", f"@{mcp_config_path()}"]
    for t in prof["allow"] + (extra_allow or []):
        if t == "forge-index" and cfg.mcp_mode == "off":
            continue
        cmd += ["--allow-tool", t]
    for t in ALWAYS_DENY + prof["deny"]:
        cmd += ["--deny-tool", t]

    est = (len(cmd[cmd.index("-p") + 1]) + context_chars) // 4
    t0 = time.time()
    run_id = uuid.uuid4().hex[:12]
    HOME.mkdir(parents=True, exist_ok=True)
    _current_run().write_text(json.dumps({"run_id": run_id, "agent": agent, "tickets": tickets}), encoding="utf-8")
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace", env={**os.environ, "AI_FORGE_HOME": str(HOME)})
        ok, out, err = proc.returncode == 0, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as e:
        ok, out, err = False, (e.stdout or ""), f"timeout after {timeout}s"
    finally:
        _current_run().unlink(missing_ok=True)
    dur = int(time.time() - t0)
    lookups = collect_lookups(run_id)
    usage = parse_usage(out + "\n" + err)
    con = _ledger()
    con.execute("INSERT INTO calls(day, tickets, agent, model, ok, duration_s, est_input_tokens, input_tokens,"
                " output_tokens, cached_tokens, mcp_mode, raw_tail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (date.today().isoformat(), ",".join(tickets), agent, model, int(ok), dur, est,
                 usage["input"], usage["output"], usage["cached"], cfg.mcp_mode,
                 "\n".join((out + "\n" + err).splitlines()[-25:])))
    con.commit()
    return RunResult(ok, out, err, dur, usage, est, lookups)


# Lookup log: run_agent marks the current call in <home>/current-run.json; the forge-index MCP server
# (a separate process started by Copilot) appends each tool call to <home>/lookups.jsonl with that run id.
def _current_run(home: Path | None = None) -> Path:
    return (home or HOME) / "current-run.json"


def _lookups(home: Path | None = None) -> Path:
    return (home or HOME) / "lookups.jsonl"


def log_lookup(home: Path, tool: str, arg: str, symbols: list[str], read: bool) -> None:
    """Called by the MCP server. `read` = the agent got code bodies (not just a list of names)."""
    try:
        run = json.loads(_current_run(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return  # not inside a Forge call (e.g. forge doctor)
    with _lookups(home).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"run_id": run["run_id"], "tool": tool, "arg": str(arg)[:200],
                             "symbols": symbols[:15], "read": read}) + "\n")


def collect_lookups(run_id: str) -> list[dict]:
    """forge-index lookups the agent made during this call, then clear them from the log."""
    path = _lookups()
    if not path.exists():
        return []
    mine, rest = [], []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        (mine if rec.get("run_id") == run_id else rest).append(rec)
    path.write_text("".join(json.dumps(r) + "\n" for r in rest), encoding="utf-8")
    return mine


def extract_json(text: str) -> dict:
    """Take the last JSON object in the output (fenced or bare). Repair locally; never re-ask Copilot."""
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = list(reversed(fenced)) or [text[text.find("{"): text.rfind("}") + 1]]
    for c in candidates:
        for attempt in (c, re.sub(r",\s*([}\]])", r"\1", c)):  # strip trailing commas
            try:
                return json.loads(attempt)
            except json.JSONDecodeError:
                continue
    raise ValueError("no valid JSON object in agent output")
