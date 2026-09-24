"""Analysis of ticket groups by Copilot agents (PLAN §5.11): one call per group in the normal path."""
import json
import traceback
from datetime import date, datetime

import jsonschema

from . import cards
from . import config as C
from . import conflicts, gitwt, triage
from .context_pack import build as build_pack
from .copilot import BudgetExceeded, extract_json, run_agent
from .index.store import Index

HIGH_PRIORITIES = {"high", "highest", "critical", "blocker", "urgent", "p1", "p0"}
PROMPT = "Analyze the ticket group in .forge/job.json."


def pack_ticket(t: dict) -> dict:
    rep = "\n".join(f"[reporter comment {c.get('created', '')[:10]}] {c['body']}"
                    for c in t.get("comments", []) if c.get("by_reporter"))
    return {"key": t["key"], "summary": t.get("summary", ""), "component": t.get("component"),
            "priority": t.get("priority"), "description": f"{t.get('description', '')}\n{rep}".strip(),
            "attachments": [(a["name"], a.get("text", "")) for a in t.get("attachments", [])]}


def budget_notice(cfg: C.Config, store, err: Exception) -> None:
    """Tell the lead once per day; queued tickets simply continue tomorrow."""
    if store.get_state("budget_notified") != date.today().isoformat():
        store.set_state("budget_notified", date.today().isoformat())
        cards.notify(cfg, store, None, f"{cfg.user_id}: {err}. Remaining tickets continue tomorrow.")


def _validate(out: dict) -> str | None:
    schema = json.loads((C.ASSETS / "schemas" / "agent_output.schema.json").read_text(encoding="utf-8"))
    try:
        jsonschema.validate(out, schema)
        return None
    except jsonschema.ValidationError as e:
        return e.message[:200]


def _call(cfg, agent, wt, keys, pack_len, model=None) -> tuple[dict | None, object, str | None]:
    res = run_agent(cfg, agent, PROMPT, wt, keys, model=model, context_chars=pack_len)
    try:
        out = extract_json(res.stdout)
    except ValueError as e:
        return None, res, str(e)
    return out, res, _validate(out)


def analyze_groups(cfg: C.Config, store, idx: Index, jira, log=print) -> dict:
    stats = {"groups": 0, "calls": 0, "tickets": 0, "failed": 0}
    for g in triage.group_ready(cfg, store, idx):
        keys = [t["key"] for t in g["tickets"]]
        try:
            _analyze_one(cfg, store, idx, jira, g, stats, log)
        except BudgetExceeded as e:
            store.set_status(keys, "ready", "budget reached")
            budget_notice(cfg, store, e)
            break
        except Exception as e:  # fail once and tell a human; a retry loop would cost tokens every cycle
            log(f"analysis {keys} failed:\n{traceback.format_exc()}")
            store.set_status(keys, "analyze_failed", f"{e.__class__.__name__}: {e}"[:300])
            cards.notify(cfg, store, keys[0], f"⚠ Analysis of {', '.join(keys)} failed: {e.__class__.__name__}: {str(e)[:200]}")
            stats["failed"] += len(keys)
    return stats


