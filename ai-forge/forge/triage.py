"""Free, local decisions before any Copilot call (PLAN §5.7–5.9):
router rules, grouping of related tickets, and past-ticket matches with git facts."""
import hashlib
import math
import re
import subprocess
from collections import Counter
from datetime import datetime, timedelta

from . import config as C
from .index.store import Index
from .shared import TICKET_FILE, read_dir
from .signals import parse_frames, subwords

NON_CODE = re.compile(
    r"(?i)\b(after (?:the )?deploy(?:ment)?|in (?:the )?(?:env|environment)|certificate|cert(?:ificate)? expired|"
    r"ssl|tls|permission|access denied|unauthori[sz]ed|forbidden|\b40[13]\b|timeout|timed out|connection refused|"
    r"dns|proxy|firewall|how (?:do|can|to) i|where can i|is it possible|config(?:uration)?|feature flag|"
    r"env(?:ironment)? var|quota|disk full|out of space|license)")
MIN_DESCRIPTION = 80
DEFAULT_NEEDS_INFO = ("Thanks for the report. To analyse this we need a bit more information:\n"
                      "1. Exact steps to reproduce\n2. Expected vs actual behaviour\n"
                      "3. Logs or the full error message / stack trace (as a text attachment)\n"
                      "4. Environment and version where it happens\n(posted automatically by AI Forge)")


# ---------------- text helpers ----------------
def full_text(t: dict) -> str:
    rep = "\n".join(c["body"] for c in t.get("comments", []) if c.get("by_reporter"))
    atts = "\n".join(a.get("text", "") for a in t.get("attachments", []))
    return f"{t.get('summary', '')}\n{t.get('description', '')}\n{rep}\n{atts}"


def cosine(a: str, b: str) -> float:
    va, vb = Counter(subwords(a, limit=300)), Counter(subwords(b, limit=300))
    if not va or not vb:
        return 0.0
    dot = sum(va[t] * vb[t] for t in va.keys() & vb.keys())
    return dot / (math.sqrt(sum(v * v for v in va.values())) * math.sqrt(sum(v * v for v in vb.values())))


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def group_id(keys: list[str]) -> str:
    return "G-" + hashlib.sha1(",".join(sorted(keys)).encode()).hexdigest()[:8]


def resolved_frames(idx: Index, text: str) -> list:
    out = []
    for f in parse_frames(text):
        s = idx.symbol_at(f.path, f.line)
        if s:
            out.append(s)
    return out


def predicted_symbols(idx: Index, text: str) -> set[str]:
    syms = resolved_frames(idx, text) + idx.search(text, k=5, kinds=("function", "class"))
    return {f"{s['path']}::{s['qualname']}" for s in syms}


# ---------------- git facts (§5.9) ----------------
def _git(repo: str, *args) -> str:
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, errors="replace")
    return r.stdout.strip() if r.returncode == 0 else ""


