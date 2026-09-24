# AI Forge – Support Ticket Copilot

Jira support tickets → local triage and code index → GitHub Copilot CLI agents → Teams approval → draft PR.
Runs on each engineer's laptop with their own Copilot login. No server, no open ports.

## Install (each teammate, once)
1. Make sure you have Python 3.11+, Git, GitHub CLI (`gh auth login`), and Copilot CLI
   (`npm install -g @github/copilot`, then run `copilot` once and log in).
2. Sync the team SharePoint library so `AI-Forge-Shared` appears in File Explorer, and set it to
   **Always keep on this device**.
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

## Demo without Jira access
Set `"jira": {"mode": "file", "path": "<folder>"}` in `team.json` and drop ticket JSON files into that folder
(samples in `examples/jira-export/`). Comments go to `comments.log` instead of Jira.

## Tests
`pip install -e . pytest && pytest -q`: runs the whole pipeline against a throwaway git repo with fake
`copilot` and `gh` executables (Linux/macOS; no network, no Copilot usage).

## Team lead, first time
1. Create `AI-Forge-Shared` in the team SharePoint library and copy this repo into `AI-Forge-Shared/tool/`.
2. Run the installer yourself. It seeds `config/team.json` and `agents/`.
3. Fill `config/team.json`: Jira, members (with `github` handles for PR reviewers), approvers, **model ids**,
   test commands, and the `savings` assumptions used by `forge report`. `forge doctor` lists anything missing.
4. Build the Teams flow in `flows/TEAMS_FLOW.md`.
5. To update everyone: edit `AI-Forge-Shared/agents/*.agent.md`, or bump `tool/VERSION` after changing code.

See `PLAN.md` for the architecture and build instructions.
