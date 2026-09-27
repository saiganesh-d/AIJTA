"""forge-index MCP server (stdio). Gives Copilot compact, targeted code lookups
instead of grepping and opening whole files. Run by Copilot CLI as: forge mcp

Never print to stdout here: stdout is the MCP protocol channel."""
from . import config as C
from .copilot import log_lookup
from .index.store import Index
from .shared import TICKET_FILE, read_dir
from .signals import error_signature

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

cfg = C.load()
idx = Index(cfg.index_db, cfg.repo_path)
server = _Server("forge-index")


def _log(tool: str, arg, rows, read: bool) -> None:
    """Record what the agent looked up, so `forge gaps` can show what the context pack missed."""
    if cfg.ctx("learn_gaps"):
        try:
            log_lookup(C.HOME, tool, arg, [f"{r['path']}::{r['qualname']}" for r in rows], read)
        except Exception:
            pass  # logging must never break a lookup


@server.tool()
def ping() -> str:
    """Health check. Returns the indexed commit."""
    return f"forge-index-ok:{idx.meta('commit')[:10]}"


@server.tool()
def search_code(query: str, k: int = 8) -> str:
    """Ranked code search over the whole repo (identifiers, strings, log messages).
    Returns one line per hit: path:start-end kind qualname | signature. Use get_symbol to read code."""
    hits = idx.search(query, k=min(k, 15), kinds=("function", "class", "block"))
    _log("search_code", query, hits, read=False)
    return "\n".join(Index.fmt(s) for s in hits) or "no hits"


@server.tool()
def get_symbol(name: str, max_lines: int = 80) -> str:
    """Source of a function/class by name or qualname (e.g. 'Parser.load'), with line numbers.
    Prefer this over reading whole files."""
    rows = idx.symbols_named(name, limit=3)
    _log("get_symbol", name, rows, read=True)
    if not rows:
        return "not found"
    return "\n\n".join(f"{Index.fmt(r)}\n{idx.body(r, max_lines=min(max_lines, 150))}" for r in rows)


@server.tool()
def symbol_at(path: str, line: int) -> str:
    """The innermost function/class containing path:line (use for stack-trace frames)."""
    r = idx.symbol_at(path, line)
    _log("symbol_at", f"{path}:{line}", [r] if r else [], read=True)
    return f"{Index.fmt(r)}\n{idx.body(r, max_lines=80)}" if r else "not found"


@server.tool()
def file_outline(path: str) -> str:
    """List of functions/classes in a file with line ranges (no bodies)."""
    rows = idx.outline(path)
    _log("file_outline", path, rows, read=False)
    return "\n".join(Index.fmt(r) for r in rows) or "no symbols (or file not indexed)"


@server.tool()
def get_callers(name: str) -> str:
    """Functions that call `name` (call-graph, name-based)."""
    rows = idx.callers(name)
    _log("get_callers", name, rows, read=False)
    return "\n".join(Index.fmt(r) for r in rows) or "no callers found"


@server.tool()
def get_callees(name: str) -> str:
    """Functions called by `name`."""
    rows = idx.symbols_named(name, limit=1)
    if not rows:
        return "not found"
    callees = idx.callees(rows[0]["id"])
    _log("get_callees", name, callees, read=False)
    return "\n".join(Index.fmt(r) for r in callees) or "no callees found"


@server.tool()
def config_lookup(query: str, k: int = 6) -> str:
    """Search configuration files (yaml/json/ini/properties/env/xml/toml) for keys or values."""
    hits = idx.search(query, k=k, kinds=("config",))
    _log("config_lookup", query, hits, read=True)
    return "\n\n".join(f"{s['path']}:{s['start']}-{s['end']}\n{idx.body(s, max_lines=40)}" for s in hits) or "no hits"


@server.tool()
def related_tickets(text: str, k: int = 3) -> str:
    """Past tickets related to `text` (an error, stack trace, symptom or function names): matched on the
    code they point to first, wording last. One line each: why it matched, root cause, fix commit state."""
    from .store import Store
    from .triage import past_matches
    probe = {"key": "LOOKUP-0", "summary": text[:200], "description": text, "comments": [], "attachments": [],
             "signature": error_signature(text)}
    found = past_matches(cfg, Store(cfg.db_path), idx, [probe], k=min(k, 5))
    return "\n".join(m["line"] for m in found) or "none"


@server.tool()
def inflight_changes(paths_or_symbols: str) -> str:
    """Teammates' in-progress or recently merged changes touching the given comma-separated paths/symbols."""
    wanted = {w.strip() for w in paths_or_symbols.split(",") if w.strip()}
    out = []
    for key, f in read_dir(cfg.shared / "inflight", TICKET_FILE).items():
        if f.get("owner") == cfg.user_id or f.get("status") in ("abandoned",):
            continue
        touched = set(f.get("changed_files", [])) | set(f.get("changed_symbols", [])) | set(f.get("planned_symbols", []))
        hit = {t for t in touched if any(w in t or t in w for w in wanted)}
        if hit:
            out.append(f"{key} by {f.get('owner')} [{f.get('status')}] branch={f.get('branch')} touches: {', '.join(sorted(hit))[:300]}")
    return "\n".join(out) or "none"


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
