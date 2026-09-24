"""Metrics (PLAN §5.16): runner counts, the savings report for management, and the baseline experiment.

All numbers come from the local event log and token ledger. Time savings are *estimates* driven by
the assumptions in team.json → "savings"; the report prints those assumptions next to the numbers."""
import html
import json
import re
import statistics
from datetime import date, datetime, timedelta
from pathlib import Path

from . import config as C
from .copilot import _ledger
from .shared import read_dir

DEFAULT_SAVINGS = {
    "minutes_triage_per_ticket": 15,       # read ticket, ask for info, classify, find duplicates
    "minutes_root_cause_per_code_bug": 90,  # locate the failing code path by hand
    "minutes_resolution_per_non_code": 30,  # figure out config/env/how-to answer
    "minutes_fix_per_pr": 60,               # write failing test + fix + PR description
    "engineer_hourly_cost": 0,              # optional; 0 hides the money line
    "currency": "EUR",
    "baseline_tokens_per_ticket": 0,        # fill from `forge baseline` results (0 = not measured yet)
}


def _week_start() -> str:
    d = date.today()
    return (d - timedelta(days=d.weekday())).isoformat()


def _q(store, sql: str, *args) -> int:
    return int(store.con.execute(sql, args).fetchone()[0] or 0)


def counts(store, since: str = "") -> dict:
    """Outcome counts (distinct tickets). since='' → all time."""
    def ev(pattern):
        return _q(store, "SELECT COUNT(DISTINCT key) FROM events WHERE event LIKE ? AND ts >= ?", pattern, since)
    first = ev("analyzed:%")
    grouped = ev("grouped")
    non_code = sum(ev(f"analyzed:{c}") for c in ("configuration", "environment", "data_issue", "user_error", "duplicate"))
    con = _ledger()
    day = since[:10] if since else ""
    tok = con.execute("SELECT COUNT(*), COALESCE(SUM(COALESCE(input_tokens, est_input_tokens) + COALESCE(output_tokens,0)),0)"
                      " FROM calls WHERE day >= ? AND agent != 'baseline'", (day,)).fetchone()
    return {
        "analyzed": first + grouped, "analysis_calls_saved_by_grouping": grouped, "grouped": grouped,
        "info_only": non_code, "duplicates": ev("rule:duplicate"), "skipped_by_rules": ev("rule:skipped"),
        "needs_info_by_rules": ev("rule:needs_info"), "approval_cards": ev("card:approval"),
        "approved": ev("status:approved"), "rejected": ev("status:rejected"),
        "prs": ev("pr") + ev("pr_unverified"), "prs_test_verified": ev("pr"), "merged": ev("status:merged"),
        "copilot_calls": int(tok[0]), "tokens": int(tok[1]),
    }


def heartbeat_counts(store) -> dict:
    return {"week": counts(store, _week_start()), "total": counts(store)}


def savings(c: dict, a: dict) -> dict:
    """Estimated engineer minutes saved, from outcome counts and the team's assumptions."""
    code = c["approval_cards"]
    minutes = (
        (c["analyzed"] + c["duplicates"] + c["needs_info_by_rules"] + c["skipped_by_rules"]) * a["minutes_triage_per_ticket"]
        + code * a["minutes_root_cause_per_code_bug"]
        + c["info_only"] * a["minutes_resolution_per_non_code"]
        + c["prs"] * a["minutes_fix_per_pr"])
    calls_avoided = c["duplicates"] + c["needs_info_by_rules"] + c["skipped_by_rules"] + c["grouped"]
    handled = c["analyzed"] + c["duplicates"] + c["needs_info_by_rules"]
    per_ticket = c["tokens"] / max(1, handled)
    out = {"hours_saved": round(minutes / 60, 1), "copilot_calls_avoided": calls_avoided,
           "tokens_per_ticket": round(per_ticket), "tickets_handled": handled}
    if a.get("baseline_tokens_per_ticket"):
        out["token_reduction_vs_baseline"] = f"{1 - per_ticket / a['baseline_tokens_per_ticket']:.0%}"
    if a.get("engineer_hourly_cost"):
        out["cost_saved"] = f"{a['currency']} {minutes / 60 * a['engineer_hourly_cost']:,.0f}"
    return out