def git_facts(repo: str, base_ref: str, key: str, analysis: dict | None, idx: Index | None = None) -> dict:
    """Is the fix for `key` still on base_ref, was it reverted, was its code modified since?"""
    a = analysis or {}
    sha = a.get("fix_commit") or next(
        (line.split(" ", 1)[0] for line in _git(repo, "log", base_ref, "-E", "-n", "20", "--format=%H %s",
                                                f"--grep=(^|[^A-Za-z0-9-]){key}([^0-9]|$)").splitlines()
         if not line.split(" ", 1)[-1].startswith('Revert "')), "")  # the revert also mentions the key
    facts = {"fix_commit": sha[:10] if sha else None, "present": False, "reverted_by": None, "modified_since": []}
    if not sha:
        return facts
    facts["present"] = subprocess.run(["git", "merge-base", "--is-ancestor", sha, base_ref], cwd=repo,
                                      capture_output=True).returncode == 0
    rev = _git(repo, "log", base_ref, f"--grep=This reverts commit {sha}", "--format=%h", "-n", "1")
    if not rev and len(sha) >= 7:
        rev = _git(repo, "log", base_ref, f"--grep=This reverts commit {sha[:7]}", "--format=%h", "-n", "1")
    facts["reverted_by"] = rev or None
    if not facts["present"]:
        return facts
    files = a.get("affected_files") or _git(repo, "show", "--name-only", "--format=", sha).splitlines()
    ranges = []
    for sym in a.get("affected_symbols") or []:
        path, _, qual = sym.partition("::")
        if idx and qual:
            row = idx.con.execute("SELECT start, end FROM symbols WHERE path=? AND qualname=? LIMIT 1",
                                  (path, qual)).fetchone()
            if row:
                ranges.append((path, qual, row["start"], row["end"]))
    seen = set()
    for path, qual, s, e in ranges:  # narrowed to the fixed symbol's current line range
        out = _git(repo, "log", "--format=@@%h|%an", f"-L{s},{e}:{path}", f"{sha}..{base_ref}")
        for line in out.splitlines():
            if line.startswith("@@") and line[2:] not in seen:
                seen.add(line[2:])
                h, _, who = line[2:].partition("|")
                facts["modified_since"].append({"commit": h, "author": who, "where": f"{path}::{qual}"})
    if not ranges and files:
        out = _git(repo, "log", "--format=%h|%an", f"{sha}..{base_ref}", "--", *[f for f in files if f])
        for line in out.splitlines()[:5]:
            h, _, who = line.partition("|")
            facts["modified_since"].append({"commit": h, "author": who, "where": ", ".join(files[:3])})
    return facts


def fact_line(key: str, a: dict, facts: dict) -> str:
    head = f"{key} ({a.get('sprint') or 'sprint ?'}, PR {a.get('pr_url') or '-'})"
    if not facts["fix_commit"]:
        return f"{head}: no fix commit found on base ({a.get('classification', 'no analysis')})"
    parts = [f"fix {facts['fix_commit']} {'present' if facts['present'] else 'NOT on base'}"]
    if facts["reverted_by"]:
        parts.append(f"REVERTED in {facts['reverted_by']}")
    for m in facts["modified_since"][:3]:
        parts.append(f"{m['where']} modified since in {m['commit']} by {m['author']}")
    return f"{head}: " + "; ".join(parts)


def is_regression(facts: dict) -> bool:
    return bool(facts.get("reverted_by") or (facts.get("present") and facts.get("modified_since")))


# ---------------- past matches (§5.9) ----------------
def past_matches(cfg: C.Config, store, idx: Index, tickets: list[dict], k: int = 3) -> list[dict]:
    own = {t["key"] for t in tickets}
    text = "\n".join(full_text(t) for t in tickets)
    sigs = {t.get("signature") for t in tickets if t.get("signature")}
    cands: dict[str, dict] = {}
    for key, a in read_dir(cfg.shared / "analyses", TICKET_FILE).items():
        if key in own:
            continue
        score = 1.0 if a.get("error_signature") in sigs else cosine(text, f"{a.get('summary', '')} {a.get('root_cause', '')}")
        cands[key] = {"key": key, "score": score, "analysis": a}
    for h in store.history():
        if h["key"] in own:
            continue
        score = 1.0 if h.get("signature") in sigs else cosine(text, f"{h['summary']} {h['description']}")
        if h["key"] not in cands or cands[h["key"]]["score"] < score:
            prev = (cands.get(h["key"]) or {}).get("analysis")
            cands[h["key"]] = {"key": h["key"], "score": score,
                               "analysis": prev or {"summary": h["summary"], "sprint": h.get("sprint")}}
    top = sorted((c for c in cands.values() if c["score"] >= 0.3), key=lambda c: -c["score"])[:k]
    for c in top:
        c["facts"] = git_facts(cfg.repo_path, cfg.base_ref, c["key"], c["analysis"], idx)
        c["line"] = fact_line(c["key"], c["analysis"], c["facts"])
        c["regression"] = is_regression(c["facts"])
        c["same_signature"] = c["score"] == 1.0
    return top


