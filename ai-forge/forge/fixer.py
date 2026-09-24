"""Fix pipeline for approved plans (PLAN §5.14): conflict re-check → worktree → forge-fixer →
local fails-before/passes-after verification → commit, push, draft PR → inflight + notify."""
import json
import re
import subprocess
import traceback

from . import cards
from . import config as C
from . import conflicts, gitwt
from .context_pack import build as build_pack
from .copilot import BudgetExceeded, extract_json, run_agent
from .index.store import Index

PROMPT = "Implement the approved plan in .forge/plan.json."


def _plans(store) -> list[tuple[list[str], dict]]:
    """Approved tickets grouped by their plan (one fix per approved card)."""
    seen, out = set(), []
    for t in store.tickets("approved"):
        if t["key"] in seen or not t.get("plan"):
            continue
        keys = [k for k in t["plan"].get("tickets", [t["key"]])]
        seen |= set(keys)
        out.append((keys, t))
    return out


def reviewer(cfg: C.Config, component: str | None) -> str | None:
    """GitHub handle of a teammate: the component owner if it isn't me, else the first other member."""
    members = cfg.team.get("members") or {}
    owner = (cfg.team.get("components_to_owner") or {}).get(component or "")
    for uid in [owner] + sorted(members):
        if uid and uid != cfg.user_id and (members.get(uid) or {}).get("github"):
            return members[uid]["github"]
    return None


def run_fixes(cfg: C.Config, store, idx: Index, log=print) -> dict:
    stats = {"fixed": 0, "conflict": 0, "invalid": 0, "failed": 0, "unverified": 0}
    for keys, t in _plans(store):
        try:
            stats[fix_one(cfg, store, idx, keys, t, log)] += 1
        except BudgetExceeded as e:
            from .analyze import budget_notice
            store.set_status(keys, "approved", "budget reached")
            budget_notice(cfg, store, e)
            break
        except Exception as e:  # fail once and tell a human; never loop (each retry would cost tokens)
            log(f"fix {keys[0]} failed:\n{traceback.format_exc()}")
            store.set_status(keys, "fix_failed", f"{e.__class__.__name__}: {e}"[:300])
            cards.notify(cfg, store, keys[0], f"⚠ Fix for {', '.join(keys)} failed: {e.__class__.__name__}: {str(e)[:200]}")
            stats["failed"] += 1
    return stats


