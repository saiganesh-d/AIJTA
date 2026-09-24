import argparse
import sys


def main() -> None:
    ap = argparse.ArgumentParser(prog="forge", description="AI Forge support-ticket automation")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup", help="one-time onboarding")
    d = sub.add_parser("doctor", help="verify installation")
    d.add_argument("--live", action="store_true", help="one tiny Copilot call to test agent + MCP wiring")
    i = sub.add_parser("index", help="refresh code index + repo map")
    i.add_argument("--full", action="store_true")
    sub.add_parser("mcp", help="run the forge-index MCP server on stdio (started by Copilot)")
    s = sub.add_parser("search", help="debug: search the code index")
    s.add_argument("query")
    c = sub.add_parser("context", help="debug/demo: build a context pack from a text file and show its token size")
    c.add_argument("file")
    r = sub.add_parser("run", help="one pipeline cycle (scheduled)")
    r.add_argument("--force", action="store_true", help="ignore work hours")
    sub.add_parser("stats", help="token/call ledger summary")
    a = ap.parse_args()

    if a.cmd == "setup":
        from .setup_wizard import run
        run()
    elif a.cmd == "doctor":
        from .doctor import run
        run(live=a.live)
    elif a.cmd == "index":
        from . import config as C
        from .index.indexer import build_repo_map, index_repo
        cfg = C.load()
        index_repo(cfg.repo_path, cfg.base_ref, cfg.index_db, full=a.full)
        build_repo_map(cfg.index_db, cfg.repo_map)
        print(f"repo map: {cfg.repo_map}")
    elif a.cmd == "mcp":
        from .mcp_server import main as serve
        serve()
    elif a.cmd == "search":
        from . import config as C
        from .index.store import Index
        cfg = C.load()
        idx = Index(cfg.index_db, cfg.repo_path)
        for h in idx.search(a.query, k=10):
            print(Index.fmt(h))
    elif a.cmd == "context":
        from pathlib import Path
        from . import config as C
        from .context_pack import build
        from .index.store import Index
        cfg = C.load()
        text = Path(a.file).read_text(encoding="utf-8", errors="replace")
        pack, stats = build(Index(cfg.index_db, cfg.repo_path),
                            [{"key": "LOCAL-1", "summary": text.splitlines()[0] if text else "", "description": text}],
                            budget_tokens=cfg.threshold("context_budget_tokens", 7000))
        sys.stdout.write(pack)
        print(f"\n---\n{stats}", file=sys.stderr)
    elif a.cmd == "run":
        from .pipeline import run_once
        run_once(force=a.force)
    elif a.cmd == "stats":
        from .copilot import _ledger
        rows = _ledger().execute(
            "SELECT day, agent, COUNT(*), SUM(COALESCE(input_tokens, est_input_tokens)), SUM(output_tokens), "
            "ROUND(AVG(duration_s)) FROM calls GROUP BY day, agent ORDER BY day DESC LIMIT 30").fetchall()
        print("day | agent | calls | input tok | output tok | avg s")
        for r in rows:
            print(" | ".join(str(x) for x in r))


if __name__ == "__main__":
    main()