def lessons(cfg: C.Config, component: str | None, n: int = 3) -> list[str]:
    """Most recent lines from lessons/<component>.md (lead) and lessons/<component>__<user>.md (runners)."""
    if not component:
        return []
    safe = re.sub(r"[^\w-]", "_", component.lower())
    lines = []
    for p in (cfg.shared / "lessons").glob(f"{safe}*.md"):
        if p.stem == safe or p.stem.startswith(f"{safe}__"):
            lines += [l[2:].strip() for l in p.read_text(encoding="utf-8", errors="replace").splitlines()
                      if l.startswith("- ")]
    return sorted(lines)[-n:]


def add_lesson(cfg: C.Config, component: str | None, key: str, text: str) -> None:
    """One writer per file: each runner appends to its own lessons/<component>__<user>.md."""
    safe = re.sub(r"[^\w-]", "_", (component or "general").lower())
    p = cfg.shared / "lessons" / f"{safe}__{cfg.user_id}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    line = " ".join((text or "").split())[:300]
    with p.open("a", encoding="utf-8") as fh:
        fh.write(f"- {datetime.now():%Y-%m-%d} {key} ({cfg.user_id}): {line}\n")


# ---------------- router (§5.7) ----------------
def route(cfg: C.Config, idx: Index, t: dict, matches: list[dict]) -> tuple[str, str, str]:
    """Returns (status, route, reason). status: skipped | needs_info | duplicate | ready."""
    j = cfg.team.get("jira") or {}
    types = {x.lower() for x in j.get("support_issue_types") or []}
    labels = {x.lower() for x in j.get("support_labels") or []}
    if (types or labels) and (t.get("type") or "").lower() not in types and not labels & {l.lower() for l in t.get("labels", [])}:
        return "skipped", "", f"issue type '{t.get('type')}' is not a support type"

    text = full_text(t)
    body = (t.get("description") or "").strip() + "".join(c["body"] for c in t.get("comments", []) if c.get("by_reporter"))
    has_att = any(a.get("text") for a in t.get("attachments", []))
    frames = parse_frames(text)
    if len(body) < MIN_DESCRIPTION and not has_att and not frames:
        return "needs_info", "", f"description has {len(body)} chars, no attachments, no stack trace"

    for m in matches:
        if m.get("same_signature") and m["facts"]["present"] and not m["regression"]:
            return "duplicate", "", f"same error signature as {m['key']}, fix {m['facts']['fix_commit']} is on base"

    app_frames = resolved_frames(idx, text)
    words = sorted({w.lower() for w in NON_CODE.findall(text)})
    cfg_hits = idx.search(text, k=3, kinds=("config",)) if words else []
    if not app_frames and (words or cfg_hits) and not any(m.get("regression") for m in matches):
        why = f"non-code signals: {', '.join(words[:5]) or '-'}; config matches: {len(cfg_hits)}; no app frames"
        return "ready", "forge-config", why
    return "ready", "forge-analyst", f"{len(app_frames)} app frames resolved" if app_frames else "default route"


def ask_reporter(cfg: C.Config, store, jira, key: str, questions: list[str] | None = None) -> None:
    """Post the needs-info questions as a Jira comment, or (read-only Jira, `post_comments: false`)
    send them to the assignee in Teams to forward to the reporter."""
    text = needs_info_comment(cfg, questions)
    if (cfg.team.get("jira") or {}).get("post_comments", False):
        jira.add_comment(key, text)
    else:
        from .cards import notify  # local import: cards depends on triage helpers
        notify(cfg, store, key, f"{key} needs more information from the reporter. Suggested reply:\n{text}")


def needs_info_comment(cfg: C.Config, questions: list[str] | None = None) -> str:
    base = (cfg.team.get("jira") or {}).get("needs_info_comment") or DEFAULT_NEEDS_INFO
    if questions:
        base = "Thanks for the report. To find the cause we need:\n" + "\n".join(
            f"{i}. {q}" for i, q in enumerate(questions, 1)) + "\n(posted automatically by AI Forge)"
    return base