def fix_one(cfg: C.Config, store, idx: Index, keys: list[str], t: dict, log=print) -> str:
    key, plan = keys[0], t["plan"]
    syms, files = plan.get("affected_symbols") or [], plan.get("affected_files") or []

    # 1) conflict re-check right before touching code
    acked = (t.get("blocked_on") or "").removeprefix("ack:")
    direct = [c for c in conflicts.detect(cfg, idx, key, syms, files)
              if c["kind"] == "direct" and c["with"] != acked and c.get("status") != "merged"]
    if direct:
        cards.send_conflict(cfg, store, key, direct[0], keys)
        store.set_status(keys, "conflict_wait", f"direct overlap with {direct[0]['with']}", blocked_on="?decision")
        return "conflict"

    # 2) worktree on a dedicated branch
    branch = f"forge/{key}"
    base = f"origin/{t['base_branch']}" if t.get("base_branch") else cfg.base_ref
    wt = conflicts.fix_worktree(cfg, key)
    store.set_status(keys, "fixing", f"branch {branch} from {base}", branch=branch)
    gitwt.worktree_add(cfg.repo_path, wt, base, branch=branch)
    conflicts.write_inflight(cfg, key, status="fixing", branch=branch, base_commit=gitwt.out(wt, "rev-parse", "HEAD")[:12],
                             planned_symbols=syms, changed_files=files)

    # 3) inputs: approved plan + note + test command, context pack, teammates' diffs
    test = cfg.team.get("test") or {}
    fx = {**plan, "reviewer_note": t.get("reviewer_note"), "targeted_test_cmd": test.get("targeted")}
    fx.pop("_summary", None)
    (wt / ".forge" / "plan.json").write_text(json.dumps(fx, indent=2), encoding="utf-8")
    pack, _ = build_pack(idx, [{"key": k, "summary": (store.ticket(k) or {}).get("summary", ""),
                                "description": plan.get("root_cause", "") + "\n" + "\n".join(
                                    f"{e.get('ref')}: {e.get('why')}" for e in plan.get("evidence") or [])}
                               for k in keys], budget_tokens=cfg.threshold("context_budget_tokens", 7000))
    (wt / ".forge" / "context.md").write_text(pack, encoding="utf-8")
    tm = wt / ".forge" / "teammates"
    for c in conflicts.detect(cfg, idx, key, syms, files):
        if c.get("branch"):
            d = gitwt.git(cfg.repo_path, "diff", f"{cfg.base_ref}...origin/{c['branch']}", check=False).stdout
            if d:
                tm.mkdir(exist_ok=True)
                (tm / f"{c['with']}.diff").write_text(d[-20000:], encoding="utf-8")

    # 4) the fixer agent (may run only the targeted test executable)
    exe = (test.get("targeted") or "").split()[:1]
    res = run_agent(cfg, "forge-fixer", PROMPT, wt, keys, extra_allow=[f"shell({exe[0]})"] if exe else None,
                    context_chars=len(pack) + len(json.dumps(fx)))
    tokens = res.tokens
    try:
        out = extract_json(res.stdout)
    except ValueError:
        out = {"status": "blocked", "summary": "fixer returned no valid JSON"}

    # 5) plan invalid / blocked → human decides
    if out.get("status") != "done":
        _abandon(cfg, store, keys, wt, "plan_invalid",
                 f"⚠ {key}: fixer returned '{out.get('status')}': {str(out.get('summary', ''))[:300]}")
        return "invalid"

    # 6) verify the test-first claim locally
    changed = gitwt.changed_files(wt)
    if not changed:
        _abandon(cfg, store, keys, wt, "fix_failed", f"⚠ {key}: fixer reported done but changed no files")
        return "failed"
    tests = [f for f in changed if gitwt.is_test_file(f) or f in (out.get("test_files") or [])]
    code = [f for f in changed if f not in tests]
    v = verify(cfg, wt, tests, code)
    if v["passed_after"] is False:
        _abandon(cfg, store, keys, wt, "fix_failed",
                 f"⚠ {key}: the new test still fails with the fix applied. Output: {v['after_out'][-300:]}")
        return "failed"
    verified = bool(v["failed_before"] and v["passed_after"])
    full_rc, full_out = gitwt.run_cmd(test["full"], wt) if test.get("full") else (0, "not configured")

    # 7) commit, push, draft PR
    warn = ("" if verified else "⚠ test not verified: ") + ("" if full_rc == 0 else "⚠ full suite failing: ")
    title = f"{warn}fix({', '.join(keys)}): {str(out.get('summary') or plan.get('root_cause', ''))[:80]}"
    gitwt.git(wt, "add", "-A")
    gitwt.git(wt, "commit", "-m", f"fix({', '.join(keys)}): {str(out.get('summary', ''))[:72]}\n\n"
                                   f"Root cause: {plan.get('root_cause', '')}\n\nApproved in Teams; generated by AI Forge.")
    tokens_total = sum((store.ticket(k) or {}).get("tokens") or 0 for k in keys) + tokens
    body = pr_body(cfg, keys, plan, out, v, verified, full_rc, full_out, t.get("reviewer_note"), tokens_total)
    push = gitwt.git(wt, "push", "--force-with-lease", "-u", "origin", branch, check=False)
    pr_url = None
    if push.returncode == 0:
        pr_url = create_pr(cfg, wt, branch, t.get("base_branch") or gitwt.branch_name(cfg.base_ref), title, body,
                           reviewer(cfg, t.get("component")))
    if not pr_url:
        (wt / ".forge" / "PR_BODY.md").write_text(body, encoding="utf-8")
        store.set_status(keys, "fix_failed", "push or PR creation failed", tokens=(t.get("tokens") or 0) + tokens)
        cards.notify(cfg, store, key, f"⚠ {key}: fix committed on {branch} in {wt} but push/PR failed: "
                                      f"{(push.stderr or '')[-200:]}")
        return "failed"

    # 8) publish state
    csyms = gitwt.changed_symbols(idx, wt, base)
    conflicts.write_inflight(cfg, key, status="pr_open", branch=branch, pr_url=pr_url,
                             changed_files=gitwt.out(wt, "diff", "--name-only", f"{base}..HEAD").splitlines(),
                             changed_symbols=csyms, planned_symbols=syms)
    store.set_status(keys, "pr_open", title, pr_url=pr_url, tokens=(t.get("tokens") or 0) + tokens)
    for k in keys:
        cards.update_analysis(cfg, k, pr_url=pr_url)
        store.event(k, "pr" if verified else "pr_unverified", pr_url)
    cards.notify(cfg, store, key, f"Draft PR ready for {', '.join(keys)}: {pr_url} "
                                  f"({'test fails before / passes after ✅' if verified else 'test NOT verified ⚠'})")
    log(f"fix {key}: {pr_url}")
    return "fixed" if verified else "unverified"


