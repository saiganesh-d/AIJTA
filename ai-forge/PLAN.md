# AI Forge – Support Ticket Copilot: Development Plan

Automates first-line analysis of Jira support tickets for a small team (3+ engineers) using
GitHub Copilot CLI, with human approval in Microsoft Teams. It runs entirely on team members'
laptops, with no server, no open ports and no premium Power Automate connectors.

**Status legend:** ✅ built in this repo · 🔧 to build · ⏳ optional / later

**Status (2026-09-24):** every 🔧 item from the first version of this plan is built and covered by
`tests/` (fake Copilot/gh CLIs, a real git repo with a bare origin, file-mode Jira). Still open: live
calibration against your Copilot CLI version (§5.6, §5.16) and the optional embeddings (§5.5). See §9 for
what was added beyond the plan.

---

## 1. Goals and principles

1. **Tokens are money.** Since June 2026 Copilot bills AI Credits per token (input, output, cached).
   Every design choice below either removes a Copilot call or shrinks one.
2. **Local first.** Anything that can be done with rules, git or a local index is done before Copilot runs.
3. **Humans decide.** Copilot proposes; a person approves in Teams. Output is always a *draft PR*.
4. **No infrastructure.** Laptops + a synced SharePoint folder + standard Power Automate connectors.
5. **Zero-effort onboarding.** One install command; updates arrive automatically through the shared folder.

## 2. Architecture

```
            Jira (REST, read + comment)
                     │
   ┌─────────────────▼─────────────────┐        ┌──────────────────────────────┐
   │  Runner on each laptop (forge run)│        │ AI-Forge-Shared (SharePoint, │
   │  every 10 min, Task Scheduler     │◄──────►│ synced by OneDrive client)   │
   │                                   │  files │  analyses/  inflight/        │
   │ sync → pre-classify → dedup/      │        │  decisions/ outbox/          │
   │ history → group → context pack →  │        │  runners/   config/ agents/  │
   │ Copilot agent → outbox card       │        │  lessons/   tool/            │
   │ ← decision → fix → PR → revalidate│        └──────────────┬───────────────┘
   │                                   │                       │ file created
   │ local: SQLite cache, code index,  │        ┌──────────────▼───────────────┐
   │ git worktrees, token ledger       │        │ Power Automate (standard)    │
   └───────────────┬───────────────────┘        │ post card → wait → write     │
                   │ copilot -p --agent …       │ decision file                │
                   ▼                            └──────────────┬───────────────┘
   GitHub Copilot CLI (user's own login) ── MCP stdio ──► forge-index        │
                   │                                           Teams channel ◄┘
                   ▼
          git push branch + gh pr create --draft
```

**Who writes what (one writer per file):**

| Shared folder | Written by | Read by |
|---|---|---|
| `analyses/<TICKET>.json` | assignee's runner | all runners, MCP `related_tickets` |
| `inflight/<TICKET>.json` | assignee's runner | all runners, MCP `inflight_changes` |
| `outbox/<TICKET>__<req>.json` | runners | Power Automate (then deleted) |
| `decisions/<TICKET>__<req>.json` | Power Automate only | assignee's runner |
| `runners/<user>.json` | that user's runner | everyone, weekly digest flow |
| `config/team.json`, `agents/`, `tool/`, `lessons/` | team lead | all runners |

**Local only (never shared):** SQLite cache, code index, embeddings, logs, raw attachments,
full Copilot output, `.env`/tokens (tokens live in the OS keychain).

## 3. Cost model and levers

Measured per ticket: **Copilot calls**, **input/output/cached tokens**, **wall time**, **outcome**.