def preclassify(cfg: C.Config, store, idx: Index, jira, log=print) -> dict:
    """new → cooling → (after cooldown) rules → skipped | needs_info | duplicate | ready(route)."""
    stats = Counter()
    for t in store.tickets("new"):
        store.set_status(t["key"], "cooling")
    cooldown = timedelta(minutes=cfg.threshold("new_ticket_cooldown_minutes", 10))
    for t in store.tickets("cooling"):
        if datetime.fromisoformat(t["status_changed"]) > datetime.now() - cooldown:
            continue
        matches = past_matches(cfg, store, idx, [t])
        status, rt, reason = route(cfg, idx, t, matches)
        stats[status if status != "ready" else rt] += 1
        if status != "ready":
            store.event(t["key"], f"rule:{status}", reason)  # handled with zero Copilot calls
        if status == "needs_info":
            ask_reporter(cfg, store, jira, t["key"])
            store.set_status(t["key"], "needs_info", reason, analyzed_at=datetime.now().isoformat(timespec="seconds"))
        elif status == "duplicate":
            from .cards import send_duplicate  # local import: cards depends on triage helpers
            store.set_status(t["key"], "duplicate", reason, analyzed_at=datetime.now().isoformat(timespec="seconds"))
            send_duplicate(cfg, store, t, next(m for m in matches if m.get("same_signature")))
        else:
            store.set_status(t["key"], status, reason, route=rt or None, route_reason=reason)
        log(f"triage {t['key']}: {status} {rt} ({reason})")
    return dict(stats)


# ---------------- grouping (§5.8) ----------------
def group_ready(cfg: C.Config, store, idx: Index) -> list[dict]:
    """Union-find over my ready tickets. Returns [{group_id, tickets, route, reasons}]."""
    ts = store.tickets("ready")
    if not ts:
        return []
    cap = cfg.threshold("max_group_size", 4)
    sim_t, sym_t = cfg.threshold("group_similarity", 0.8), cfg.threshold("group_symbol_jaccard", 0.5)
    parent = {t["key"]: t["key"] for t in ts}
    size = {t["key"]: 1 for t in ts}
    reasons: dict[str, list[str]] = {t["key"]: [] for t in ts}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b, why):
        ra, rb = find(a), find(b)
        if ra == rb or size[ra] + size[rb] > cap:
            return
        parent[rb] = ra
        size[ra] += size[rb]
        reasons[ra] += reasons.pop(rb) + [f"{a}+{b}: {why}"]

    texts = {t["key"]: full_text(t) for t in ts}
    syms = {t["key"]: predicted_symbols(idx, texts[t["key"]]) for t in ts}
    for i, a in enumerate(ts):
        for b in ts[i + 1:]:
            if a.get("signature") and a["signature"] == b.get("signature"):
                union(a["key"], b["key"], f"same error signature {a['signature']}")
            elif (a.get("component") == b.get("component")
                  and (s := cosine(texts[a["key"]], texts[b["key"]])) >= sim_t):
                union(a["key"], b["key"], f"text similarity {s:.2f}, component {a.get('component')}")
            elif (j := jaccard(syms[a["key"]], syms[b["key"]])) >= sym_t:
                union(a["key"], b["key"], f"predicted-symbol overlap {j:.2f}")

    by_root: dict[str, list[dict]] = {}
    for t in ts:
        by_root.setdefault(find(t["key"]), []).append(t)
    groups = []
    for root, members in by_root.items():
        keys = sorted(m["key"] for m in members)
        rt = "forge-analyst" if any(m.get("route") != "forge-config" for m in members) else "forge-config"
        gid = group_id(keys)
        store.save_group(gid, keys, rt, reasons.get(root, []))
        for k in keys:
            store.update(k, group_id=gid)
        groups.append({"group_id": gid, "tickets": members, "route": rt, "reasons": reasons.get(root, [])})
    return groups