def verify(cfg: C.Config, wt, tests: list[str], code: list[str]) -> dict:
    """Stash the non-test change → the new test must FAIL; restore → it must PASS."""
    tpl = (cfg.team.get("test") or {}).get("targeted") or ""
    v = {"tests": tests, "failed_before": None, "passed_after": None, "before_out": "", "after_out": ""}
    if not tests or not tpl:
        return v
    cmd = tpl.replace("{test_path}", " ".join(tests))
    if code:
        gitwt.git(wt, "stash", "push", "--include-untracked", "--", *code)
        try:
            rc, v["before_out"] = gitwt.run_cmd(cmd, wt)
            v["failed_before"] = rc != 0
        finally:
            gitwt.git(wt, "stash", "pop")
    rc, v["after_out"] = gitwt.run_cmd(cmd, wt)
    v["passed_after"] = rc == 0
    return v


def pr_body(cfg, keys, plan, out, v, verified, full_rc, full_out, note, tokens) -> str:
    ev = "\n".join(f"- `{e.get('ref')}` – {e.get('why')}" for e in plan.get("evidence") or []) or "-"
    steps = "\n".join(f"{i}. {s}" for i, s in enumerate((plan.get("proposed_fix") or {}).get("steps") or [], 1))
    return f"""## Tickets
{cards.ticket_links(cfg, keys)}

## Root cause
{plan.get('root_cause', '')}

**Evidence**
{ev}

## Change
{out.get('summary', '')}

{steps}

## Test
- New test(s): {', '.join(out.get('test_names') or v['tests']) or '-'}
- Fails without the fix: {'yes ✅' if v['failed_before'] else 'NOT VERIFIED ⚠'}
- Passes with the fix: {'yes ✅' if v['passed_after'] else 'NOT VERIFIED ⚠'}
- Full suite: {'pass ✅' if full_rc == 0 else 'FAILING ⚠'}
{'' if full_rc == 0 else chr(10) + '```' + chr(10) + full_out[-1500:] + chr(10) + '```'}

## Review notes
- Approver note: {note or '-'}
- Fixer note: {out.get('notes_for_reviewer') or '-'}
- Deviations from plan: {', '.join(out.get('deviations') or []) or 'none'}
- Confidence {plan.get('confidence', 0):.0%}, risk {plan.get('risk', '-')}, Copilot cost ≈ {tokens:,} tokens

_Draft PR generated by AI Forge after human approval. Nothing merges automatically._
"""


def create_pr(cfg, wt, branch, base, title, body, reviewer_handle) -> str | None:
    cmd = ["gh", "pr", "create", "--draft", "--head", branch, "--base", base, "--title", title, "--body", body]
    if reviewer_handle:
        cmd += ["--reviewer", reviewer_handle]
    r = subprocess.run(cmd, cwd=str(wt), capture_output=True, text=True, errors="replace", timeout=120)
    if r.returncode != 0 and reviewer_handle:  # reviewer may lack access: retry once without it
        r = subprocess.run(cmd[:-2], cwd=str(wt), capture_output=True, text=True, errors="replace", timeout=120)
    if r.returncode != 0:
        existing = subprocess.run(["gh", "pr", "view", branch, "--json", "url", "-q", ".url"], cwd=str(wt),
                                  capture_output=True, text=True, timeout=60)
        return existing.stdout.strip() or None
    m = re.search(r"https://\S+/pull/\d+", r.stdout)
    return m.group(0) if m else r.stdout.strip() or None


def _abandon(cfg, store, keys, wt, status, msg) -> None:
    store.set_status(keys, status, msg)
    conflicts.write_inflight(cfg, keys[0], status="abandoned")
    cards.notify(cfg, store, keys[0], msg)
    gitwt.worktree_remove(cfg.repo_path, wt)