| Lever | Where | Effect |
|---|---|---|
| Rules + dedup + error-signature match | §5.7, §5.9 | Many tickets never reach Copilot |
| Non-code tickets go to `forge-config` on a cheap model | §5.7, §5.11 | Cheaper model, narrow scope |
| Grouping related tickets | §5.8 | N tickets → 1 analysis |
| Context pack (~300–7,000 tokens) | §5.10 ✅ | Agent starts with evidence, doesn't explore |
| `forge-index` MCP tools | §5.6 ✅ | Compact lookups instead of reading whole files |
| REPO_MAP.md (~2k tokens) | §5.5 ✅ | Replaces directory exploration |
| Stable prompt prefix (agent file + repo map first, ticket last) | §5.11 | More cached-token hits (cheaper) |
| Strict JSON output with length caps | agents ✅ | Small output tokens |
| Approved plan passed to fixer | §5.14 | Fixer doesn't re-investigate |
| Local JSON repair, never "re-ask" | `copilot.py` ✅ | No retry calls |
| Negative cache: `needs_info` waits for reporter update | §5.3 | No repeated analysis |
| Daily per-user token budget | `copilot.py` ✅ | Hard stop |

## 4. Repository layout

```
ai-forge/
  PLAN.md  README.md  VERSION  pyproject.toml  install.ps1  install.sh
  flows/TEAMS_FLOW.md                 Power Automate build guide (standard connectors)
  forge/
    cli.py                ✅ forge setup|doctor|index|mcp|search|context|run|stats
    config.py             ✅ local config + team.json + keychain
    shared.py             ✅ atomic writes, safe reads, conflict-copy filtering
    signals.py            ✅ scrub, log trim, stack frames, error signature, search terms
    index/indexer.py      ✅ incremental index of origin/main + repo map
    index/store.py        ✅ search, symbol_at, callers/callees, impact
    context_pack.py       ✅ token-budgeted context pack
    mcp_server.py         ✅ forge-index MCP server (10 tools)
    copilot.py            ✅ agent runner: permissions, MCP, budget, token ledger, JSON extraction
    setup_wizard.py       ✅ onboarding, agent sync, scheduler
    doctor.py             ✅ health checks + live MCP-mode detection
    pipeline.py           ✅ run cycle: lock, work hours, validation, self-update, stages, heartbeat
    jira.py               ✅ §5.3 (Server/DC, Cloud, file mode)
    store.py              ✅ §5.4 local SQLite cache
    triage.py             ✅ §5.7–5.9
    analyze.py            ✅ §5.11
    cards.py              ✅ §5.13
    fixer.py              ✅ §5.14
    gitwt.py              ✅ worktrees, test runs, diff → symbols
    conflicts.py          ✅ §5.15
    metrics.py            ✅ §5.16 counts, savings report, baseline experiment
    assets/agents/        ✅ forge-analyst, forge-config, forge-fixer, forge-adapter, forge-doctor
    assets/cards/         ✅ approval, info, conflict
    assets/schemas/       ✅ analysis, decision, inflight, outbox, runner
    assets/team.example.json ✅
```

---

## 5. Build instructions by section

Each section lists what to build, the interfaces, and **acceptance criteria (AC)**.

### 5.1 Installer and auto-update ✅ (verify on a clean laptop)
- The team lead puts this repo in `AI-Forge-Shared/tool/`. Teammates run
  `powershell -ExecutionPolicy Bypass -File "<shared>\tool\install.ps1"`.
- The installer checks python ≥3.11, git, gh, copilot. It then creates `~/.ai-forge/venv`, installs
  the package and runs `forge setup`.
- ✅ **Auto-update:** at the start of `forge run`, compare `<shared>/tool/VERSION` with
  `~/.ai-forge/installed_version`. If newer, `pip install <shared>/tool` into the venv, update the
  file, log it and continue. Agents already sync every run (`sync_agents`).
- **AC:** a new teammate goes from nothing to `forge doctor` all ✓ in under 15 minutes, with no manual file editing.

### 5.2 Configuration and secrets ✅
- Local `~/.ai-forge/config.json` holds user id, email, repo path, shared path and `mcp_mode`.
- `team.json` in the shared folder holds Jira settings, members, approvers, models, thresholds,
  test commands and work hours. The lead edits it once for everyone.
