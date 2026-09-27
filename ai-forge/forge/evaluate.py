"""`forge check-retrieval <folder>`: how good is the context we would give Copilot? Zero Copilot calls.

For closed tickets with a known answer (same files as `forge baseline`: expected.files, optionally
expected.related = past ticket keys a human would call related), it measures:
- top-3: a fixed file is among the 3 strongest code locations the ticket points to
- in pack: share of the fixed files whose code is in the context pack
- related: share of the expected past tickets found by past-ticket retrieval
- pack tokens: size of what Copilot would receive
Tune team.json → context against these numbers before spending any credits."""
import json
import statistics
from datetime import datetime
from pathlib import Path

from . import analyze, triage
from . import config as C
from .context_pack import build as build_pack
from .history import sync_ticket_map
from .index.indexer import index_repo
from .index.store import Index
from .signals import error_signature
from .store import Store


def check_retrieval(cfg: C.Config, tickets_dir: Path, log=print) -> Path:
    index_repo(cfg.repo_path, cfg.base_ref, cfg.index_db, fetch=False, log=lambda *_: None)
    if cfg.ctx("git_ticket_map"):
        sync_ticket_map(cfg.repo_path, cfg.base_ref, cfg.index_db)
    idx, store = Index(cfg.index_db, cfg.repo_path), Store(cfg.db_path)
    rows = []
    for p in sorted(tickets_dir.glob("*.json")):
        t = json.loads(p.read_text(encoding="utf-8"))
        for k in ("attachments", "comments"):
            t.setdefault(k, [])
        exp = t.get("expected") or {}
        want, related = set(exp.get("files") or []), set(exp.get("related") or [])
        text = triage.full_text(t)
        t["signature"] = error_signature(text)
        _, files = triage.related_code(idx, text)
        matches = triage.past_matches(cfg, store, idx, [t])
        extras = {"past_matches": [m["line"] for m in matches] if cfg.ctx("past_tickets") else [],
                  "recent_changes": analyze.recent_for(cfg, idx, text),
                  "past_evidence": analyze.past_evidence(cfg, matches)}
        _, stats = build_pack(idx, [analyze.pack_ticket(t, cfg.ctx("all_comments"))], extras,
                              budget_tokens=cfg.threshold("context_budget_tokens", 7000))
        found = [m["key"] for m in matches]
        rank = next((i for i, f in enumerate(files, 1) if f in want), None)
        rows.append({"key": t["key"], "expected_files": sorted(want), "top3": bool(want & set(files[:3])),
                     "rank": rank, "in_pack": len(want & set(stats["files"])), "want": len(want),
                     "related_found": len(related & set(found)), "related_want": len(related),
                     "matched": [f"{m['key']} ({m['why']})" for m in matches], "pack_tokens": stats["est_tokens"]})
        log(f"{t['key']}: top3={rows[-1]['top3']} rank={rank} pack={stats['est_tokens']} tok matched={found}")
    return _write(rows)


def _write(rows: list[dict]) -> Path:
    out = C.HOME / "eval" / f"retrieval-{datetime.now():%Y%m%d-%H%M}"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    n = len(rows) or 1
    rel_want = sum(r["related_want"] for r in rows)
    lines = ["| ticket | fixed file in top 3 | rank | fixed files in pack | related found | past tickets matched | pack tokens |",
             "|---|---|---:|---|---|---|---:|"]
    for r in rows:
        rel = f"{r['related_found']}/{r['related_want']}" if r["related_want"] else "-"
        lines.append(f"| {r['key']} | {'✓' if r['top3'] else '✗'} | {r['rank'] or '-'} | {r['in_pack']}/{r['want']} | "
                     f"{rel} | {'; '.join(r['matched']) or '-'} | {r['pack_tokens']:,} |")
    lines += ["", f"**Fixed file in top 3:** {sum(r['top3'] for r in rows)}/{len(rows)} · "
              f"**Fixed files in pack:** {sum(r['in_pack'] for r in rows)}/{sum(r['want'] for r in rows)} · "
              + (f"**Related found:** {sum(r['related_found'] for r in rows)}/{rel_want} · " if rel_want else "")
              + f"**Median pack:** {int(statistics.median([r['pack_tokens'] for r in rows])) if rows else 0:,} tokens",
              "", "Zero Copilot calls. Misses on tickets without a stack trace point at wording-only retrieval: "
              "that is where local embeddings would help."]
    out.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out.with_suffix(".md")
