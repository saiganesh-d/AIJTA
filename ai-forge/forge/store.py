"""Local SQLite cache (~/.ai-forge/forge.db): my tickets, team history, groups, pending card requests.

Every status change is one transaction plus an `events` row, so a runner killed at any point
can simply run again. Nothing here is shared; the shared folder only gets derived results."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS tickets(
  key TEXT PRIMARY KEY, summary TEXT, description TEXT, type TEXT, component TEXT, priority TEXT,
  sprint TEXT, reporter TEXT, labels_json TEXT DEFAULT '[]', updated TEXT, jira_status TEXT,
  status TEXT NOT NULL DEFAULT 'new', status_changed TEXT, first_seen TEXT,
  route TEXT, route_reason TEXT, group_id TEXT, signature TEXT,
  attachments_json TEXT DEFAULT '[]', comments_json TEXT DEFAULT '[]', raw_json TEXT,
  analyzed_at TEXT, last_reporter_activity TEXT, request_id TEXT,
  reviewer_note TEXT, base_branch TEXT, blocked_on TEXT, branch TEXT, pr_url TEXT,
  tokens INT DEFAULT 0, revalidated_json TEXT DEFAULT '[]', plan_json TEXT);
CREATE TABLE IF NOT EXISTS history(
  key TEXT PRIMARY KEY, summary TEXT, description TEXT, component TEXT, sprint TEXT,
  signature TEXT, updated TEXT, jira_status TEXT, fix_versions TEXT);
CREATE TABLE IF NOT EXISTS sync_state(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS groups(
  group_id TEXT PRIMARY KEY, tickets_json TEXT, route TEXT, reasons_json TEXT, analysis_json TEXT,
  agent TEXT, model TEXT, tokens_json TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS pending_requests(
  request_id TEXT PRIMARY KEY, ticket_key TEXT, kind TEXT, created TEXT, reposted INT DEFAULT 0,
  payload_json TEXT);
CREATE TABLE IF NOT EXISTS events(ts TEXT, key TEXT, event TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS ix_events_event ON events(event);
"""

STATUSES = {"new", "cooling", "ready", "analyzing", "awaiting_decision", "approved", "fixing", "fix_ready", "pr_open", "merged",
            "skipped", "duplicate", "needs_info", "info_sent", "resolved", "rejected", "fix_failed",
            "plan_invalid", "analyze_failed", "conflict_wait"}
