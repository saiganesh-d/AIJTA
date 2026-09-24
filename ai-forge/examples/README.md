# Examples

## `jira-export/` – demo or restricted Jira without API access
Set `"jira": {"mode": "file", "path": "<folder>", ...}` in `team.json` (or put files in
`AI-Forge-Shared/jira-export/`). Each file is one ticket in the normalised format shown here.
`assignee` must match your `user_email` for the ticket to be treated as yours; every file also feeds the
team history used for dedup. Comments the runner would post go to `comments.log` in the same folder
instead of Jira, so a live demo never writes to real tickets.

The four samples show the main paths: SUP-101 + SUP-102 share an error signature (one grouped
analysis), SUP-103 routes to the cheap `forge-config` agent, and SUP-104 gets a needs-info comment with no Copilot call.
The code paths in them are fictional: point them at your own repo's real files for a live demo.

## `baseline/` – input for `forge baseline <folder>`
One closed ticket per file, with an `expected` block:
- `classification` – what a human decided (`code_bug`, `configuration`, ...).
- `files` – the files the real fix changed (used for the automatic "root-cause file ok" column).
- `base_commit` – a commit **before** the real fix, so neither approach can see the answer.

Pick about 10 tickets with known root causes. Report the real numbers, including the tickets Forge loses.