- The Jira token lives in the OS keychain via `keyring`. Never write it to disk or to shared files.
- ✅ Validate `team.json` on load. Refuse to run on `REPLACE_*` model ids, and list the problems.
- **AC:** grepping the shared folder and `~/.ai-forge` for the token finds nothing.

### 5.3 Jira sync ✅ `forge/jira.py`
1. Build a `Jira` client for Server/DC (Bearer PAT, `/rest/api/2/search`, `startAt`) and Cloud
   (basic email+token, `/rest/api/3/search/jql`, `nextPageToken`, ADF→text).
2. Use two queries per run:
   - **Mine:** `<scope_jql> AND assignee = currentUser() AND updated >= "<last_run>"`. These are
     analyzed and acted on.
   - **Team history (hourly):** `<scope_jql without status filter> AND updated >= "<last>"`. These
     are embedded only, for dedup and grouping hints.
3. Fields: summary, description, issuetype, labels, components, priority, attachment, comment,
   updated, sprint (custom field id in `team.json`), fixVersions.
4. Attachments: download only text-like types (≤2 MB) into `~/.ai-forge/attachments/<key>/`.
   Store trimmed and scrubbed text only (`signals.trim_log`, `signals.scrub`). List images and
   binaries by name. Optional OCR comes later.
5. Re-analysis rule: a ticket in `needs_info` re-enters the pipeline only if a new comment or
   attachment from the reporter arrived after the analysis.
6. **Cooldown:** new tickets wait `new_ticket_cooldown_minutes`, so a burst of similar tickets
   from several people is grouped instead of analyzed twice.
- **AC:** 500 tickets sync in under 60 s. A second run with no Jira changes makes 1–2 requests and no DB writes.

### 5.4 Local store ✅ `forge/store.py` (SQLite, `~/.ai-forge/forge.db`)
Tables: `tickets(key, summary, description, type, component, priority, sprint, reporter,
updated, status, group_id, signature, embedding BLOB, attachments_json, raw_json)`,
`sync_state`, `pending_requests(request_id, ticket_key, kind, created, reposted)`.
Ticket status machine:
`new → cooling → ready → analyzing → awaiting_decision → approved → fixing → pr_open → merged`,
with side exits `skipped | duplicate | needs_info | info_sent | resolved | rejected | fix_failed | plan_invalid`.
- **AC:** every transition is written in one transaction, and the runner is safe to kill at any point and rerun.

### 5.5 Code index and repo map ✅ `forge/index/*`
- Indexes `base_ref` (for example `origin/main`) straight from git objects, independent of the working copy.
- It is incremental by blob sha: only changed files are re-parsed on each run (seconds).
- Tree-sitter parses around 20 languages into functions and classes. Python falls back to `ast`, and
  everything else falls back to 60-line windows. Config files are indexed as 40-line windows.
- It builds a call graph (by name) and SQLite FTS5 over identifier sub-words, strings and log messages.
- `REPO_MAP.md` is a ~2k-token map of files ranked by in-degree.
- ⏳ Optional: `pip install ai-forge[embeddings]` adds fastembed vectors for chunks, used for hybrid
  search, only if the corporate proxy allows the model download. FTS alone works well for code identifiers.
- **AC:** `forge search "<error text>"` puts the right function in the top 3 for 8 of 10 past tickets.

### 5.6 forge-index MCP server ✅ `forge/mcp_server.py`
Tools: `ping, search_code, get_symbol, symbol_at, file_outline, get_callers, get_callees,
config_lookup, related_tickets, inflight_changes`. It runs over stdio as a Copilot subprocess, so no port is needed.
- **Known CLI behaviour:** some Copilot CLI versions don't pass MCP tools to custom agents in
  `-p` mode. `forge doctor --live` tests this and sets `mcp_mode`:
  `agent` (MCP inside custom agents), `global` (MCP works only without `--agent`), or `off`
  (context pack only, which still works at a slightly higher token cost).
- ✅ If the mode is `global`, the runner passes the agent's instructions as a prompt prefix file
  instead of `--agent`. Build this in `copilot.run_agent`.
- **AC:** `forge doctor --live` reports a mode, and the ledger shows fewer input tokens with MCP on than off.