def _analyze_one(cfg, store, idx, jira, g, stats, log) -> None:
    tickets, keys, gid = g["tickets"], [t["key"] for t in g["tickets"]], g["group_id"]
    text = "\n".join(triage.full_text(t) for t in tickets)
    matches = triage.past_matches(cfg, store, idx, tickets)
    predicted = sorted(triage.predicted_symbols(idx, text))
    overlaps = conflicts.detect(cfg, idx, keys[0], predicted, [])
    comps = {t.get("component") for t in tickets if t.get("component")}
    extras = {"past_matches": [m["line"] for m in matches], "teammate_inflight": conflicts.lines(overlaps),
              "lessons": [l for c in comps for l in triage.lessons(cfg, c)]}
    pack, pstats = build_pack(idx, [pack_ticket(t) for t in tickets], extras,
                              budget_tokens=cfg.threshold("context_budget_tokens", 7000))
    job = {"schema": 1, "group_id": gid, "route": g["route"], "grouping_reasons": g["reasons"],
           "tickets": [{"key": t["key"], "summary": t.get("summary"), "component": t.get("component"),
                        "priority": t.get("priority"), "sprint": t.get("sprint"), "error_signature": t.get("signature"),
                        "route_reason": t.get("route_reason")} for t in tickets],
           "past_matches": [{"key": m["key"], "fact": m["line"], "possible_regression": m["regression"]} for m in matches],
           "teammate_inflight": overlaps, "lessons": extras["lessons"], "base_ref": cfg.base_ref}

    store.set_status(keys, "analyzing", f"group {gid} via {g['route']}")
    wt = gitwt.worktree_add(cfg.repo_path, cfg.worktree_root / f"analyze-{gid}", cfg.base_ref)
    try:
        (wt / ".forge" / "job.json").write_text(json.dumps(job, indent=2), encoding="utf-8")
        (wt / ".forge" / "context.md").write_text(pack, encoding="utf-8")
        if cfg.repo_map.exists():
            (wt / ".forge" / "REPO_MAP.md").write_text(cfg.repo_map.read_text(encoding="utf-8"), encoding="utf-8")
        plen = len(pack) + len(json.dumps(job))

        agent = g["route"]
        model = cfg.model_for(agent)
        out, res, err = _call(cfg, agent, wt, keys, plen)
        calls, tokens = 1, res.tokens
        # forge-config found real code logic → one analyst run
        if not err and agent == "forge-config" and any(x.get("escalate_to_analyst") for x in out["groups"]):
            log(f"{gid}: forge-config escalated to forge-analyst")
            agent, model = "forge-analyst", cfg.model_for("forge-analyst")
            out, res, err = _call(cfg, agent, wt, keys, plen)
            calls, tokens = calls + 1, tokens + res.tokens
        # low confidence on an urgent ticket → one rerun on the escalation model
        esc = (cfg.team.get("models") or {}).get("escalation", "")
        urgent = any((t.get("priority") or "").lower() in HIGH_PRIORITIES for t in tickets)
        if (not err and urgent and esc and not esc.startswith("REPLACE") and agent == "forge-analyst"
                and min(x["confidence"] for x in out["groups"]) < cfg.threshold("escalate_below_confidence", 0.5)):
            log(f"{gid}: low confidence on urgent ticket, rerun on {esc}")
            out2, res2, err2 = _call(cfg, agent, wt, keys, plen, model=esc)
            calls, tokens = calls + 1, tokens + res2.tokens
            if not err2:
                out, res, model = out2, res2, esc
        stats["calls"] += calls
        stats["groups"] += 1
        tok_info = {"input": res.usage.get("input"), "output": res.usage.get("output"), "estimated": tokens,
                    "calls": calls, "pack_tokens": pstats["est_tokens"]}
        store.save_group_result(gid, out, agent, model, tok_info)
        if err:
            store.set_status(keys, "analyze_failed", f"invalid agent output: {err}", tokens=tokens // len(keys))
            cards.notify(cfg, store, keys[0], f"Analysis of {', '.join(keys)} returned invalid output ({err}). "
                                              "Not retried (saves tokens); check `forge stats` / the ledger.")
            stats["failed"] += len(keys)
            return
        _publish(cfg, store, idx, jira, tickets, out, agent, model, tok_info, matches, log)
        stats["tickets"] += len(keys)
    finally:
        gitwt.worktree_remove(cfg.repo_path, wt)


def _publish(cfg, store, idx, jira, tickets, out, agent, model, tok, matches, log) -> None:
    by_key = {t["key"]: t for t in tickets}
    seen: set[str] = set()
    share = max(1, tok["estimated"] // max(1, len(tickets)))
    for entry in out["groups"]:
        keys = [k for k in entry["tickets"] if k in by_key and k not in seen]
        if not keys:
            continue
        seen |= set(keys)
        entry["tickets"] = keys
        first = by_key[keys[0]]
        entry["_summary"] = first.get("summary")
        cls, conf = entry["classification"], float(entry.get("confidence", 0))
        gid = triage.group_id(keys)
        common = {"group_id": gid, "group_tickets": keys, "classification": cls, "root_cause": entry["root_cause"],
                  "evidence": entry.get("evidence") or [], "affected_symbols": entry.get("affected_symbols") or [],
                  "affected_files": entry.get("affected_files") or [], "proposed_fix": entry.get("proposed_fix"),
                  "non_code_resolution": entry.get("non_code_resolution"), "regression_of": entry.get("regression_of"),
                  "conflicts": entry.get("conflicts") or [], "risk": entry.get("risk") or "medium",
                  "confidence": conf, "agent": agent, "model": model,
                  "tokens": {"input": tok["input"], "output": tok["output"], "estimated": tok["estimated"]}}
        now = datetime.now().isoformat(timespec="seconds")

        if cls == "code_bug" and conf >= cfg.threshold("min_confidence_for_fix_card", 0.5):
            reg = next((m for m in matches if m["regression"]), None)
            regression = ({"key": entry["regression_of"], "note": "flagged by the analyst"} if entry.get("regression_of")
                          else {"key": reg["key"], "note": reg["line"]} if reg else None)
            detected = conflicts.detect(cfg, idx, keys[0], common["affected_symbols"], common["affected_files"])
            blocking = [c for c in detected if c["kind"] in ("direct", "dependency")] or entry.get("conflicts") or []
            conflict = {"note": "; ".join(f"{c['with']} ({c.get('owner')}): {c.get('note') or c.get('kind')}"
                                          for c in blocking)[:400]} if blocking else None
            for k in keys:
                cards.write_analysis(cfg, by_key[k], {**common, "status": "awaiting_decision"})
            store.set_status(keys, "awaiting_decision", f"{cls} {conf:.2f}", plan=entry, analyzed_at=now,
                             tokens=share, group_id=gid)
            cards.send_approval(cfg, store, entry, tok["estimated"], regression, conflict)
            conflicts.write_inflight(cfg, keys[0], status="planned", branch=None, base_commit=cards._base_commit(cfg),
                                     planned_symbols=common["affected_symbols"],
                                     changed_files=common["affected_files"])
        elif cls in ("code_bug", "needs_info"):
            qs = entry.get("questions_for_reporter") or []
            for k in keys:
                cards.write_analysis(cfg, by_key[k], {**common, "classification": "needs_info", "status": "needs_info"})
                if (cfg.team.get("jira") or {}).get("post_comments", True):
                    jira.add_comment(k, triage.needs_info_comment(cfg, qs))
            store.set_status(keys, "needs_info", f"{cls} confidence {conf:.2f}", analyzed_at=now, tokens=share,
                             group_id=gid)
        else:
            for k in keys:
                cards.write_analysis(cfg, by_key[k], {**common, "status": "info_sent",
                                                      "duplicate_of": entry.get("regression_of") if cls == "duplicate" else None})
            store.set_status(keys, "info_sent", cls, plan=entry, analyzed_at=now, tokens=share, group_id=gid)
            cards.send_info(cfg, store, entry, tok["estimated"])
        store.event(keys[0], f"analyzed:{cls}", f"{len(keys)} tickets, {tok['estimated']} tokens")
        if len(keys) > 1:
            for k in keys[1:]:
                store.event(k, "grouped", keys[0])
        log(f"analysis {', '.join(keys)}: {cls} ({conf:.2f})")
    missing = [k for k in by_key if k not in seen]
    if missing:
        store.set_status(missing, "analyze_failed", "agent output did not cover these tickets")

