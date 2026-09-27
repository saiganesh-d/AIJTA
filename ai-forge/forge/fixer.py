"""Fix pipeline for approved plans (PLAN §5.14): conflict re-check → worktree → forge-fixer →
local fails-before/passes-after verification → commit on forge/<KEY> → (optional) push + draft PR
→ inflight + notify. Default is local only: a human reviews the branch and pushes it."""
import json
import re
import subprocess
import traceback
from pathlib import PurePosixPath

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
    stats = {"fixed": 0, "conflict": 0, "invalid": 0, "failed": 0, "unverified": 0, "skipped": 0}
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


def existing_tests(idx: Index, syms: list[str], limit: int = 5) -> list[str]:
    """Test files that already exercise the affected symbols: the fixer extends these instead of
    inventing a new test module (keeps the repo's layout and fixtures)."""
    found: list[str] = []

    def add(path):
        if gitwt.is_test_file(path) and path not in found:
            found.append(path)

    for s in syms:  # tests calling the symbol, or calling one of its callers (depth 2)
        for c in idx.callers(s, limit=60):
            add(c["path"])
            for cc in idx.callers(c["qualname"], limit=30):
                add(cc["path"])
    stems = {PurePosixPath(s.partition("::")[0]).stem for s in syms}
    for (path,) in idx.con.execute("SELECT path FROM files"):  # naming convention: test_parser.py ↔ parser.py
        name = PurePosixPath(path).stem.lower()
        if any(st and st.lower() in name for st in stems):
            add(path)
    return found[:limit]


def scope(plan: dict) -> set[str]:
    """Files the approved plan allows the fixer to change (besides tests)."""
    return set(plan.get("affected_files") or []) | {s.partition("::")[0] for s in plan.get("affected_symbols") or []}


