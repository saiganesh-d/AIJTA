"""Code history as retrieval signals, computed locally from git (zero Copilot tokens):

- ticket map: which files each ticket's fix commits touched, learned from commit messages that mention
  a ticket key ("SUP-12: ..."). Covers every ticket ever fixed, not only the ones Forge analysed.
- recent changes: what changed lately in the code a new ticket points to (the usual regression source)."""
import re
import sqlite3
import subprocess

KEY = re.compile(r"\b[A-Z][A-Z0-9_]+-\d+\b")
SCHEMA = """
CREATE TABLE IF NOT EXISTS commit_tickets(key TEXT, sha TEXT, day TEXT, subject TEXT, file TEXT);
CREATE INDEX IF NOT EXISTS ix_ct_file ON commit_tickets(file);
CREATE INDEX IF NOT EXISTS ix_ct_key ON commit_tickets(key);
CREATE TABLE IF NOT EXISTS commit_map_meta(k TEXT PRIMARY KEY, v TEXT);
"""


def _git(repo: str, *args) -> tuple[int, str]:
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode, r.stdout


def _con(db_path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def sync_ticket_map(repo: str, ref: str, db_path) -> int:
    """Incremental: only commits since the last sync are read. Reverts are skipped (they mention the key
    but undo the fix). Returns the number of (ticket, file) rows added."""
    con = _con(db_path)
    last = (con.execute("SELECT v FROM commit_map_meta WHERE k='last'").fetchone() or [None])[0]
    head = _git(repo, "rev-parse", ref)[1].strip()
    if not head or head == last:
        return 0
    incremental = last and _git(repo, "merge-base", "--is-ancestor", last, head)[0] == 0
    if not incremental:
        con.execute("DELETE FROM commit_tickets")
    rng = f"{last}..{head}" if incremental else head
    _, out = _git(repo, "log", rng, "--no-merges", "--date=short", "--format=@@%H|%ad|%s", "--name-only")
    rows, cur = [], None
    for line in out.splitlines():
        if line.startswith("@@"):
            sha, day, subject = (line[2:].split("|", 2) + ["", ""])[:3]
            keys = [] if subject.startswith('Revert "') else sorted(set(KEY.findall(subject)))
            cur = (sha[:12], day, subject[:160], keys)
        elif line.strip() and cur and cur[3]:
            rows += [(k, cur[0], cur[1], cur[2], line.strip()) for k in cur[3]]
    con.executemany("INSERT INTO commit_tickets(key, sha, day, subject, file) VALUES (?,?,?,?,?)", rows)
    con.execute("INSERT OR REPLACE INTO commit_map_meta(k, v) VALUES ('last', ?)", (head,))
    con.commit()
    return len(rows)


def fixes_touching(db_path, files: list[str]) -> dict[str, dict]:
    """{ticket key: {sha, day, subject, files}} for past fix commits that touched any of `files`."""
    if not files:
        return {}
    con = _con(db_path)
    marks = ",".join("?" * len(files))
    out: dict[str, dict] = {}
    for r in con.execute(f"SELECT key, sha, day, subject, file FROM commit_tickets WHERE file IN ({marks}) "
                         "ORDER BY day DESC", files):
        e = out.setdefault(r["key"], {"sha": r["sha"], "day": r["day"], "subject": r["subject"], "files": []})
        if r["file"] not in e["files"]:
            e["files"].append(r["file"])
    return out


def recent_changes(repo: str, ref: str, files: list[str], days: int, max_commits: int = 5,
                   diff_lines: int = 20) -> list[str]:
    """One entry per recent commit on `files`: '<sha> <date> <author>: <subject> [files]' plus a short diff."""
    if not files or days <= 0:
        return []
    rc, out = _git(repo, "log", ref, f"--since={days}.days", "--no-merges", f"-n{max_commits}", "--date=short",
                   "--format=@@%h|%ad|%an|%s", "--name-only", "--", *files)
    if rc != 0:
        return []
    entries, cur = [], None
    for line in out.splitlines():
        if line.startswith("@@"):
            sha, day, who, subject = (line[2:].split("|", 3) + ["", "", ""])[:4]
            cur = {"sha": sha, "head": f"{sha} {day} {who}: {subject[:120]}", "files": []}
            entries.append(cur)
        elif line.strip() and cur is not None:
            cur["files"].append(line.strip())
    lines = []
    for e in entries:
        text = f"{e['head']} [{', '.join(e['files'][:4])}]"
        if diff_lines > 0:
            _, diff = _git(repo, "show", "-U1", "--format=", e["sha"], "--", *files)
            body = [l for l in diff.splitlines() if l[:1] in "+-@" and not l.startswith(("+++", "---"))]
            if body:
                text += "\n```diff\n" + "\n".join(body[:diff_lines]) + ("\n…" if len(body) > diff_lines else "") + "\n```"
        lines.append(text)
    return lines
