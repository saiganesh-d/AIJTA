"""Jira sync (PLAN §5.3). Server/DC (Bearer PAT, /rest/api/2/search) and Cloud (basic email+token,
/rest/api/3/search/jql, ADF). A `file` mode reads exported issues from a folder, for demos,
tests and teams whose Jira API is not reachable from laptops."""
import json
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
    """Atlassian Document Format (Cloud v3) → plain text. Keeps code blocks and line breaks."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    t = node.get("type")
    if t == "text":
        return node.get("text", "")
    if t == "hardBreak":
        return "\n"
    if t == "mention":
        return (node.get("attrs") or {}).get("text", "@user")
    inner = "".join(adf_to_text(c) for c in node.get("content", []) or [])
    if t in ("paragraph", "heading", "blockquote", "listItem"):
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
        self.base = j.get("base_url", "").rstrip("/")
        self.cloud = str(j.get("api_version", "2")) == "3" or j.get("deployment") == "cloud"
        self.sprint_field = j.get("sprint_field")
        token = token if token is not None else C.jira_token(cfg.user_email)
        auth = (cfg.user_email, token or "") if self.cloud else None
        headers = {"Accept": "application/json"}
        if not self.cloud:
            headers["Authorization"] = f"Bearer {token or ''}"
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
        fields = FIELDS + ([self.sprint_field] if self.sprint_field else [])
        out = []
        if self.cloud:
            token = None
            while True:
                params = {"jql": jql, "maxResults": max_results, "fields": ",".join(fields)}
                if token:
                    params["nextPageToken"] = token
                data = self._get("/rest/api/3/search/jql", **params)
                out += data.get("issues", [])
                token = data.get("nextPageToken")
                if not token or data.get("isLast"):
                    return out
        start = 0
        while True:
            data = self._get("/rest/api/2/search", jql=jql, startAt=start, maxResults=max_results,
                             fields=",".join(fields))
            issues = data.get("issues", [])
            out += issues
            start += len(issues)
            if not issues or start >= data.get("total", 0):
                return out

    def add_comment(self, key: str, text: str) -> None:
        self.requests += 1
        if self.cloud:
            body = {"body": {"type": "doc", "version": 1, "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": line}]} for line in text.split("\n") if line]}}
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
        return adf_to_text(v) if isinstance(v, dict) else (v or "")

    def normalize(self, issue: dict) -> dict:
        f = issue.get("fields") or {}
        comps = [c.get("name") for c in f.get("components") or [] if c.get("name")]
        rep = f.get("reporter") or {}
        reporter = rep.get("emailAddress") or rep.get("name") or rep.get("accountId") or ""
        rep_ids = {x for x in (rep.get("emailAddress"), rep.get("name"), rep.get("accountId")) if x}
        comments, last_rep = [], None
        for c in ((f.get("comment") or {}).get("comments") or []):
            a = c.get("author") or {}
            is_rep = bool(rep_ids & {a.get("emailAddress"), a.get("name"), a.get("accountId")})
            comments.append({"author": a.get("displayName") or a.get("name") or "?", "created": c.get("created"),
                             "by_reporter": is_rep, "body": scrub(self.text(c.get("body")))[:1500]})
            if is_rep:
                last_rep = max(filter(None, (last_rep, c.get("created"))))
        atts = []
        for a in f.get("attachment") or []:
            atts.append({"name": a.get("filename"), "size": a.get("size", 0), "url": a.get("content"),
                         "created": a.get("created"), "mime": a.get("mimeType", ""),
                         "by_reporter": bool(rep_ids & {(a.get("author") or {}).get(k) for k in ("emailAddress", "name", "accountId")})})
            if atts[-1]["by_reporter"]:
                last_rep = max(filter(None, (last_rep, a.get("created"))))
        sprint = None
        if self.sprint_field and f.get(self.sprint_field):
            sv = f[self.sprint_field]
            last = sv[-1] if isinstance(sv, list) else sv
            sprint = last.get("name") if isinstance(last, dict) else (re.search(r"name=([^,\]]+)", str(last)) or [None, str(last)])[1]
        desc = self.text(f.get("description"))
        return {
            "key": issue["key"], "summary": f.get("summary") or "", "description": desc,
            "type": (f.get("issuetype") or {}).get("name"), "labels": f.get("labels") or [],
            "component": comps[0] if comps else None, "priority": (f.get("priority") or {}).get("name"),
            "sprint": sprint, "reporter": reporter, "updated": f.get("updated") or "",
            "jira_status": (f.get("status") or {}).get("name"),
            "done": ((f.get("status") or {}).get("statusCategory") or {}).get("key") == "done",
            "fix_versions": [v.get("name") for v in f.get("fixVersions") or []],
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
    scope = (cfg.team.get("jira") or {}).get("scope_jql", "")
    if isinstance(jira, FileJira):
        issues = [jira.normalize(i) for i in jira.mine(last)]
    else:
        # assigned_to_me: false → every ticket matching scope_jql (e.g. a trial on a project's open tickets)
        mine = " AND assignee = currentUser()" if (cfg.team.get("jira") or {}).get("assigned_to_me", True) else ""
        jql = f"({scope}){mine}" + (f' AND updated >= "{jql_time(last)}"' if last else "")
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
            jql = history_jql(scope) + (f' AND updated >= "{jql_time(hist_last)}"' if hist_last else "")
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