### 5.7 Pre-classifier and router ✅ `forge/triage.py`
Free rules decide the **route** before any Copilot call:
1. Not a support issue type or label → `skipped`.
2. Too little information (description < 80 chars, no attachments, no stack trace) → Jira comment
   asking for logs and steps → `needs_info`. No Copilot call.
3. **Known signature:** same `error_signature` as a past analysis whose fix is still present on
   `base_ref` → `duplicate` card with the old analysis. No Copilot call.
4. **Non-code suspicion** → route `forge-config` (cheap model). Signals include: config keys from
   the ticket found by `index.search(kinds=("config",))`; words like "after deploy", "in env",
   "certificate", "permission", "401/403", "timeout", "connection refused", "how do I"; no app
   stack frames resolved.
5. Otherwise → route `forge-analyst`.
- **AC:** on 30 historical tickets, rules route at least 90% the same way a human would, and fewer
  than 5% of real code bugs are sent to `forge-config`. The agent escalates those anyway.

### 5.8 Grouping ✅ `forge/triage.py`
Build candidate groups over *my* `ready` tickets using union-find:
- Same `error_signature` → join.
- Text similarity ≥ `group_similarity` (FTS/embedding) and same component → join.
- Jaccard of predicted symbols (top-5 `search` hits + resolved frames) ≥ `group_symbol_jaccard` → join.
- Cap each group at `max_group_size`. Record the reasons in `job.json`; the analyst can split groups.
- **AC:** 4 tickets with 2 shared causes produce 2 Copilot calls. Split groups come back as separate
  `groups[]` entries, and each gets its own card.

### 5.9 Dedup, past sprints and regression facts ✅ `forge/triage.py`
For each group, find the top 3 similar past tickets from `analyses/` plus Jira team history. For
each match with a known fix, compute **git facts** locally:
- Fix commit: `pr_url`/`fix_commit` from the analysis, else `git log --grep=<KEY> <base_ref>`.
- Present: `git merge-base --is-ancestor <sha> <base_ref>`.
- Reverted: `git log --grep="This reverts commit <sha>" <base_ref>`.
- Modified since: `git log --format=%h <sha>..<base_ref> -- <files>`, narrowed to the fix's
  symbol line ranges using the index.
These go to `job.json.past_matches` as one-line facts, for example
`SUP-880 (Sprint 42, PR #123): fix a1b2c3 present; parser.py::load modified since in d4e5f6 by X`.
- **AC:** a reverted fix is flagged as a possible regression on the card, with the commit.

### 5.10 Context pack ✅ `forge/context_pack.py`
Priority order under `context_budget_tokens`: scrubbed tickets → code at stack-trace frames →
call-graph neighbours → search hits → config matches → past matches, teammate overlaps and lessons.
- ✅ Wire `extras` from §5.9 and §5.15. Add `lessons/<component>.md` (the three most recent lines).
- Demo: `forge context ticket.txt` prints the pack and its token estimate.
- **AC:** median pack under 3k tokens, and the pack alone contains the root cause for most tickets.

### 5.11 Analysis ✅ `forge/analyze.py`
1. Create a detached worktree at `base_ref`: `~/.ai-forge/worktrees/analyze-<group>`.
2. Write `.forge/job.json`, `.forge/context.md` and `.forge/REPO_MAP.md`. Add `.forge/` to the
   worktree's `info/exclude`.
3. Call `copilot.run_agent(cfg, "forge-analyst" | "forge-config", "Analyze the ticket group in .forge/job.json.", cwd, tickets, context_chars=len(pack))`.
   Keep the prompt tiny and constant; all instructions live in the agent file.
4. Parse with `extract_json`, then validate each group against `analysis.schema.json`. On invalid
   JSON, mark `analyze_failed`. **Never re-ask.**
5. If `forge-config` returns `escalate_to_analyst`, run `forge-analyst` once, with the budget check.
6. If confidence < `escalate_below_confidence` and priority is High/Critical, allow one rerun on the
   `escalation` model (budget permitting). Otherwise send `needs_info` questions to Jira.
