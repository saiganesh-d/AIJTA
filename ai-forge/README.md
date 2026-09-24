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
| `forge doctor --live` | health check + detects whether MCP works with agents on your CLI version |
| `forge run --force` | run one cycle now |
| `forge search "text"` | search the code index |
| `forge context ticket.txt` | show the context pack Copilot would get, with its token estimate |
| `forge index --full` | rebuild the code index |
| `forge stats` | Copilot calls and tokens per day and agent |

## Team lead, first time
1. Create `AI-Forge-Shared` in the team SharePoint library and copy this repo into `AI-Forge-Shared/tool/`.
2. Run the installer yourself. It seeds `config/team.json` and `agents/`.
3. Fill `config/team.json`: Jira, members, approvers, **model ids**, and test commands.
4. Build the Teams flow in `flows/TEAMS_FLOW.md`.
5. To update everyone: edit `AI-Forge-Shared/agents/*.agent.md`, or bump `tool/VERSION` after changing code.

See `PLAN.md` for the architecture and build instructions.
