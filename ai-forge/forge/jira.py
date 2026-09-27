"""Jira sync (PLAN §5.3). Server/DC (Bearer PAT, /rest/api/2/search) and Cloud (basic email+token,
/rest/api/3/search/jql, ADF). A `file` mode reads exported issues from a folder, for demos,
tests and teams whose Jira API is not reachable from laptops."""
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from . import config as C
from .signals import error_signature, scrub, trim_log

TEXT_EXT = {".log", ".txt", ".json", ".xml", ".yaml", ".yml", ".csv", ".out", ".err", ".trace", ".properties",
            ".ini", ".conf", ".cfg", ".md", ".stack", ".html"}
MAX_ATTACHMENT = 2 * 1024 * 1024
FIELDS = ["summary", "description", "issuetype", "labels", "components", "priority", "attachment", "comment",
          "updated", "status", "reporter", "fixVersions", "assignee"]
STATUS_CLAUSE = re.compile(
    r'(?i)\s*\b(?:AND|OR)\s+(?:status|statusCategory)\s*(?:!=|=|not\s+in|in)\s*(?:\([^)]*\)|"[^"]*"|\S+)'
    r'|\s*\b(?:status|statusCategory)\s*(?:!=|=|not\s+in|in)\s*(?:\([^)]*\)|"[^"]*"|\S+)\s*(?:AND\s+)?')


def history_jql(scope_jql: str) -> str:
    """Scope JQL without its status filter, e.g. 'project = SUP AND statusCategory != Done' → 'project = SUP'."""
    return STATUS_CLAUSE.sub("", scope_jql).strip() or scope_jql