7. Write `analyses/<TICKET>.json` for every ticket in the group (same `group_id`). Remove the worktree.
- **AC:** exactly one Copilot call per group in the normal path, and every card shows its token cost.

### 5.12 Shared-folder protocol ✅ `forge/shared.py` (use it everywhere)
- Always `atomic_write`, and always read through `read_dir` with the strict file-name regex.
- Unknown `schema` versions are skipped, not crashed on.
- Setup reminds users to mark the folder "Always keep on this device".
- **AC:** simultaneous writes from 3 laptops for an hour produce no parse errors and no processed conflict copies.

### 5.13 Teams cards and decisions ✅ `forge/cards.py` + flow (see `flows/TEAMS_FLOW.md`)
- Fill the templates in `assets/cards/`. Remove the `regression_banner` and `conflict_banner`
  containers when they don't apply. Add an @mention of the assignee.
- Card types:
  - `approval` for code bugs, single or grouped (buttons: approve, reject).
  - `info` for non-code tickets (buttons: resolved, analyze_as_code). There is no fix flow for these.
  - `conflict` (buttons: wait, build_on, proceed).
  - `notify` for plain messages: PR ready, revalidated, budget reached, runner offline.
- Write `outbox/<KEY>__<request_id>.json`. Record `pending_requests`.
- Read `decisions/`. Accept a decision only if all of these hold:
  - its `request_id` is pending,
  - the `responder` is in `approvers`,
  - the responder is the assignee or the lead.
  Otherwise ignore it and post a notify.
- Re-post stale approvals after 5 working days.
- On `reject`, append the comment to `lessons/<component>.md`. That's the learning loop.
- **AC:** a hand-made decision file from a non-approver is ignored, and a stale card's click is ignored.

### 5.14 Fix pipeline ✅ `forge/fixer.py`
1. Before fixing, re-run the conflict check (§5.15). If it's `direct` and the teammate hasn't
   merged yet, send a conflict card and wait for the choice.
2. Create worktree `-B forge/<key>` from `base_ref`, or from the teammate's branch on `build_on`.
   Write `inflight/<KEY>.json` with status `fixing` and `planned_symbols`.
3. Write `.forge/plan.json` (the approved analysis + `reviewer_note` + `targeted_test_cmd`),
   `.forge/context.md`, and `.forge/teammates/*.diff` for overlapping in-flight work.
4. Run `forge-fixer`, allowing `shell(<targeted test executable>)` in addition to its profile.
5. Handle `plan_invalid` / `blocked` → notify, status `plan_invalid`, stop.
6. **Verify the test-first claim locally:**
   - Stash the non-test changes and run the new test; it must **fail**.
   - Restore the changes and run it again; it must **pass**.
   - Run the full suite.
7. Commit, push, and run `gh pr create --draft` with the root cause, evidence, test result and token
   cost in the body. Request review from another team member.
8. Update `inflight` (`pr_open`, `changed_files`, and `changed_symbols` mapped from the diff hunks
   through `index.symbol_at`). Send a notify card.
- **AC:** no PR without a test that failed before and passes after, unless flagged "⚠ test not
  verified" in the PR title.

### 5.15 Conflicts and post-merge revalidation ✅ `forge/conflicts.py`
- **Detect** (at analysis and again before fixing) against other owners' `inflight/*.json`:
  - `direct`: my affected symbols ∩ their changed or planned symbols.
  - `dependency`: `index.impact(their_symbols, depth=2)` ∩ my symbols, or the reverse.
  - `same_file`: file overlap only (note it on the card, no blocking).
- **Merge watch** (every run): for my PRs, run `git fetch`. For every teammate `inflight` entry
  that turned `merged` (or whose `merge_commit` is on `base_ref`) and overlaps my branch:
  1. `git rebase <base_ref>` in my worktree.
  2. On a clean rebase, run the full tests. On pass, push with force-with-lease and notify
     "still valid after SUP-X ✅".
  3. On conflict or test failure, write `.forge/adapt.json` and run `forge-adapter` once. Continue
     the rebase, run the tests and push. If the status is `needs_human`, send a notify and leave the
     branch paused.