# Terminal states can only be left by a new Jira update (sync) or an explicit decision.
TERMINAL = {"skipped", "duplicate", "resolved", "rejected", "merged"}
ACTIVE = {"analyzing", "fixing"}  # interrupted mid-way → back to the state before on restart


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(path), isolation_level=None, timeout=30)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)

    @contextmanager
    def tx(self):
        self.con.execute("BEGIN IMMEDIATE")
        try:
            yield self.con
            self.con.execute("COMMIT")
        except BaseException:
            self.con.execute("ROLLBACK")
            raise

    # ---------- sync state ----------
    def get_state(self, k: str, default: str = "") -> str:
        row = self.con.execute("SELECT v FROM sync_state WHERE k=?", (k,)).fetchone()
        return row["v"] if row else default

    def set_state(self, k: str, v: str) -> None:
        self.con.execute("INSERT OR REPLACE INTO sync_state(k, v) VALUES (?,?)", (k, v))

    # ---------- tickets ----------
    def ticket(self, key: str) -> dict | None:
        row = self.con.execute("SELECT * FROM tickets WHERE key=?", (key,)).fetchone()
        return self._decode(row) if row else None

    def tickets(self, *statuses: str) -> list[dict]:
        if statuses:
            q = f"SELECT * FROM tickets WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY key"
            rows = self.con.execute(q, statuses).fetchall()
        else:
            rows = self.con.execute("SELECT * FROM tickets ORDER BY key").fetchall()
        return [self._decode(r) for r in rows]

    @staticmethod
    def _decode(row) -> dict:
        d = dict(row)
        for k in ("labels", "attachments", "comments", "revalidated"):
            d[k] = json.loads(d.pop(f"{k}_json") or "[]")
        d["plan"] = json.loads(d.pop("plan_json") or "null")
        return d

    def upsert_ticket(self, t: dict) -> str:
        """Insert or update from Jira. Returns 'new', 'updated' or 'unchanged' (no write)."""
        old = self.con.execute("SELECT updated, status FROM tickets WHERE key=?", (t["key"],)).fetchone()
        if old and old["updated"] == t["updated"]:
            return "unchanged"
        cols = dict(summary=t.get("summary", ""), description=t.get("description", ""), type=t.get("type"),
                    component=t.get("component"), priority=t.get("priority"), sprint=t.get("sprint"),
                    reporter=t.get("reporter"), labels_json=json.dumps(t.get("labels", [])),
                    updated=t["updated"], jira_status=t.get("jira_status"),
                    attachments_json=json.dumps(t.get("attachments", [])),
                    comments_json=json.dumps(t.get("comments", [])), raw_json=json.dumps(t.get("raw") or {}),
                    signature=t.get("signature"), last_reporter_activity=t.get("last_reporter_activity"))
        with self.tx() as con:
            if old:
                con.execute(f"UPDATE tickets SET {', '.join(f'{k}=?' for k in cols)} WHERE key=?",
                            (*cols.values(), t["key"]))
                self._event(con, t["key"], "jira_updated")
                return "updated"
            cols.update(key=t["key"], status="new", status_changed=now(), first_seen=now())
            con.execute(f"INSERT INTO tickets({', '.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                        tuple(cols.values()))
            self._event(con, t["key"], "status:new")
            return "new"

    def set_status(self, keys, status: str, detail: str = "", **fields) -> None:
        """Atomic status transition (plus optional column updates) for one or many tickets."""
        if status not in STATUSES:
            raise ValueError(f"unknown status {status}")
        keys = [keys] if isinstance(keys, str) else list(keys)
        for k, v in list(fields.items()):
            if k in ("labels", "attachments", "comments", "revalidated", "plan"):
                fields[f"{k}_json"] = json.dumps(fields.pop(k))
        sets = ", ".join(["status=?", "status_changed=?"] + [f"{k}=?" for k in fields])
        with self.tx() as con:
            for key in keys:
                con.execute(f"UPDATE tickets SET {sets} WHERE key=?", (status, now(), *fields.values(), key))
                self._event(con, key, f"status:{status}", detail)

    def update(self, key: str, **fields) -> None:
        for k, v in list(fields.items()):
            if k in ("labels", "attachments", "comments", "revalidated", "plan"):
                fields[f"{k}_json"] = json.dumps(fields.pop(k))
        with self.tx() as con:
            con.execute(f"UPDATE tickets SET {', '.join(f'{k}=?' for k in fields)} WHERE key=?",
                        (*fields.values(), key))

    def recover_interrupted(self) -> list[str]:
        """A killed run can leave tickets in analyzing/fixing: put them back so the stage reruns."""
        back = {"analyzing": "ready", "fixing": "approved"}
        moved = []
        for t in self.tickets(*ACTIVE):
            self.set_status(t["key"], back[t["status"]], "recovered after interrupted run")
            moved.append(t["key"])
        return moved

    # ---------- history (team tickets, used for dedup hints only) ----------
    def upsert_history(self, t: dict) -> bool:
        old = self.con.execute("SELECT updated FROM history WHERE key=?", (t["key"],)).fetchone()
        if old and old["updated"] == t["updated"]:
            return False
        self.con.execute("INSERT OR REPLACE INTO history(key, summary, description, component, sprint, signature,"
                         " updated, jira_status, fix_versions) VALUES (?,?,?,?,?,?,?,?,?)",
                         (t["key"], t.get("summary", ""), (t.get("description") or "")[:4000], t.get("component"),
                          t.get("sprint"), t.get("signature"), t["updated"], t.get("jira_status"),
                          ",".join(t.get("fix_versions") or [])))
        return True

    def history(self) -> list[dict]:
        return [dict(r) for r in self.con.execute("SELECT * FROM history")]

    # ---------- groups ----------
    def save_group(self, group_id: str, keys: list[str], route: str, reasons: list[str]) -> None:
        self.con.execute("INSERT OR REPLACE INTO groups(group_id, tickets_json, route, reasons_json, created)"
                         " VALUES (?,?,?,?,?)", (group_id, json.dumps(keys), route, json.dumps(reasons), now()))

    def group(self, group_id: str) -> dict | None:
        r = self.con.execute("SELECT * FROM groups WHERE group_id=?", (group_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["tickets"] = json.loads(d["tickets_json"] or "[]")
        d["reasons"] = json.loads(d["reasons_json"] or "[]")
        d["analysis"] = json.loads(d["analysis_json"] or "null")
        d["tokens"] = json.loads(d["tokens_json"] or "{}")
        return d

    def save_group_result(self, group_id: str, analysis: dict | None, agent: str, model: str, tokens: dict) -> None:
        self.con.execute("UPDATE groups SET analysis_json=?, agent=?, model=?, tokens_json=? WHERE group_id=?",
                         (json.dumps(analysis), agent, model, json.dumps(tokens), group_id))

    # ---------- card requests ----------
    def add_request(self, request_id: str, key: str, kind: str, payload: dict) -> None:
        with self.tx() as con:
            con.execute("INSERT INTO pending_requests(request_id, ticket_key, kind, created, payload_json)"
                        " VALUES (?,?,?,?,?)", (request_id, key, kind, now(), json.dumps(payload)))
            con.execute("UPDATE tickets SET request_id=? WHERE key=?", (request_id, key))

    def pending(self, request_id: str | None = None) -> list[dict]:
        if request_id:
            rows = self.con.execute("SELECT * FROM pending_requests WHERE request_id=?", (request_id,)).fetchall()
        else:
            rows = self.con.execute("SELECT * FROM pending_requests").fetchall()
        return [{**dict(r), "payload": json.loads(r["payload_json"] or "{}")} for r in rows]

    def close_request(self, request_id: str) -> None:
        self.con.execute("DELETE FROM pending_requests WHERE request_id=?", (request_id,))

    def close_requests_for(self, key: str) -> None:
        self.con.execute("DELETE FROM pending_requests WHERE ticket_key=?", (key,))

    # ---------- events / metrics ----------
    @staticmethod
    def _event(con, key: str, event: str, detail: str = "") -> None:
        con.execute("INSERT INTO events(ts, key, event, detail) VALUES (?,?,?,?)", (now(), key, event, detail))

    def event(self, key: str, event: str, detail: str = "") -> None:
        self._event(self.con, key, event, detail)

    def counts(self, since: str = "") -> dict:
        """Distinct tickets per outcome since a timestamp (for heartbeats, digests and the savings report)."""
        rows = self.con.execute("SELECT event, COUNT(DISTINCT key) FROM events WHERE ts >= ? GROUP BY event",
                                (since,)).fetchall()
        return {r[0]: r[1] for r in rows}
