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
    sub.add_parser("status", help="my tickets and where each one is in the pipeline")
    rp = sub.add_parser("report", help="savings report (HTML) for management")
    rp.add_argument("--team", action="store_true", help="aggregate all runners from the shared folder")
    rp.add_argument("--out", help="output .html path")
    b = sub.add_parser("baseline", help="baseline experiment: plain Copilot vs Forge on closed tickets")
    b.add_argument("tickets_dir", help="folder of ticket JSON files with an 'expected' block")
    b.add_argument("--no-fix", action="store_true", help="compare analysis only (skip the fixer run)")
    sub.add_parser("gaps", help="code Copilot needed that the context pack did not contain (tune retrieval)")
    cr = sub.add_parser("check-retrieval", help="free: does the context pack find the right code and past tickets?")
    cr.add_argument("tickets_dir", help="folder of closed-ticket JSON files with an 'expected' block")
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
        from . import analyze, triage
        from .signals import error_signature
        from .store import Store
        cfg = C.load()
        text = Path(a.file).read_text(encoding="utf-8", errors="replace")
        idx = Index(cfg.index_db, cfg.repo_path)
        t = {"key": "LOCAL-1", "summary": text.splitlines()[0] if text else "", "description": text,
             "comments": [], "attachments": [], "signature": error_signature(text)}
        matches = triage.past_matches(cfg, Store(cfg.db_path), idx, [t])  # same sources as a real analysis
        extras = {"past_matches": [m["line"] for m in matches] if cfg.ctx("past_tickets") else [],
                  "recent_changes": analyze.recent_for(cfg, idx, text),
                  "past_evidence": analyze.past_evidence(cfg, matches)}
        pack, stats = build(idx, [analyze.pack_ticket(t)], extras,
                            budget_tokens=cfg.threshold("context_budget_tokens", 7000))
        stats.pop("symbols", None)
        sys.stdout.write(pack)
        print(f"\n---\n{stats}", file=sys.stderr)
    elif a.cmd == "run":
        from .pipeline import run_once
        run_once(force=a.force)
    elif a.cmd == "status":
        from . import config as C
        from .store import Store
        st = Store(C.load().db_path)
        print("ticket | status | route | group | updated | pr")
        for t in st.tickets():
            print(" | ".join(str(t.get(k) or "-") for k in ("key", "status", "route", "group_id", "status_changed", "pr_url")))
        for p in st.pending():
            print(f"pending card {p['request_id']} ({p['kind']}) for {p['ticket_key']} since {p['created']}")
    elif a.cmd == "report":
        from pathlib import Path
        from . import config as C
        from .metrics import report
        from .store import Store
        cfg = C.load()
        print(report(cfg, Store(cfg.db_path), team=a.team, out=Path(a.out) if a.out else None))
    elif a.cmd == "baseline":
        from pathlib import Path
        from . import config as C
        from .metrics import baseline
        out = baseline(C.load(), Path(a.tickets_dir), with_fix=not a.no_fix)
        print(out.read_text(encoding="utf-8"))
        print(f"saved: {out}")
    elif a.cmd == "gaps":
        from . import config as C
        from .store import Store
        st = Store(C.load().db_path)
        hit, total = st.coverage()
        print(f"Evidence already in the context pack: {hit}/{total}" + (f" ({hit / total:.0%})" if total else ""))
        print("symbol | times missed | via | tickets")
        for g in st.gaps():
            print(f"{g['symbol']} | {g['times']} | {g['sources']} | {g['tickets']}")
    elif a.cmd == "check-retrieval":
        from pathlib import Path
        from . import config as C
        from .evaluate import check_retrieval
        out = check_retrieval(C.load(), Path(a.tickets_dir))
        print(out.read_text(encoding="utf-8"))
        print(f"saved: {out}")
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