- **AC:** a scripted scenario (two branches editing the same function, one merged) produces a
  conflict card first, then an automatic rebase with a correct adapter result or a clear `needs_human`.

### 5.16 Metrics and the baseline experiment ✅ `forge/metrics.py`
- The ledger (`~/.ai-forge/ledger.db`, ✅) records agent, model, duration, estimated and parsed
  tokens, and the raw usage tail. Calibrate `copilot.parse_usage` against your CLI version's real
  output. Reconcile weekly with the GitHub billing usage report.
- `runners/<user>.json` counts: analyzed, grouped, info_only, duplicates, fixes, prs, rejected, tokens.
- A weekly digest flow reads `runners/*.json` and posts to Teams.
- **Baseline experiment for the event:** pick 10 closed tickets with known root causes. Run
  (a) plain `copilot -p "<ticket text> find the root cause and fix it"` in a fresh worktree, and
  (b) the Forge pipeline. Compare tokens, calls, time, correct root cause (yes/no) and correct
  classification.
- **AC:** the numbers go on one slide. Report the real ones, even if some tickets lose.

### 5.17 Safety checklist
- Deny `shell(git push|commit|rebase|reset|rm|curl|wget|ssh|pip|npm)` and `url` for every agent,
  plus `write` and `shell` for the read-only agents (✅ `copilot.PROFILES`).
- Agents are told ticket content is untrusted, as a guard against prompt injection in tickets.
- Scrub secrets, emails and VINs before anything is written to a context pack or shared file (✅ `signals.scrub`).
  Extend the rules for your data classes.
- Only draft PRs, reviewed by a second person. Nothing merges automatically.
- Confirm with IT/security that Copilot CLI use on this repo and the SharePoint folder location
  meet your data classification rules.

---

## 6. Phases

| Phase | Scope | Exit criteria |
|---|---|---|
| 0. Setup (1–2 days) | lead fills `team.json`; flow built; installer tested on 2 laptops | `forge doctor --live` ✓ on all laptops |
| 1. Single-user core (3–4 days) | §5.3, 5.4, 5.7 (rules 1–2, 5), 5.11, 5.13 approval + info, 5.14 | one real ticket → card → approve → draft PR |
| 2. Intelligence (3 days) | §5.7 full, 5.8, 5.9, 5.10 extras, lessons | grouped card, duplicate card, regression banner, info card |
| 3. Team (2–3 days) | §5.15, runner heartbeats, offline alert, auto-update | conflict scenario passes AC |
| 4. Measure and demo (2 days) | §5.16 baseline, digest flow, slides | before/after table with real numbers |

## 7. Demo script (7 minutes)
1. The problem: support tickets, time to root cause, and token cost now metered per token.
2. `forge context` on a real ticket: "the whole answer fits in ~300 tokens".
3. Live Teams channel: a grouped card (2 tickets, 1 call), an info card (config, no code change),
   a regression banner.
4. Approve, then show the draft PR with the fails-before/passes-after test.
5. The conflict card between two teammates, then the automatic revalidation after merge.
6. The baseline table: tokens, calls and time per ticket vs plain Copilot. The weekly digest.
7. Onboarding: "a new teammate runs one command".

## 8. Risks and mitigations
| Risk | Mitigation |
|---|---|
| Copilot CLI flags or agent behaviour change between versions | `forge doctor --live` on every version bump; pin the CLI version in `team.json`; all calls go through `copilot.py` |
| MCP not available in `-p` mode | `mcp_mode` fallback; the context pack carries the evidence |
| OneDrive sync delays or conflicts | one writer per file, atomic writes, strict readers, decisions keyed by request id |
| Laptop offline | stale-heartbeat alert; the lead reassigns in Jira |
| Wrong classification sends a real bug to info-only | "It's actually code → analyze" button; rejection lessons |
| Token budget exhausted mid-day | queue continues next day; budget card to the lead |