def fix_one(cfg: C.Config, store, idx: Index, keys: list[str], t: dict, log=print) -> str:
    key, plan = keys[0], t["plan"]
    syms, files = plan.get("affected_symbols") or [], plan.get("affected_files") or []

    # 0) latest remote state right before touching code: approval can come hours after the analysis
    try:
        gitwt.fetch(cfg.repo_path)
    except gitwt.GitError as e:
        log(f"fix {key}: fetch failed, using the last fetched {cfg.base_ref}: {e}")
    fixed = next((c for c in (gitwt.already_fixed(cfg.repo_path, cfg.base_ref, k) for k in keys) if c), None)
    if fixed:  # someone fixed it meanwhile: no fixer call, no tokens
        store.set_status(keys, "resolved", f"already fixed on {cfg.base_ref}: {fixed}")
        cards.notify(cfg, store, key, f"{', '.join(keys)} looks already fixed on {cfg.base_ref} by {fixed}. "
                                      "No fix generated; check and close the ticket.")
        return "skipped"

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
    moved = gitwt.drift(cfg.repo_path, plan.get("analyzed_commit"), base, sorted(scope(plan)))
    fx = {**plan, "reviewer_note": t.get("reviewer_note"), "targeted_test_cmd": test.get("targeted"),
          "existing_tests": existing_tests(idx, syms), "allowed_files": sorted(scope(plan)),
          "code_changed_since_analysis": moved or None}
    fx.pop("_summary", None)
    if moved:
        log(f"fix {key}: {len(moved['files'])} planned file(s) changed since the analysis; the fixer re-checks them")
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
    # tests are decided by path, never by the agent's own label (a mislabelled code file would stay
    # in place during the "must fail before" run and weaken the check)
    tests = [f for f in changed if gitwt.is_test_file(f)]
    code = [f for f in changed if f not in tests]

    # scope guard: code outside the approved plan is not delivered unless the fixer declared it
    allowed = scope(plan)
    declared = " ".join(str(d) for d in out.get("deviations") or [])
    outside = [f for f in code if allowed and f not in allowed]
    undeclared = [f for f in outside if f not in declared]
    if undeclared:
        store.event(key, "quality:scope_violation", ", ".join(undeclared)[:300])
        _abandon(cfg, store, keys, wt, "fix_failed",
                 f"⚠ {key}: the fixer changed files outside the approved plan without declaring them: "
                 f"{', '.join(undeclared[:5])}. Nothing was committed.")
        return "failed"
    v = verify(cfg, wt, tests, code)
    if v["passed_after"] is False:
        _abandon(cfg, store, keys, wt, "fix_failed",
                 f"⚠ {key}: the new test still fails with the fix applied. Output: {v['after_out'][-300:]}")
        return "failed"
    verified = bool(v["failed_before"] and v["passed_after"])
    full_rc, full_out = gitwt.run_cmd(test["full"], wt) if test.get("full") else (0, "not configured")
    if full_rc != 0 and base_suite_passes(cfg, wt):  # it passed before the change: the fix broke something
        store.event(key, "quality:broke_existing_tests", full_out[-300:])
        _abandon(cfg, store, keys, wt, "fix_failed",
                 f"⚠ {key}: the fix breaks tests that pass on {base}. Nothing was committed. Output: {full_out[-300:]}")
        return "failed"

    # 7) commit; push and draft PR only when enabled in team.json → delivery
    warn = (("" if verified else "⚠ test not verified: ") + ("" if full_rc == 0 else "⚠ suite already failing on base: ")
            + ("⚠ deviates from plan: " if outside else ""))
    title = f"{warn}fix({', '.join(keys)}): {str(out.get('summary') or plan.get('root_cause', ''))[:80]}"
    gitwt.git(wt, "add", "-A")
    gitwt.git(wt, "commit", "-m", f"fix({', '.join(keys)}): {str(out.get('summary', ''))[:72]}\n\n"
                                   f"Root cause: {plan.get('root_cause', '')}\n\nApproved in Teams; generated by AI Forge.")
    tokens_total = sum((store.ticket(k) or {}).get("tokens") or 0 for k in keys) + tokens
    # function-level scope: changed functions the plan did not name (inside allowed files) are shown to you
    csyms = gitwt.changed_symbols(idx, wt, base)
    extra_syms = [s for s in gitwt.touched_base_symbols(idx, wt, base)
                  if s not in syms and not gitwt.is_test_file(s.partition("::")[0])]
    if extra_syms:
        store.event(key, "quality:extra_functions", ", ".join(extra_syms)[:300])
    body = pr_body(cfg, keys, plan, out, v, verified, full_rc, full_out, t.get("reviewer_note"), tokens_total,
                   extra_syms)
    (wt / ".forge" / "PR_BODY.md").write_text(f"# {title}\n\n{body}", encoding="utf-8")
    sha = gitwt.out(wt, "rev-parse", "--short", "HEAD")
    pushed, pr_url, push_err = False, None, ""
    r = gitwt.push(cfg, wt, "-u", "origin", branch)
    if r is not None:
        pushed, push_err = r.returncode == 0, (r.stderr or "")[-200:]
    if pushed and gitwt.delivery(cfg)["pull_request"]:
        pr_url = create_pr(cfg, wt, branch, t.get("base_branch") or gitwt.branch_name(cfg.base_ref), title, body,
                           reviewer(cfg, t.get("component")))

    # 8) publish state: pr_open (draft PR on GitHub) or fix_ready (verified commit on the local branch)
    status = "pr_open" if pr_url else "fix_ready"
    conflicts.write_inflight(cfg, key, status=status, branch=branch, pr_url=pr_url, pushed=pushed,
                             changed_files=gitwt.out(wt, "diff", "--name-only", f"{base}..HEAD").splitlines(),
                             changed_symbols=csyms, planned_symbols=syms)
    store.set_status(keys, status, title, pr_url=pr_url, tokens=(t.get("tokens") or 0) + tokens)
    for k in keys:
        cards.update_analysis(cfg, k, pr_url=pr_url, fix_tests=out.get("test_names") or v["tests"])  # for future tickets
        store.event(k, "pr" if verified else "pr_unverified", pr_url or f"{branch}@{sha}")
    check = "test fails before / passes after ✅" if verified else "test NOT verified ⚠"
    if pr_url:
        msg = f"Draft PR ready for {', '.join(keys)}: {pr_url} ({check})"
    else:
        where = f"pushed to origin/{branch}" if pushed else f"local branch {branch}"
        msg = (f"Fix ready for {', '.join(keys)} on {where}, commit {sha} ({check}). "
               f"Review it in {wt}; PR description: {wt / '.forge' / 'PR_BODY.md'}"
               + ("" if pushed else f". Push when happy: git push -u origin {branch}")
               + (f". ⚠ push failed: {push_err}" if r is not None and not pushed else ""))
    if extra_syms:
        msg += f". Also changed functions not named in the plan: {', '.join(extra_syms[:5])}"
    cards.notify(cfg, store, key, msg)
    log(f"fix {key}: {pr_url or branch + '@' + sha}")
    return "fixed" if verified else "unverified"