def adf_to_text(node) -> str:
    """Atlassian Document Format (Cloud v3) → plain text. Keeps code blocks, links and line breaks."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(adf_to_text(c) for c in node)
    if not isinstance(node, dict):
        return str(node)
    t = node.get("type")
    if t == "text":
        return node.get("text", "")
    if t == "hardBreak":
        return "\n"
    if t == "mention":
        return (node.get("attrs") or {}).get("text", "@user")
    if t in ("inlineCard", "blockCard"):
        return (node.get("attrs") or {}).get("url", "")
    inner = "".join(adf_to_text(c) for c in node.get("content", []) or [])
    if t in ("paragraph", "heading", "blockquote", "listItem", "tableRow"):
        return inner + "\n"
    if t == "codeBlock":
        return f"\n```\n{inner}\n```\n"
    return inner


def jql_time(ts: str, skew_minutes: int = 30) -> str:
    """ISO timestamp → JQL date literal (interpreted in the Jira user's time zone). Starts 30 min earlier
    to absorb clock/time-zone skew; re-fetched unchanged tickets cost nothing (no download, no write)."""
    d = datetime.fromisoformat(ts) - timedelta(minutes=skew_minutes)
    return d.strftime("%Y/%m/%d %H:%M")


class Jira:
    def __init__(self, cfg: C.Config, token: str | None = None, transport=None):
        j = cfg.team.get("jira") or {}
        self.cfg = cfg
        self.base = (os.environ.get("JIRA_BASE_URL") or j.get("base_url") or "").rstrip("/")
        self.cloud = (str(j.get("api_version", "2")) == "3" or j.get("deployment") == "cloud"
                      or "atlassian.net" in self.base.lower())
        self.sprint_field = j.get("sprint_field")
        self.user_email = C.jira_email(cfg)
        token = (token if token is not None else C.jira_token(self.user_email) or "").strip()
        auth = (self.user_email, token) if self.cloud else None
        headers = {"Accept": "application/json"}
        if not self.cloud:
            headers["Authorization"] = f"Bearer {token}"
        self.http = httpx.Client(base_url=self.base, headers=headers, auth=auth, timeout=30, transport=transport,
                                 verify=j.get("verify_tls", True))
        self.requests = 0

    def _get(self, path: str, **params):
        self.requests += 1
        r = self.http.get(path, params=params)
        r.raise_for_status()
        return r.json()

    def myself(self) -> dict:
        return self._get(f"/rest/api/{'3' if self.cloud else '2'}/myself")

    def search(self, jql: str, max_results: int = 100) -> list[dict]:
        """Cloud: /rest/api/3/search/jql (nextPageToken; startAt if the site returns `total`). Sites where
        that endpoint is missing (HTTP 400/404/405) and Server/DC use the classic /search with startAt."""
        fields = ",".join(FIELDS + ([self.sprint_field] if self.sprint_field else []))
        out: list[dict] = []
        if self.cloud:
            token = None
            try:
                while True:
                    params = {"jql": jql, "maxResults": max_results, "fields": fields}
                    if token:
                        params["nextPageToken"] = token
                    data = self._get("/rest/api/3/search/jql", **params)
                    issues = data.get("issues", [])
                    out += issues
                    token = data.get("nextPageToken")
                    if token:
                        continue
                    if data.get("isLast") or not issues or "total" not in data:
                        return out
                    start = len(out)
                    while start < data.get("total", 0):
                        more = self._get("/rest/api/3/search/jql", jql=jql, startAt=start,
                                         maxResults=max_results, fields=fields).get("issues", [])
                        if not more:
                            break
                        out += more
                        start += len(more)
                    return out
            except httpx.HTTPStatusError as e:
                if e.response.status_code not in (400, 404, 405):
                    raise
                out = []  # fall back to the classic endpoint below
        path = f"/rest/api/{'3' if self.cloud else '2'}/search"
        start = 0
        while True:
            data = self._get(path, jql=jql, startAt=start, maxResults=max_results, fields=fields)
            issues = data.get("issues", [])
            out += issues
            start += len(issues)
            if not issues or start >= data.get("total", 0):
                return out

    def add_comment(self, key: str, text: str) -> None:
        self.requests += 1
        lines = [line for line in text.split("\n") if line.strip()]
        if self.cloud:
            content = [{"type": "paragraph", "content": [{"type": "text", "text": line}]} for line in lines]
            if not content:
                content = [{"type": "paragraph", "content": [{"type": "text", "text": text or " "}]}]
            body = {"body": {"type": "doc", "version": 1, "content": content}}
            r = self.http.post(f"/rest/api/3/issue/{key}/comment", json=body)
        else:
            r = self.http.post(f"/rest/api/2/issue/{key}/comment", json={"body": text})
        r.raise_for_status()

    def download(self, url: str) -> bytes:
        self.requests += 1
        r = self.http.get(url, follow_redirects=True)
        r.raise_for_status()
        return r.content[:MAX_ATTACHMENT]

    # ---------- normalisation ----------
    def text(self, v) -> str:
        return adf_to_text(v) if isinstance(v, (dict, list)) else (v or "")

    @staticmethod
    def _ids(person) -> set:
        if isinstance(person, dict):
            return {x for x in (person.get("emailAddress"), person.get("name"), person.get("accountId")) if x}
        return {str(person)} if person else set()

    @staticmethod
    def _name(v, key: str = "name"):
        return v.get(key) if isinstance(v, dict) else v

    def normalize(self, issue: dict) -> dict:
        """Raw REST issue → Forge ticket dict. Tolerates plain strings where Jira usually sends objects,
        and passes through issues that are already normalised (e.g. from an export script)."""
        f = issue.get("fields") or {}
        if not f and "summary" in issue:
            t = dict(issue)
            for k in ("attachments", "comments", "labels"):
                t.setdefault(k, [])
            return t
        comps = [c.get("name") if isinstance(c, dict) else str(c) for c in f.get("components") or [] if c]
        rep = f.get("reporter") or {}
        rep_ids = self._ids(rep)
        reporter = (rep.get("emailAddress") or rep.get("name") or rep.get("accountId") or rep.get("displayName") or ""
                    ) if isinstance(rep, dict) else str(rep)
        asg = f.get("assignee") or {}
        assignee = (asg.get("emailAddress") or asg.get("name") or asg.get("accountId") or asg.get("displayName") or ""
                    ) if isinstance(asg, dict) else str(asg)
        comments, last_rep = [], None
        raw_comments = (f.get("comment") or {}).get("comments") or [] if isinstance(f.get("comment"), dict) \
            else (f.get("comment") or [])
        for c in raw_comments:
            if not isinstance(c, dict):
                continue
            a = c.get("author") or {}
            is_rep = bool(rep_ids & self._ids(a))
            author = (a.get("displayName") or a.get("name") or "?") if isinstance(a, dict) else str(a)
            comments.append({"author": author, "created": c.get("created"), "by_reporter": is_rep,
                             "body": scrub(self.text(c.get("body")))[:1500]})
            if is_rep and c.get("created"):
                last_rep = max(filter(None, (last_rep, c.get("created"))))
        atts = []
        for a in f.get("attachment") or []:
            if not isinstance(a, dict):
                continue
            by_rep = bool(rep_ids & self._ids(a.get("author") or {}))
            atts.append({"name": a.get("filename"), "size": a.get("size", 0), "url": a.get("content"),
                         "created": a.get("created"), "mime": a.get("mimeType", ""), "by_reporter": by_rep})
            if by_rep and a.get("created"):
                last_rep = max(filter(None, (last_rep, a.get("created"))))
        sprint = None
        if self.sprint_field and f.get(self.sprint_field):
            sv = f[self.sprint_field]
            last = sv[-1] if isinstance(sv, list) else sv
            sprint = last.get("name") if isinstance(last, dict) else (re.search(r"name=([^,\]]+)", str(last)) or [None, str(last)])[1]
        status = f.get("status") or {}
        status_name = self._name(status) or ""
        status_cat = (status.get("statusCategory") or {}) if isinstance(status, dict) else {}
        return {
            "key": issue.get("key", ""), "summary": f.get("summary") or "", "description": self.text(f.get("description")),
            "type": self._name(f.get("issuetype") or {}), "labels": f.get("labels") or [],
            "component": comps[0] if comps else None, "priority": self._name(f.get("priority") or {}),
            "sprint": sprint, "reporter": reporter, "assignee": assignee,
            "project": self._name(f.get("project") or {}, "key"), "created": f.get("created") or "",
            "updated": f.get("updated") or "", "jira_status": status_name,
            "done": status_cat.get("key") == "done" or str(status_name).lower() in ("closed", "done", "resolved", "cancelled"),
            "fix_versions": [self._name(v) for v in f.get("fixVersions") or []],
            "attachments": atts, "comments": comments, "last_reporter_activity": last_rep,
        }


class FileJira:
    """Reads normalised or raw Jira issue JSON files from <shared>/jira-export or team.jira.path."""

    def __init__(self, cfg: C.Config):
        j = cfg.team.get("jira") or {}
        self.dir = Path(j.get("path") or cfg.shared / "jira-export")
        self.me = cfg.user_email.lower()
        self.only_mine = j.get("assigned_to_me", True)
        self.requests = 0
        self.comments: list[tuple[str, str]] = []
        self.cloud = False

    def _all(self) -> list[dict]:
        out = []
        for p in sorted(self.dir.glob("*.json")):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except ValueError:
                continue
        return out

    # Files are cheap to re-read, and exported timestamps can be older than the last run,
    # so file mode ignores `since`; unchanged tickets are skipped by the `updated` check.
    def mine(self, since: str) -> list[dict]:
        self.requests += 1
        return [t for t in self._all() if not self.only_mine or (t.get("assignee") or "").lower() == self.me]

    def team(self, since: str) -> list[dict]:
        self.requests += 1
        return self._all()

    def add_comment(self, key: str, text: str) -> None:
        self.comments.append((key, text))
        log = self.dir / "comments.log"
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"--- {key} {datetime.now().isoformat(timespec='seconds')}\n{text}\n")

    def normalize(self, t: dict) -> dict:
        t = dict(t)
        t.setdefault("attachments", [])
        t.setdefault("comments", [])
        t.setdefault("labels", [])
        return t


def client(cfg: C.Config):
    return FileJira(cfg) if (cfg.team.get("jira") or {}).get("mode") == "file" else Jira(cfg)


def fetch_attachments(jira, t: dict, dest_root: Path) -> list[tuple[str, str]]:
    """Download text-like attachments (≤2 MB), keep only trimmed + scrubbed text. Images/binaries by name."""
    out = []
    for a in t.get("attachments", []):
        name = a.get("name") or "attachment"
        if "text" in a:  # file mode / already fetched
            out.append((name, a["text"]))
            continue
        ext = Path(name).suffix.lower()
        if ext not in TEXT_EXT or (a.get("size") or 0) > MAX_ATTACHMENT or not a.get("url"):
            out.append((name, f"(binary or large attachment, {a.get('size', 0)} bytes, not downloaded)"))
            continue
        d = dest_root / t["key"]
        d.mkdir(parents=True, exist_ok=True)
        cached = d / (re.sub(r"[^\w.-]", "_", name) + ".trimmed.txt")
        if not cached.exists():
            try:
                raw = jira.download(a["url"]).decode("utf-8", errors="replace")
            except httpx.HTTPError as e:
                out.append((name, f"(download failed: {e.__class__.__name__})"))
                continue
            cached.write_text(trim_log(scrub(raw)), encoding="utf-8")
        out.append((name, cached.read_text(encoding="utf-8")))
    return out


def ticket_text(t: dict) -> str:
    atts = "\n".join(txt for _, txt in t.get("attachment_texts", []))
    return f"{t.get('summary', '')}\n{t.get('description', '')}\n{atts}"


# Closed in Jira before a fix started: stop waiting. Fixes in progress / open PRs keep going.
CLOSABLE = {"new", "cooling", "ready", "needs_info", "awaiting_decision", "info_sent", "duplicate", "analyze_failed",
            "plan_invalid", "conflict_wait"}
REENTER = {"needs_info", "skipped", "duplicate", "resolved", "info_sent", "rejected", "analyze_failed"}


def _prepare(jira, t: dict, cfg: C.Config) -> dict:
    t["attachments"] = [{"name": n, "text": x} for n, x in fetch_attachments(jira, t, C.HOME / "attachments")]
    t["signature"] = error_signature(f"{t.get('description', '')}\n" + "\n".join(a["text"] for a in t["attachments"]))
    return t


def sync(cfg: C.Config, store, jira, log=print) -> dict:
    """Pull my changed tickets (every run) and team history (hourly). Returns counts."""
    stats = {"new": 0, "updated": 0, "reentered": 0, "history": 0, "closed": 0}
    started = datetime.now().isoformat(timespec="seconds")
    last = store.get_state("mine_last")
    scope = ((cfg.team.get("jira") or {}).get("scope_jql") or "").strip()
    if isinstance(jira, FileJira):
        issues = [jira.normalize(i) for i in jira.mine(last)]
    else:
        # assigned_to_me: false → every ticket matching scope_jql (e.g. a trial on a project's open tickets)
        parts = [f"({scope})"] if scope else []
        if (cfg.team.get("jira") or {}).get("assigned_to_me", True):
            parts.append("assignee = currentUser()")
        if last:
            parts.append(f'updated >= "{jql_time(last)}"')
        jql = " AND ".join(parts) or "created is not null"
        issues = [jira.normalize(i) for i in jira.search(jql + " ORDER BY updated ASC")]
    for t in issues:
        old = store.ticket(t["key"])
        if old and old["updated"] == t["updated"]:
            continue  # no download, no write
        res = store.upsert_ticket(_prepare(jira, t, cfg))
        stats[res] = stats.get(res, 0) + 1
        if res == "updated" and old and old["status"] in REENTER:
            since = old.get("analyzed_at") or old.get("status_changed") or ""
            rep = t.get("last_reporter_activity") or ""
            if old["status"] == "skipped" or (rep and rep > since):
                store.set_status(t["key"], "new", "reporter update after " + old["status"], route=None)
                stats["reentered"] += 1
    if issues:
        store.set_state("mine_last", started)

    hist_last = store.get_state("history_last")
    due = not hist_last or datetime.fromisoformat(hist_last) < datetime.now() - timedelta(minutes=60)
    if due:
        if isinstance(jira, FileJira):
            team = [jira.normalize(i) for i in jira.team(hist_last)]
        else:
            parts = [f"({history_jql(scope)})"] if scope else []
            if hist_last:
                parts.append(f'updated >= "{jql_time(hist_last)}"')
            jql = " AND ".join(parts) or "created is not null"
            team = [jira.normalize(i) for i in jira.search(jql)]
        for t in team:
            t["signature"] = error_signature(t.get("description", ""))
            stats["history"] += store.upsert_history(t)
            mine = store.ticket(t["key"])
            if t.get("done") and mine and mine["status"] in CLOSABLE:
                store.set_status(t["key"], "resolved", "closed in Jira")
                store.close_requests_for(t["key"])
                stats["closed"] += 1
        store.set_state("history_last", started)
    return stats