## 9. Additions beyond the original plan
| Addition | Why |
|---|---|
| `forge report [--team]` → self-contained HTML (light/dark) | The management view: hours saved (estimate with visible assumptions), Copilot calls avoided, tokens per ticket, PRs, outcomes per person. No server; drop the file in SharePoint or Teams. Assumptions live in `team.json → savings`. |
| `forge baseline <folder>` | Runs §5.16 end to end: plain Copilot vs Forge on the same commit *before* the real fix, with an automatic "root-cause file ok" column and a markdown table for the slide. |
| Jira `file` mode (`jira.mode = "file"`) | Demos and teams whose Jira API is blocked from laptops: tickets come from exported JSON files, comments go to `comments.log`. See `examples/`. |
| Single-instance OS lock | A 20-minute fix must not overlap the next 10-minute scheduled run. The lock is released automatically if the process dies. |
| Crash recovery | Tickets left in `analyzing`/`fixing` by a killed run go back to `ready`/`approved`. |
| Stage isolation + errors in heartbeat | One failing stage (e.g. Jira down) doesn't stop decisions, fixes or merge watch; errors are visible in `runners/<user>.json`. |
| Per-writer lessons (`lessons/<component>__<user>.md`) | Keeps "one writer per file" for the rejection-learning loop; the lead's `lessons/<component>.md` is read too. |
| Merge detection of my own PRs (`gh pr view`) | Sets `inflight` to `merged` with `merge_commit`, records `fix_commit` in `analyses/` (feeds §5.9) and removes the worktree. Closed PRs → `abandoned`. |
| Tickets closed in Jira stop waiting | The hourly team-history query closes pending cards for tickets that were resolved elsewhere. |
| Local tool copy before `pip install` | An in-tree pip build would write `build/` and `*.egg-info` into the synced shared folder for everyone. |
| Hidden scheduled task on Windows (`wscript` launcher) | No console window flashing every 10 minutes; no admin rights needed. |
| `copilot.cmd` resolution + prefix file for `global` mode | npm installs a `.cmd` shim on Windows that `subprocess` can't find by name, and cmd.exe mangles multi-line arguments. |
| Fix commit lookup skips `Revert "…"` commits | Otherwise the revert itself (which mentions the key) is taken as the fix and a regression is missed. |
| `forge status` | Where each of my tickets is, and which cards are pending. |

## 9a. Pilot decisions (2026-09-24)
- Jira Cloud, REST v3; **read-only** (`post_comments: false`). Needs-info questions go to the assignee in Teams.
- Models: `"auto"` everywhere (no `--model` flag). Trade-off: the cheap-model routing lever of §3 is left to
  Copilot, and escalation (§5.11 step 6) is disabled. Set explicit ids per agent later if the ledger shows it pays off.
- Delivery: **local only** by default (`delivery.push: false`, no GitHub CLI, no CI). A fix ends as a verified
  commit on `forge/<KEY>` (status `fix_ready`); the engineer pushes it. `delivery.push` / `delivery.pull_request`
  turn §5.14 step 7 back on later. Merge detection without GitHub: `git cherry <base_ref> forge/<KEY>`.
- Checks run from the command line (`python -m pytest -q`); the GitHub Actions workflow was removed.
- Savings assumptions: defaults until the lead confirms them. Security sign-off (§5.17): pending.

## 10. Next steps (suggested)
1. Phase 0 on two laptops: `forge doctor --live`, then calibrate `copilot.parse_usage` against the real usage
   tail stored in `ledger.db → calls.raw_tail` (one regex change if the format differs).
2. Run `forge baseline` on 10 closed tickets and put `baseline_tokens_per_ticket` into `team.json → savings`,
   so the report shows the real token reduction.
3. Agree the `savings` minutes with the team lead (they drive the "hours saved" tile), then schedule the weekly
   digest flow (Flow 2) with a link to `forge report --team`.
4. Later: optional embeddings (§5.5), OCR for screenshot attachments, and a Jira transition on `resolved`.

