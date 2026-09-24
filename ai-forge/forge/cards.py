"""Teams cards via the shared outbox, and decisions back from Power Automate (PLAN §5.13).

The runner never talks to Teams directly: it writes outbox/<KEY>__<request_id>.json, the standard
SharePoint-triggered flow posts it and writes decisions/<KEY>__<request_id>.json with the clicker's
Teams identity. A decision counts only if its request is still pending and the responder is allowed."""
import json
import shutil
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import jsonschema

from . import config as C
from .shared import REQUEST_FILE, atomic_write, read_dir, read_json
from .signals import scrub
from .triage import add_lesson

SYSTEM_KEY = "FORGE-0"  # outbox key for messages not tied to a ticket (budget, runner alerts)
ALLOWED = {"approval": {"approve", "reject"}, "info": {"resolved", "analyze_as_code"},
           "duplicate": {"resolved", "analyze_as_code"}, "conflict": {"wait", "build_on", "proceed"}}
STALE_WORKING_DAYS = 5


def new_request_id() -> str:
    return uuid.uuid4().hex[:8]


def _schema(name: str) -> dict:
    return json.loads((C.ASSETS / "schemas" / f"{name}.schema.json").read_text(encoding="utf-8"))


def render(template: str, values: dict, drop: tuple[str, ...] = ()) -> dict:
    """Fill ${vars} in an Adaptive Card template (JSON-escaped) and remove optional containers by id."""
    text = (C.ASSETS / "cards" / f"{template}_card.json").read_text(encoding="utf-8")
    for k, v in values.items():
        text = text.replace("${" + k + "}", json.dumps("" if v is None else str(v), ensure_ascii=False)[1:-1])
    card = json.loads(text)
    card["body"] = [b for b in card["body"] if b.get("id") not in drop]
    return card


def ticket_url(cfg: C.Config, key: str) -> str:
    return f"{(cfg.team.get('jira') or {}).get('base_url', '').rstrip('/')}/browse/{key}"


def ticket_links(cfg: C.Config, keys: list[str]) -> str:
    return ", ".join(f"[{k}]({ticket_url(cfg, k)})" for k in keys)


def _assignee(cfg: C.Config) -> tuple[str, str]:
    return cfg.me.get("name") or cfg.user_id, cfg.me.get("email") or cfg.user_email


def _write_outbox(cfg: C.Config, rid: str, key: str, kind: str, wait: bool, card=None, text=None) -> None:
    msg = {"schema": 1, "request_id": rid, "ticket_key": key, "kind": "info" if kind == "duplicate" else kind,
           "wait_for_response": wait}
    if card is not None:
        msg["card"] = card
    if text is not None:
        msg["text"] = text
    jsonschema.validate(msg, _schema("outbox"))
    atomic_write(cfg.shared / "outbox" / f"{key}__{rid}.json", msg)


def notify(cfg: C.Config, store, key: str | None, text: str) -> str:
    """Plain Teams message (PR ready, revalidated, budget reached, ignored click...). No response expected."""
    rid, key = new_request_id(), key or SYSTEM_KEY
    _write_outbox(cfg, rid, key, "notify", False, text=f"[AI Forge · {cfg.user_id}] {text}")
    store.event(key, "card:notify", text[:200])
    return rid


def _steps(items) -> str:
    return "\n".join(f"{i}. {s}" for i, s in enumerate(items or [], 1)) or "-"


def send_approval(cfg: C.Config, store, g: dict, tokens: int, regression: dict | None, conflict: dict | None) -> str:
    keys = g["tickets"]
    name, email = _assignee(cfg)
    fix = g.get("proposed_fix") or {}
    values = {
        "title": f"{'Group of ' + str(len(keys)) + ': ' if len(keys) > 1 else ''}{scrub(g.get('_summary') or keys[0])}",
        "assigneeName": name, "assigneeEmail": email, "ticketLinks": ticket_links(cfg, keys),
        "regressionOf": (regression or {}).get("key", ""), "regressionNote": (regression or {}).get("note", ""),
        "conflictNote": (conflict or {}).get("note", ""), "classification": g.get("classification"),
        "risk": g.get("risk", "-"), "confidence": f"{g.get('confidence', 0):.0%}",
        "files": ", ".join(g.get("affected_files") or []) or "-", "tokens": f"{tokens:,}",
        "rootCause": g.get("root_cause", ""),
        "fixSteps": _steps(fix.get("steps")) + (f"\n\nTest: {fix['test_plan']}" if fix.get("test_plan") else ""),
        "ticketUrl": ticket_url(cfg, keys[0]),
    }
    drop = tuple(x for x, on in (("regression_banner", regression), ("conflict_banner", conflict)) if not on)
    rid = new_request_id()
    card = render("approval", {**values, "requestId": rid}, drop)
    return _post_with_id(cfg, store, rid, keys[0], "approval", card, {"tickets": keys, "group": g})


