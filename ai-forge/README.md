# AI Forge – Support Ticket Copilot

Jira support tickets → local triage and code index → GitHub Copilot CLI agents → Teams approval → verified fix branch.
Runs on your laptop with your own Copilot login. No server, no open ports. **Single user:** you own the code,
and only you approve or reject (the Teams click must come from your own account).

## Install (each teammate, once)
1. Make sure you have Python 3.11+, Git and Copilot CLI (`npm install -g @github/copilot`, then run
   `copilot` once and log in). The GitHub CLI is **not** needed (only if you later turn on `delivery.pull_request`).
2. Create a folder `AI-Forge-Shared` in **your own OneDrive** (not a shared library) and set it to
   **Always keep on this device**. Only you and your Power Automate flow write to it, so nobody else can
   drop in a decision file or change the tool.
3. Run:
   `powershell -ExecutionPolicy Bypass -File "<path>\AI-Forge-Shared\tool\install.ps1"`
4. Run `forge doctor --live`.

That's it. A scheduled task runs `forge run` every 10 minutes during work hours. Tool and agent
updates arrive automatically through the shared folder.

## Useful commands
| Command | What it does |
|---|---|
| `forge doctor --live` | health check (incl. `team.json` validation) + detects whether MCP works with agents on your CLI version |
| `forge run --force` | run one cycle now |
| `forge status` | my tickets, their pipeline state, and pending Teams cards |
| `forge report` / `forge report --team` | HTML savings report for management (hours saved, calls avoided, tokens per ticket, PRs) |
| `forge baseline <folder>` | baseline experiment: plain Copilot vs Forge on closed tickets (see `examples/baseline`) |
| `forge check-retrieval <folder>` | **free** (no Copilot): on closed tickets, does the context find the fixed files and the related past tickets? Tune `context` with it |
| `forge gaps` | how much of the evidence Copilot cited was already in the pack, and the code it most often had to fetch itself: where retrieval should improve |
| `forge search "text"` | search the code index |
| `forge context ticket.txt` | show the context pack Copilot would get, with its token estimate |
| `forge index --full` | rebuild the code index |
| `forge stats` | Copilot calls and tokens per day and agent |

## What one cycle does
`forge run` (every 10 min, one instance at a time): self-update → sync agents → refresh the code index →
Jira sync → free local rules (skip / ask for info / known duplicate / route) → Teams decisions →
group related tickets → one Copilot call per group → cards → merge watch and revalidation →
approved fixes (test must fail before and pass after) → draft PR → heartbeat.
Every step that can be done without Copilot is done without it; see `PLAN.md` §3.

## Team decisions (pilot)
| Topic | Setting |
|---|---|
| Jira | Cloud, REST v3 (`api_version: "3"`): basic auth with your email + an API token from id.atlassian.com, kept in the OS keychain |
| Jira writes | **Read-only** (`post_comments: false`): Forge never writes to Jira. Questions for the reporter arrive in Teams for the assignee to forward |
| Models | `"auto"` for every agent: Copilot picks the model and no `--model` flag is passed. Escalation is off while it is `auto` |
| Approval | Only you: a decision counts only if the Teams responder is your own email |
| Budget | `team.json → budget`: `daily_tokens` and `monthly_tokens` (0 = off). Set the monthly value from your Copilot allowance once `forge stats` shows real usage |
| Savings minutes | Defaults in `team.json → savings`; only accepted work earns credit (approved analyses, resolved answers, test-verified fixes) |
| Delivery | **Local only** (`delivery.push: false`): an approved fix becomes a verified commit on the local branch `forge/<KEY>`; the engineer reviews and pushes it. Teams gets the branch, commit and a ready PR description (`.forge/PR_BODY.md`). Merges are detected with plain git, squash merges included |
| Security sign-off | Pending: confirm Copilot CLI use on this repo and the OneDrive folder with IT before the pilot |

