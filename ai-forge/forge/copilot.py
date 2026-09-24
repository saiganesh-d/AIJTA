"""Single entry point for every Copilot CLI call: agent, model, permissions, MCP, budget, token ledger."""
import json
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .config import HOME, Config

COPILOT_BIN = "copilot"

# Permissions per agent. Deny always wins over allow in Copilot CLI.
ALWAYS_DENY = ["shell(git push)", "shell(git commit)", "shell(git rebase)", "shell(git reset)", "shell(rm)",
               "shell(curl)", "shell(wget)", "shell(ssh)", "shell(pip)", "shell(npm)", "url"]
PROFILES = {
    "forge-analyst": {"allow": ["forge-index"], "deny": ["write", "shell"]},
    "forge-config":  {"allow": ["forge-index"], "deny": ["write", "shell"]},
    "forge-fixer":   {"allow": ["write", "forge-index", "shell(git diff)", "shell(git status)"], "deny": []},
    "forge-adapter": {"allow": ["write", "forge-index", "shell(git diff)", "shell(git status)"], "deny": []},
    "forge-doctor":  {"allow": ["forge-index"], "deny": ["write", "shell"]},
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


def tokens_today(user_budget_key: str = "") -> int:
    con = _ledger()
    row = con.execute("SELECT COALESCE(SUM(COALESCE(input_tokens, est_input_tokens) + COALESCE(output_tokens,0)),0) "
                      "FROM calls WHERE day=?", (date.today().isoformat(),)).fetchone()
    return int(row[0])


def mcp_config_path() -> Path:
    return HOME / "mcp.json"


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
    members = cfg.team.get("members") or {}
    budget = (members.get(cfg.user_id) or {}).get("daily_token_budget", 400_000)
    if tokens_today() >= budget:
        raise BudgetExceeded(f"daily token budget {budget:,} reached")

    prof = PROFILES[agent]
    model = model or cfg.model_for(agent)
    cmd = [COPILOT_BIN, "--agent", agent, "-p", prompt, "--model", model, "--no-ask-user"]
    if cfg.mcp_mode in ("agent", "global"):
        cmd += ["--additional-mcp-config", f"@{mcp_config_path()}"]
    for t in prof["allow"] + (extra_allow or []):
        if t == "forge-index" and cfg.mcp_mode == "off":
            continue
        cmd += ["--allow-tool", t]
    for t in ALWAYS_DENY + prof["deny"]:
        cmd += ["--deny-tool", t]

    est = (len(prompt) + context_chars) // 4
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace")
        ok, out, err = proc.returncode == 0, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as e:
        ok, out, err = False, (e.stdout or ""), f"timeout after {timeout}s"
    dur = int(time.time() - t0)
    usage = parse_usage(out + "\n" + err)
    con = _ledger()
    con.execute("INSERT INTO calls(day, tickets, agent, model, ok, duration_s, est_input_tokens, input_tokens,"
                " output_tokens, cached_tokens, mcp_mode, raw_tail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (date.today().isoformat(), ",".join(tickets), agent, model, int(ok), dur, est,
                 usage["input"], usage["output"], usage["cached"], cfg.mcp_mode,
                 "\n".join((out + "\n" + err).splitlines()[-25:])))
    con.commit()
    return RunResult(ok, out, err, dur, usage, est)


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