def time_to_analysis(store) -> float | None:
    rows = store.con.execute("SELECT first_seen, analyzed_at FROM tickets WHERE analyzed_at IS NOT NULL"
                             " AND first_seen IS NOT NULL").fetchall()
    mins = [(datetime.fromisoformat(r[1]) - datetime.fromisoformat(r[0])).total_seconds() / 60 for r in rows]
    return round(statistics.median(mins), 1) if mins else None


# ---------------- report ----------------
def report(cfg: C.Config, store, team: bool = False, out: Path | None = None) -> Path:
    a = {**DEFAULT_SAVINGS, **(cfg.team.get("savings") or {})}
    if team:
        people = {k: v.get("counts", {}).get("total", {}) for k, v in
                  read_dir(cfg.shared / "runners", re.compile(r"^[\w.-]+\.json$")).items()}
        people = {k: v for k, v in people.items() if v}
    else:
        people = {cfg.user_id: counts(store)}
    keys = sorted({k for v in people.values() for k in v})
    total = {k: sum(v.get(k, 0) for v in people.values()) for k in keys}
    s = savings({**_zero(), **total}, a)
    week = counts(store, _week_start()) if not team else None
    tta = time_to_analysis(store) if not team else None
    out = out or (C.HOME / "reports" / f"forge-report-{'team' if team else cfg.user_id}-{date.today()}.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_html(cfg, people, total, s, a, week, tta, team), encoding="utf-8")
    return out


def _zero() -> dict:
    return {k: 0 for k in ("analyzed", "grouped", "info_only", "duplicates", "skipped_by_rules", "needs_info_by_rules",
                           "approval_cards", "approved", "rejected", "prs", "prs_test_verified", "merged",
                           "copilot_calls", "tokens", "analysis_calls_saved_by_grouping")}


def _html(cfg, people, total, s, a, week, tta, team) -> str:
    e = html.escape
    tiles = [("Engineer hours saved (est.)", s["hours_saved"]), ("Tickets handled", s["tickets_handled"]),
             ("Copilot calls avoided", s["copilot_calls_avoided"]), ("Tokens per ticket", f"{s['tokens_per_ticket']:,}"),
             ("Draft PRs (test-verified)", f"{total.get('prs', 0)} ({total.get('prs_test_verified', 0)})")]
    if "token_reduction_vs_baseline" in s:
        tiles.append(("Tokens vs plain Copilot", "−" + s["token_reduction_vs_baseline"]))
    if "cost_saved" in s:
        tiles.append(("Engineer cost saved (est.)", s["cost_saved"]))
    if tta is not None:
        tiles.append(("Median time to analysis", f"{tta} min"))
    rows = "".join(f"<tr><td>{e(k.replace('_', ' '))}</td>" + "".join(f"<td>{v.get(k, 0):,}</td>" for v in people.values())
                   + (f"<td><b>{total.get(k, 0):,}</b></td>" if len(people) > 1 else "") + "</tr>"
                   for k in _zero())
    head = "".join(f"<th>{e(p)}</th>" for p in people) + ("<th>Total</th>" if len(people) > 1 else "")
    assumptions = "".join(f"<li>{e(k.replace('_', ' '))}: <b>{e(str(v))}</b></li>" for k, v in a.items())
    tiles_html = "".join(f'<div class="tile"><div class="v">{e(str(v))}</div><div class="l">{e(l)}</div></div>'
                         for l, v in tiles)
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>AI Forge report</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{{--bg:#fff;--fg:#1d2330;--muted:#5d6678;--card:#f4f6fa;--line:#dde2ea;--accent:#2f6fde}}
@media (prefers-color-scheme: dark){{:root{{--bg:#12151c;--fg:#e6e9ef;--muted:#9aa3b5;--card:#1b202a;--line:#2b3240;--accent:#7aa5ff}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,Segoe UI,sans-serif;margin:0;padding:24px 16px}}
main{{max-width:980px;margin:auto}} h1{{margin:0 0 4px}} .sub{{color:var(--muted);margin-bottom:24px}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:28px}}
.tile{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}}
.v{{font-size:26px;font-weight:650;color:var(--accent)}} .l{{color:var(--muted);font-size:13px}}
.wrap{{overflow-x:auto}} table{{border-collapse:collapse;width:100%}} td,th{{border-bottom:1px solid var(--line);padding:6px 10px;text-align:right}}
td:first-child,th:first-child{{text-align:left}} ul{{color:var(--muted)}}
</style></head><body><main>
<h1>AI Forge – support automation report</h1>
<div class="sub">{'Team' if team else e(cfg.user_id)} · generated {datetime.now():%Y-%m-%d %H:%M} · all time{f" · this week: {week['analyzed']} analyzed, {week['prs']} PRs" if week else ''}</div>
<div class="tiles">{tiles_html}</div>
<h2>Outcomes</h2><div class="wrap"><table><tr><th>metric</th>{head}</tr>{rows}</table></div>
<h2>How "hours saved" is estimated</h2>
<p>Each outcome is multiplied by the manual effort it replaces. Adjust these in <code>team.json → savings</code>
and back them with the baseline experiment (<code>forge baseline</code>).</p><ul>{assumptions}</ul>
<p>Tokens are parsed from Copilot CLI output where available, otherwise estimated (chars/4). Reconcile weekly with the GitHub billing report.</p>
</main></body></html>"""


# ---------------- baseline experiment (§5.16) ----------------
def baseline(cfg: C.Config, tickets_dir: Path, with_fix: bool = True, log=print) -> Path:
    """For each ticket JSON in `tickets_dir` (a closed ticket with a known cause), compare:
    (a) plain `copilot -p "<ticket> find the root cause and fix it"` in a fresh worktree, and
    (b) the Forge path: rules → context pack → one agent call (→ fixer for code bugs).
    Each file: {key, summary, description, attachments?, expected: {classification, files, base_commit}}."""
    from . import analyze, fixer, gitwt, triage
    from .context_pack import build as build_pack
    from .copilot import BudgetExceeded, extract_json, run_agent
    from .index.indexer import index_repo
    from .index.store import Index

    class _Hist:  # triage needs a store for history; the experiment uses none
        def history(self):
            return []

    rows = []
    for p in sorted(tickets_dir.glob("*.json")):
        t = json.loads(p.read_text(encoding="utf-8"))
        t.setdefault("attachments", [])
        t.setdefault("comments", [])
        exp = t.get("expected") or {}
        ref = exp.get("base_commit") or cfg.base_ref
        # (a) plain Copilot
        wt = gitwt.worktree_add(cfg.repo_path, cfg.worktree_root / f"baseline-{t['key']}", ref)
        (wt / "TICKET.md").write_text(f"# {t['key']}: {t.get('summary', '')}\n\n{t.get('description', '')}\n" +
                                      "\n".join(f"\n## {a['name']}\n{a.get('text', '')}" for a in t["attachments"]),
                                      encoding="utf-8")
        try:
            r = run_agent(cfg, "baseline", "Read TICKET.md. Find the root cause of this support ticket and fix it.",
                          wt, [t["key"]], timeout=1800)
            files = [f for f in gitwt.changed_files(wt) if f != "TICKET.md"]
            rows.append(_row(t, "plain copilot", 1, r.usage, r.tokens, r.duration_s, "-", files, exp))
        except BudgetExceeded as e:
            log(f"{t['key']}: {e}")
            break
        finally:
            gitwt.worktree_remove(cfg.repo_path, wt)
        # (b) Forge
        db = C.HOME / "index" / f"baseline-{ref.replace('/', '_')}.db"
        index_repo(cfg.repo_path, ref, db, fetch=False, log=lambda *_: None)
        idx = Index(db, cfg.repo_path)
        t["signature"] = None
        status, route, _ = triage.route(cfg, idx, t, [])
        if status != "ready":
            rows.append(_row(t, "forge", 0, {}, 0, 0, status, [], exp))
            continue
        wt = gitwt.worktree_add(cfg.repo_path, cfg.worktree_root / f"baseline-forge-{t['key']}", ref)
        try:
            pack, _ = build_pack(idx, [analyze.pack_ticket(t)], budget_tokens=cfg.threshold("context_budget_tokens", 7000))
            job = {"schema": 1, "group_id": t["key"], "route": route, "tickets": [{"key": t["key"], "summary": t.get("summary")}],
                   "past_matches": [], "teammate_inflight": [], "lessons": []}
            (wt / ".forge" / "job.json").write_text(json.dumps(job), encoding="utf-8")
            (wt / ".forge" / "context.md").write_text(pack, encoding="utf-8")
            r = run_agent(cfg, route, analyze.PROMPT, wt, [t["key"]], context_chars=len(pack))
            calls, usage, tok, dur = 1, dict(r.usage), r.tokens, r.duration_s
            try:
                g = extract_json(r.stdout)["groups"][0]
            except (ValueError, KeyError, IndexError):
                g = {"classification": "invalid", "affected_files": []}
            files = g.get("affected_files") or []
            if with_fix and g.get("classification") == "code_bug":
                (wt / ".forge" / "plan.json").write_text(json.dumps({**g, "targeted_test_cmd": (cfg.team.get("test") or {}).get("targeted")}), encoding="utf-8")
                r2 = run_agent(cfg, "forge-fixer", fixer.PROMPT, wt, [t["key"]], context_chars=len(pack))
                calls, tok, dur = calls + 1, tok + r2.tokens, dur + r2.duration_s
                for k in ("input", "output"):
                    usage[k] = (usage.get(k) or 0) + (r2.usage.get(k) or 0) or None
                files = gitwt.changed_files(wt) or files
            rows.append(_row(t, "forge", calls, usage, tok, dur, g.get("classification"), files, exp))
        except BudgetExceeded as e:
            log(f"{t['key']}: {e}")
            break
        finally:
            gitwt.worktree_remove(cfg.repo_path, wt)
    return _write_baseline(rows)


def _row(t, approach, calls, usage, tokens, dur, cls, files, exp) -> dict:
    want = set(exp.get("files") or [])
    hit = bool(want & set(files)) if want else None
    return {"ticket": t["key"], "approach": approach, "calls": calls, "input_tokens": usage.get("input"),
            "output_tokens": usage.get("output"), "tokens": tokens, "seconds": dur, "classification": cls,
            "classification_ok": (cls == exp.get("classification")) if exp.get("classification") and cls != "-" else None,
            "files": files[:5], "root_cause_file_ok": hit}


def _write_baseline(rows: list[dict]) -> Path:
    out = C.HOME / "reports" / f"baseline-{datetime.now():%Y%m%d-%H%M}"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    fmt = lambda v: "-" if v is None else ("✓" if v is True else "✗" if v is False else str(v))
    lines = ["| ticket | approach | calls | tokens | seconds | class | class ok | root-cause file ok |",
             "|---|---|---:|---:|---:|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['ticket']} | {r['approach']} | {r['calls']} | {r['tokens']:,} | {r['seconds']} | "
                     f"{r['classification']} | {fmt(r['classification_ok'])} | {fmt(r['root_cause_file_ok'])} |")
    for appr in ("plain copilot", "forge"):
        rs = [r for r in rows if r["approach"] == appr]
        if rs:
            ok = [r["root_cause_file_ok"] for r in rs if r["root_cause_file_ok"] is not None]
            lines.append(f"| **{appr} total** | {len(rs)} tickets | {sum(r['calls'] for r in rs)} | "
                         f"{sum(r['tokens'] for r in rs):,} | {sum(r['seconds'] for r in rs)} | | | "
                         f"{sum(ok)}/{len(ok) if ok else '-'} |")
    lines.append("\n'root-cause file ok' is automatic (expected files ∩ touched/predicted files); "
                 "confirm each root cause by hand before presenting.")
    out.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out.with_suffix(".md")