## Jira connection: keychain or `.env`
The Jira token normally lives in the OS keychain (`forge setup`). Alternatively put a `.env` file in
`%USERPROFILE%\.ai-forge\` (or the folder you run `forge` from, or your home folder):
```
JIRA_BASE_URL=https://yourorg.atlassian.net
JIRA_EMAIL=you@yourorg.com
JIRA_API_TOKEN=your-api-token
```
Values from the environment win over `team.json` / the keychain. `*.atlassian.net` is treated as Cloud
automatically. If `/rest/api/3/search/jql` isn't available on your site, Forge falls back to the classic
`/search` endpoint. `.env` is git-ignored; never put it in the shared folder (it holds your token in plain text).

## Trial on normal open tickets (no support tickets yet)
In `team.json → jira`:
```json
"scope_jql": "project = ABC AND statusCategory != Done AND issuetype = Bug AND updated >= -30d",
"support_issue_types": [],
"support_labels": [],
"assigned_to_me": false
```
Empty type/label lists switch off the "not a support ticket" skip rule; `assigned_to_me: false` takes every
ticket matching `scope_jql`, not only yours. Keep the JQL narrow: each analysed ticket costs Copilot tokens
(`budget.daily_tokens` / `monthly_tokens` stop it at the limit).

To evaluate on **closed** tickets, set `jira.allow_closed_for_eval: true` (or `AI_FORGE_ALLOW_CLOSED_EVAL=1`):
tickets closed in Jira are then analysed instead of being marked resolved. Switch it off for normal use.

## Attachments
Every attachment up to `jira.max_attachment_mb` (default 10) is downloaded to `~/.ai-forge/attachments/<KEY>/`
(local only). Text files and PDFs (via `pypdf`) become **scrubbed, trimmed** text in the context pack; images
and other binaries are described by name and type. The raw files are also copied into the analysis worktree
(`.forge/attachments/`, listed in `job.json`) so Copilot can open a screenshot or the full PDF **only when the
text is not enough**: an attachment it doesn't open costs no tokens. Raw files are not scrubbed; turn this off
with `context.attachment_files: false` if your data rules require it.

## File types in the code index
Built in: common languages, config files, and Vector CANoe: `.can`/`.cin`/`.capl` are parsed as code (C grammar),
`.vsysvar`, `.xvp`, `.vsme`, `.sil`, `.xsd`, `.xsl(t)` as config. Add your own in `team.json → index`:
`"code_ext": {".sin": "c"}`, `"config_ext": [".varsys"]`. Changing a type re-indexes those files on the next run.

## Result labels on info cards
Non-code results say what they are (configuration, environment, data, usage, already known). When the fault is
probably in code **outside this repository**, the agent sets `code_elsewhere` and the card says so, with the likely
owner, instead of offering a fix here.

## Demo without Jira access
Set `"jira": {"mode": "file", "path": "<folder>"}` in `team.json` and drop ticket JSON files into that folder
(samples in `examples/jira-export/`). Comments go to `comments.log` instead of Jira.

## Check that it works (from cmd, no CI and no GitHub needed)
```
cd ai-forge
python -m venv .venv && .venv\Scripts\activate      (macOS/Linux: source .venv/bin/activate)
pip install -e . pytest
python -m pytest -q
```
This runs the whole pipeline (Jira → rules → grouping → cards → approval → fix with fails-before/passes-after
check → local branch → merge detection, plus conflicts and revalidation) against a throwaway git repo, with
stand-ins for Copilot and GitHub. No network, no Copilot usage, no tokens.

## First time
1. Copy this repo into `AI-Forge-Shared/tool/` and run the installer. It seeds `config/team.json` and `agents/`.
2. Fill `config/team.json`: Jira, **model ids**, test commands, `budget`, and the `savings` assumptions used by
   `forge report`. `forge doctor` lists anything missing.
3. Build the Teams flow in `flows/TEAMS_FLOW.md`.
4. To change agent behaviour, edit `AI-Forge-Shared/agents/*.agent.md`; bump `tool/VERSION` after changing code.

## What Copilot gets: `team.json → context`
Every source is computed locally (zero Copilot tokens) and costs pack tokens only when it finds something.
The pack is capped by `thresholds.context_budget_tokens` (default 7000); lower sections are dropped first.

| Setting | Default | What it adds |
|---|---|---|
| `past_tickets` | `true` | Related past tickets, one line each: why it matched, root cause, fix commit (present / reverted / changed since), covering test |
| `max_past_tickets` / `min_past_score` | `3` / `0.3` | How many, and how related they must be. Matching is on the code first: same error (1.0) > past fix touched the same function (0.7–0.9) > same file (0.5–0.65) > similar wording only (0.5 × similarity) |
| `git_ticket_map` | `true` | Learns ticket → files from commit messages that mention a key (`SUP-12: …`), so tickets fixed before Forge count too. Incremental: only new commits are read |
| `recent_changes_days` | `14` | Recent commits on the files the ticket points to, the usual source of regressions (`0` = off) |
| `recent_changes_max_commits` / `recent_changes_diff_lines` | `5` / `20` | How many commits, and how many diff lines each (`0` = one line per commit) |
| `all_comments` | `true` | All ticket comments, not only the reporter's; authors shown by role only |
| `past_evidence` / `past_evidence_min_score` / `past_evidence_max_blocks` | `true` / `0.5` / `2` | For a strongly related past ticket (same file or stronger), the code that proved its cause, re-read from **today's** code (pointers are stored, never old text; deleted code is dropped). Copilot starts where the last investigation ended |
| `learn_gaps` | `true` | After each analysis, records the code Copilot needed (its cited evidence, and with MCP on, what it looked up) and whether the pack already had it. See `forge gaps` and the report |

Past tickets are still used for the duplicate rule and the regression banner when `past_tickets` is off; only the
copy sent to Copilot is dropped.

## How it keeps Copilot honest (and cheap)
| Guard | What happens |
|---|---|
| Citation check | Every file, line range and symbol the analyst cites is checked against the local index. Anything that doesn't exist is shown on the card as "Not found in the code" and counted in `forge report` |
| Latest code before fixing | Right after approval: `git fetch --prune origin`, then a fresh `forge/<KEY>` branch from the latest `origin/main`. If the ticket key already appears in a commit on main, the fix is skipped (no tokens). If the planned files changed since the analysis, the fixer gets the list of changes and must re-check before editing |
| Scope guard | The fixer may only change the plan's files plus tests. An undeclared change elsewhere discards the fix; a declared deviation is delivered but flagged in the title. Functions changed inside allowed files but not named in the plan are listed in the notification and PR description |
| Existing tests first | The fixer is given the test files that already cover the affected code and extends them instead of creating new test modules |
| Test must fail before and pass after | Checked locally, not taken from the agent's word. Test files are identified by path, not by the agent's label |
| No broken tests | If the full suite fails with the fix but passes on the base commit, the fix is discarded |
| Budget | Daily and monthly token limits, checked before every Copilot call |

See `PLAN.md` for the architecture and build instructions.