def send_info(cfg: C.Config, store, g: dict, tokens: int) -> str:
    keys = g["tickets"]
    res = g.get("non_code_resolution") or {}
    rid = new_request_id()
    card = render("info", {
        "title": scrub(g.get("_summary") or keys[0]), "ticketLinks": ticket_links(cfg, keys),
        "classification": g.get("classification"), "where": res.get("where", "-"), "owner": res.get("owner", "-"),
        "confidence": f"{g.get('confidence', 0):.0%}", "what": res.get("what") or g.get("root_cause", ""),
        "steps": _steps(res.get("steps")) + f"\n\nCost: {tokens:,} tokens", "ticketUrl": ticket_url(cfg, keys[0]),
        "requestId": rid})
    return _post_with_id(cfg, store, rid, keys[0], "info", card, {"tickets": keys, "group": g})


def send_duplicate(cfg: C.Config, store, t: dict, match: dict) -> str:
    a, f = match["analysis"], match["facts"]
    rid = new_request_id()
    card = render("info", {
        "title": f"{t['key']} looks like a duplicate of {match['key']}", "ticketLinks": ticket_links(cfg, [t["key"]]),
        "classification": "duplicate (no Copilot call)", "where": a.get("pr_url") or f"commit {f['fix_commit']}",
        "owner": a.get("owner", "-"), "confidence": "same error signature",
        "what": f"{match['key']}: {a.get('root_cause') or a.get('summary', '')}",
        "steps": _steps([f"Fix {f['fix_commit']} is present on {cfg.base_ref}.",
                         "Check the reporter's version/deployment includes it; if yes, click 'analyze'."]),
        "ticketUrl": ticket_url(cfg, t["key"]), "requestId": rid})
    write_analysis(cfg, t, {"classification": "duplicate", "duplicate_of": match["key"],
                            "root_cause": f"Same error signature as {match['key']}: {a.get('root_cause', '')}"[:600],
                            "confidence": 0.9, "status": "awaiting_decision", "agent": "rules", "model": "-",
                            "group_id": t.get("group_id") or t["key"], "tokens": {"estimated": 0}})
    return _post_with_id(cfg, store, rid, t["key"], "duplicate", card, {"tickets": [t["key"]], "duplicate_of": match["key"]})


def send_conflict(cfg: C.Config, store, key: str, c: dict, keys: list[str] | None = None) -> str:
    rid = new_request_id()
    card = render("conflict", {
        "ticket": key, "otherTicket": c["with"], "otherOwner": c.get("owner", "?"), "kind": c["kind"],
        "symbols": ", ".join(c.get("symbols") or [])[:300] or "-", "otherStatus": c.get("status", "?"),
        "recommendation": c.get("recommendation", "wait"), "note": c.get("note", ""), "requestId": rid})
    return _post_with_id(cfg, store, rid, key, "conflict", card, {"tickets": keys or [key], "conflict": c})


def _post_with_id(cfg, store, rid, key, kind, card, payload) -> str:
    _write_outbox(cfg, rid, key, kind, True, card=card)
    store.add_request(rid, key, kind, {**payload, "card": card})
    store.event(key, f"card:{kind}")
    return rid


# ---------------- analyses/<KEY>.json ----------------
def write_analysis(cfg: C.Config, t: dict, fields: dict) -> dict:
    path = cfg.shared / "analyses" / f"{t['key']}.json"
    old = read_json(path) or {}
    obj = {**old, "schema": 1, "ticket_key": t["key"], "summary": scrub(t.get("summary", "")),
           "component": t.get("component"), "sprint": t.get("sprint"), "owner": cfg.user_id,
           "error_signature": t.get("signature"), "base_commit": old.get("base_commit") or _base_commit(cfg),
           "analyzed_at": datetime.now().isoformat(timespec="seconds"), **fields}
    obj.setdefault("group_id", t["key"])
    jsonschema.validate(obj, _schema("analysis"))
    atomic_write(path, obj)
    return obj


def update_analysis(cfg: C.Config, key: str, **fields) -> None:
    path = cfg.shared / "analyses" / f"{key}.json"
    old = read_json(path)
    if old and old.get("owner") == cfg.user_id:
        atomic_write(path, {**old, **fields})


def _base_commit(cfg: C.Config) -> str:
    import subprocess
    r = subprocess.run(["git", "rev-parse", cfg.base_ref], cwd=cfg.repo_path, capture_output=True, text=True)
    return r.stdout.strip()[:12] or "unknown"


# ---------------- decisions ----------------
def accept(cfg: C.Config, t: dict, d: dict, pend: list[dict]) -> tuple[bool, str]:
    if not pend or pend[0]["ticket_key"] != d["ticket_key"]:
        return False, "card is no longer pending (stale or already answered)"
    who = (d.get("responder") or "").lower()
    if who not in {a.lower() for a in cfg.team.get("approvers") or []}:
        return False, f"{who or 'unknown'} is not an approver"
    allowed = {(cfg.me.get("email") or cfg.user_email).lower(), (cfg.team.get("lead") or "").lower()}
    if who not in allowed:
        return False, f"{who} is neither the assignee nor the lead"
    if d["action"] not in ALLOWED.get(pend[0]["kind"], set()):
        return False, f"action '{d['action']}' does not fit a {pend[0]['kind']} card"
    return True, ""


