"""SQLite code index: symbols, call graph and an FTS5 search table. Zero external services."""
import sqlite3
import subprocess
from pathlib import Path

from ..signals import subwords

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, sha TEXT, lang TEXT, lines INT);
CREATE TABLE IF NOT EXISTS symbols(
  id INTEGER PRIMARY KEY, path TEXT, name TEXT, qualname TEXT, kind TEXT,
  start INT, end INT, signature TEXT);
CREATE INDEX IF NOT EXISTS ix_sym_name ON symbols(name);
CREATE INDEX IF NOT EXISTS ix_sym_path ON symbols(path);
CREATE TABLE IF NOT EXISTS calls(caller INTEGER, callee_name TEXT);
CREATE INDEX IF NOT EXISTS ix_calls_callee ON calls(callee_name);
CREATE INDEX IF NOT EXISTS ix_calls_caller ON calls(caller);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(symbol_id UNINDEXED, kind UNINDEXED, path, qualname, terms);
"""

CODE_KINDS = ("function", "class")


def connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def _fts(terms: list[str]) -> str:
    return " OR ".join(f'"{t}"' for t in terms[:24])


class Index:
    def __init__(self, db_path: Path, repo_path: str):
        self.con = connect(db_path)
        self.repo = repo_path
        self._blob_cache: dict[str, list[str]] = {}

    # ---------- meta ----------
    def meta(self, key: str, default: str = "") -> str:
        row = self.con.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        return row["v"] if row else default

    # ---------- source ----------
    def _lines(self, path: str) -> list[str]:
        row = self.con.execute("SELECT sha FROM files WHERE path=?", (path,)).fetchone()
        if not row:
            return []
        sha = row["sha"]
        if sha not in self._blob_cache:
            out = subprocess.run(["git", "cat-file", "-p", sha], cwd=self.repo, capture_output=True)
            self._blob_cache[sha] = out.stdout.decode("utf-8", errors="replace").splitlines()
        return self._blob_cache[sha]

    def body(self, sym, max_lines: int = 80) -> str:
        lines = self._lines(sym["path"])[sym["start"] - 1: sym["end"]]
        cut = len(lines) > max_lines
        lines = lines[:max_lines]
        text = "\n".join(f"{sym['start'] + i}| {l}" for i, l in enumerate(lines))
        return text + (f"\n... ({sym['end'] - sym['start'] + 1 - max_lines} more lines)" if cut else "")

    @staticmethod
    def fmt(sym) -> str:
        return f"{sym['path']}:{sym['start']}-{sym['end']}  {sym['kind']}  {sym['qualname']}  | {sym['signature']}"

    # ---------- lookup ----------
    def search(self, text: str, k: int = 8, kinds: tuple[str, ...] | None = None) -> list[sqlite3.Row]:
        terms = subwords(text, limit=24)
        if not terms:
            return []
        rows = self.con.execute(
            "SELECT symbol_id, kind, bm25(chunks) AS score FROM chunks WHERE chunks MATCH ? ORDER BY score LIMIT ?",
            (_fts(terms), k * 4)).fetchall()
        if kinds:
            rows = [r for r in rows if r["kind"] in kinds]
        out = []
        best = rows[0]["score"] if rows else 0
        for r in rows:
            if best < 0 and r["score"] > best * 0.35:  # bm25: more negative = better; drop weak tail
                break
            sym = self.con.execute("SELECT * FROM symbols WHERE id=?", (r["symbol_id"],)).fetchone()
            if sym:
                out.append(sym)
            if len(out) >= k:
                break
        return out

    def symbols_named(self, name: str, limit: int = 5) -> list[sqlite3.Row]:
        short = name.split("::")[-1].split(".")[-1]
        rows = self.con.execute(
            "SELECT * FROM symbols WHERE (qualname=? OR name=?) AND kind IN ('function','class') LIMIT ?",
            (name.split("::")[-1], short, limit)).fetchall()
        return rows

    def symbol_at(self, path_hint: str, line: int):
        parts = path_hint.replace("\\", "/").strip("/").split("/")
        suffix = "/".join(parts[-3:])
        return self.con.execute(
            "SELECT * FROM symbols WHERE (path=? OR path LIKE ?) AND start<=? AND end>=? "
            "AND kind IN ('function','class') ORDER BY (end-start) ASC LIMIT 1",
            (suffix, f"%{suffix}", line, line)).fetchone()

    def outline(self, path: str) -> list[sqlite3.Row]:
        return self.con.execute(
            "SELECT * FROM symbols WHERE path=? AND kind IN ('function','class') ORDER BY start", (path,)).fetchall()

    # ---------- graph ----------
    def callers(self, name: str, limit: int = 15) -> list[sqlite3.Row]:
        short = name.split("::")[-1].split(".")[-1]
        return self.con.execute(
            "SELECT DISTINCT s.* FROM calls c JOIN symbols s ON s.id=c.caller WHERE c.callee_name=? LIMIT ?",
            (short, limit)).fetchall()

    def callees(self, sym_id: int, limit: int = 20) -> list[sqlite3.Row]:
        names = [r["callee_name"] for r in self.con.execute(
            "SELECT DISTINCT callee_name FROM calls WHERE caller=?", (sym_id,))]
        out = []
        for n in names:
            out += self.symbols_named(n, limit=2)
            if len(out) >= limit:
                break
        return out

    def impact(self, names: list[str], depth: int = 2) -> set[str]:
        """Qualnames that (transitively) call any of `names` – used for dependency-conflict detection."""
        frontier, seen = {n.split("::")[-1].split(".")[-1] for n in names}, set()
        for _ in range(depth):
            nxt = set()
            for n in frontier:
                for s in self.callers(n, limit=50):
                    key = f"{s['path']}::{s['qualname']}"
                    if key not in seen:
                        seen.add(key)
                        nxt.add(s["name"])
            frontier = nxt
        return seen
