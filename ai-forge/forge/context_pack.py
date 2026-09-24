"""Build the token-budgeted context pack (.forge/context.md) BEFORE Copilot runs.

This is the guaranteed token saver: it works even if MCP tools are unavailable in
programmatic mode. Priorities: stack-frame code > search hits > callers > history."""
from .index.store import Index
from .signals import estimate_tokens, parse_frames, scrub, trim_log


def _ticket_block(t: dict) -> str:
    parts = [f"### {t['key']}: {scrub(t.get('summary', ''))}",
             f"component: {t.get('component') or '-'} | priority: {t.get('priority') or '-'}",
             scrub(t.get("description", ""))[:2500]]
    for name, txt in t.get("attachments", [])[:4]:
        parts.append(f"attachment `{name}`:\n```\n{trim_log(scrub(txt), max_chars=2500)}\n```")
    return "\n".join(parts)


def build(idx: Index, tickets: list[dict], extras: dict | None = None, budget_tokens: int = 7000) -> tuple[str, dict]:
    extras = extras or {}
    budget = budget_tokens * 4
    out: list[str] = []
    used = 0
    included: set[int] = set()
    stats = {"frames_resolved": 0, "code_blocks": 0, "hits": 0}

    def add(block: str) -> bool:
        nonlocal used
        if used + len(block) > budget:
            return False
        out.append(block)
        used += len(block)
        return True

    add("# Context pack (pre-ranked locally; read this before using any tool)\n")
    add("## Tickets\n" + "\n\n".join(_ticket_block(t) for t in tickets) + "\n")

    all_text = "\n".join(
        f"{t.get('summary', '')}\n{t.get('description', '')}\n" + "\n".join(x for _, x in t.get("attachments", []))
        for t in tickets)

    # 1) code at stack frames: the most precise evidence we can give
    frame_syms = []
    for f in parse_frames(all_text):
        sym = idx.symbol_at(f.path, f.line)
        if sym and sym["id"] not in included:
            included.add(sym["id"])
            frame_syms.append((f, sym))
    if frame_syms:
        add("## Code at stack-trace frames\n")
        for f, sym in frame_syms[:4]:
            if add(f"### {Index.fmt(sym)}  (frame line {f.line})\n```\n{idx.body(sym, max_lines=60)}\n```\n"):
                stats["frames_resolved"] += 1
                stats["code_blocks"] += 1

    # 2) lexical search hits (signatures for all, bodies for the top few)
    hits = [s for s in idx.search(all_text, k=10, kinds=("function", "class")) if s["id"] not in included]
    stats["hits"] = len(hits)
    if hits:
        add("## Likely relevant symbols (search hits)\n" + "\n".join(f"- {Index.fmt(s)}" for s in hits) + "\n")
        ranked = sorted(hits[:4], key=lambda h: h["kind"] != "function")  # prefer function bodies
        for s in ranked[: max(0, 3 - stats["code_blocks"]) + 1]:
            if s["id"] in included:
                continue
            included.add(s["id"])
            if add(f"### {Index.fmt(s)}\n```\n{idx.body(s, max_lines=50)}\n```\n"):
                stats["code_blocks"] += 1

    # 3) call graph around the evidence (signatures only: cheap, but shows where values come from/go to)
    anchors = [s for _, s in frame_syms[:3]] or hits[:1]
    graph: list[str] = []
    for a in anchors:
        for c in idx.callees(a["id"], limit=6):
            if c["id"] not in included:
                graph.append(f"- {a['qualname']} → calls {Index.fmt(c)}")
        for c in idx.callers(a["name"], limit=6):
            if c["id"] not in included:
                graph.append(f"- {a['qualname']} ← called by {Index.fmt(c)}")
    if graph:
        add("## Call graph around the evidence\n" + "\n".join(dict.fromkeys(graph)) + "\n")

    # 4) config hits (for configuration-type tickets)
    cfg_hits = idx.search(all_text, k=4, kinds=("config",))
    if cfg_hits:
        add("## Config file matches\n" + "\n".join(
            f"### {s['path']}:{s['start']}-{s['end']}\n```\n{idx.body(s, max_lines=15)}\n```" for s in cfg_hits[:2]) + "\n")

    # 5) history + team signals computed by the pipeline (past fixes, regressions, in-flight work, lessons)
    for title, key in (("Past similar tickets (with git facts)", "past_matches"),
                       ("Teammates' in-flight changes overlapping this area", "teammate_inflight"),
                       ("Team lessons for this component", "lessons")):
        items = extras.get(key) or []
        if items:
            add(f"## {title}\n" + "\n".join(f"- {i}" for i in items) + "\n")

    text = "".join(out)
    stats["est_tokens"] = estimate_tokens(text)
    return text, stats