def base_suite_passes(cfg: C.Config, wt) -> bool:
    """Run the full suite on the unchanged base commit, in a clean throwaway worktree. Only called when
    the suite fails with the change, to tell "we broke it" apart from "it was already red"."""
    base = cfg.worktree_root / f"{wt.name}-base"
    gitwt.worktree_add(cfg.repo_path, base, gitwt.out(wt, "rev-parse", "HEAD"))
    try:
        rc, _ = gitwt.run_cmd(cfg.team["test"]["full"], base)
    finally:
        gitwt.worktree_remove(cfg.repo_path, base)
    return rc == 0


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


def pr_body(cfg, keys, plan, out, v, verified, full_rc, full_out, note, tokens, extra_syms=()) -> str:
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
- Full suite: {'pass ✅' if full_rc == 0 else 'failing, but it already fails on the base branch ⚠'}
{'' if full_rc == 0 else chr(10) + '```' + chr(10) + full_out[-1500:] + chr(10) + '```'}

## Review notes
- Approver note: {note or '-'}
- Fixer note: {out.get('notes_for_reviewer') or '-'}
- Deviations from plan: {', '.join(out.get('deviations') or []) or 'none'}
- Functions changed but not named in the plan: {', '.join(extra_syms) or 'none'}
- Confidence {plan.get('confidence', 0):.0%}, risk {plan.get('risk', '-')}, Copilot cost ≈ {tokens:,} tokens

_Draft PR generated by AI Forge after human approval. Nothing merges automatically._
"""


def create_pr(cfg, wt, branch, base, title, body, reviewer_handle) -> str | None:
    cmd = [*gitwt.gh_cmd(), "pr", "create", "--draft", "--head", branch, "--base", base, "--title", title, "--body", body]
    if reviewer_handle:
        cmd += ["--reviewer", reviewer_handle]
    r = subprocess.run(cmd, cwd=str(wt), capture_output=True, text=True, errors="replace", timeout=120)
    if r.returncode != 0 and reviewer_handle:  # reviewer may lack access: retry once without it
        r = subprocess.run(cmd[:-2], cwd=str(wt), capture_output=True, text=True, errors="replace", timeout=120)
    if r.returncode != 0:
        existing = subprocess.run([*gitwt.gh_cmd(), "pr", "view", branch, "--json", "url", "-q", ".url"], cwd=str(wt),
                                  capture_output=True, text=True, timeout=60)
        return existing.stdout.strip() or None
    m = re.search(r"https://\S+/pull/\d+", r.stdout)
    return m.group(0) if m else r.stdout.strip() or None


def _abandon(cfg, store, keys, wt, status, msg) -> None:
    store.set_status(keys, status, msg)
    conflicts.write_inflight(cfg, keys[0], status="abandoned")
    cards.notify(cfg, store, keys[0], msg)
    gitwt.worktree_remove(cfg.repo_path, wt)