def process_decisions(cfg: C.Config, store, log=print) -> list[tuple[str, str]]:
    folder = cfg.shared / "decisions"
    archive = C.HOME / "decisions-archive"
    archive.mkdir(parents=True, exist_ok=True)
    schema = _schema("decision")
    applied = []
    for name, d in read_dir(folder, REQUEST_FILE).items():
        t = store.ticket(d.get("ticket_key", ""))
        if not t:
            continue  # a teammate's ticket: their runner handles it
        src = folder / f"{name}.json"
        try:
            jsonschema.validate(d, schema)
        except jsonschema.ValidationError as e:
            notify(cfg, store, t["key"], f"Ignored malformed decision file {name}: {e.message[:120]}")
            _archive(src, archive)
            continue
        pend = store.pending(d["request_id"])
        ok, why = accept(cfg, t, d, pend)
        if not ok:
            store.event(t["key"], "decision_ignored", why)
            notify(cfg, store, t["key"], f"Ignored '{d['action']}' on {t['key']} from {d.get('responder')}: {why}")
            _archive(src, archive)
            continue
        apply(cfg, store, t, d, pend[0])
        store.close_request(d["request_id"])
        _archive(src, archive)
        applied.append((t["key"], d["action"]))
        log(f"decision {t['key']}: {d['action']} by {d['responder']}")
    return applied


def _abandon_inflight(cfg: C.Config, key: str) -> None:
    path = cfg.shared / "inflight" / f"{key}.json"
    old = read_json(path)
    if old and old.get("owner") == cfg.user_id:
        atomic_write(path, {**old, "status": "abandoned", "updated": datetime.now().isoformat(timespec="seconds")})


def _archive(src: Path, archive: Path) -> None:
    try:
        shutil.move(str(src), str(archive / src.name))
    except OSError:
        src.unlink(missing_ok=True)


def apply(cfg: C.Config, store, t: dict, d: dict, pend: dict) -> None:
    action, note = d["action"], (d.get("comment") or "").strip()
    keys = pend["payload"].get("tickets") or [t["key"]]
    by = d["responder"]
    if action == "approve":
        store.set_status(keys, "approved", f"approved by {by}", reviewer_note=note or None)
        for k in keys:
            update_analysis(cfg, k, status="approved")
    elif action == "reject":
        store.set_status(keys, "rejected", f"rejected by {by}: {note}")
        _abandon_inflight(cfg, keys[0])
        if note:
            add_lesson(cfg, t.get("component"), t["key"], note)
        for k in keys:
            update_analysis(cfg, k, status="rejected")
    elif action == "resolved":
        store.set_status(keys, "resolved", f"resolved by {by}")
        for k in keys:
            update_analysis(cfg, k, status="resolved")
    elif action == "analyze_as_code":
        store.set_status(keys, "ready", f"{by}: analyze as code", route="forge-analyst",
                         route_reason="human asked for code analysis", group_id=None)
        if note:
            add_lesson(cfg, t.get("component"), t["key"], f"misrouted as non-code: {note}")
    elif action in ("wait", "build_on", "proceed"):
        c = pend["payload"].get("conflict") or {}
        if action == "wait":
            store.set_status(keys, "conflict_wait", f"{by}: wait for {c.get('with')}", blocked_on=c.get("with"))
        elif action == "build_on":
            store.set_status(keys, "approved", f"{by}: build on {c.get('branch')}", base_branch=c.get("branch"),
                             blocked_on=f"ack:{c.get('with')}")
        else:
            store.set_status(keys, "approved", f"{by}: proceed despite {c.get('with')}", blocked_on=f"ack:{c.get('with')}")


# ---------------- stale cards ----------------
def working_days_between(a: date, b: date) -> int:
    days, d = 0, a
    while d < b:
        d += timedelta(days=1)
        if d.isoweekday() <= 5:
            days += 1
    return days


def repost_stale(cfg: C.Config, store, today: date | None = None) -> list[str]:
    today = today or date.today()
    out = []
    for p in store.pending():
        if working_days_between(datetime.fromisoformat(p["created"]).date(), today) < STALE_WORKING_DAYS:
            continue
        card = p["payload"].get("card")
        store.close_request(p["request_id"])  # a late click on the old card is now ignored
        if card:
            rid = new_request_id()
            card = json.loads(json.dumps(card).replace(p["request_id"], rid))
            _post_with_id(cfg, store, rid, p["ticket_key"], p["kind"], card,
                          {k: v for k, v in p["payload"].items() if k != "card"})
            out.append(p["ticket_key"])
    return out
