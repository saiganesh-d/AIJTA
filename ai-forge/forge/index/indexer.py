"""Incremental code indexer.

Indexes the *base branch* (e.g. origin/main) straight from git objects, so it never
depends on the state of anyone's working copy. Only changed blobs are re-parsed."""
import ast
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import PurePosixPath

from ..signals import subwords
from .store import connect

LANG_BY_EXT = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".ts": "typescript",
    ".tsx": "tsx", ".java": "java", ".kt": "kotlin", ".go": "go", ".c": "c", ".h": "c", ".cpp": "cpp",
    ".cc": "cpp", ".hpp": "cpp", ".cs": "csharp", ".rb": "ruby", ".rs": "rust", ".php": "php", ".scala": "scala",
}
CONFIG_EXT = {".yaml", ".yml", ".json", ".ini", ".cfg", ".conf", ".properties", ".toml", ".env", ".xml"}
SKIP_PARTS = {"node_modules", "vendor", "dist", "build", "target", ".venv", "venv", "__pycache__", "migrations", ".git"}
MAX_BYTES = 400_000

DEF_TYPES = {
    "function_definition", "function_declaration", "method_definition", "method_declaration",
    "class_definition", "class_declaration", "interface_declaration", "constructor_declaration",
    "function_item", "impl_item", "struct_item", "enum_declaration", "trait_item", "object_declaration",
    "struct_specifier", "class_specifier", "method", "singleton_method", "module",
}
CLASS_HINT = ("class", "interface", "struct", "enum", "impl", "trait", "object", "module")
CALL_TYPES = {"call", "call_expression", "method_invocation", "invocation_expression", "function_call_expression",
              "member_call_expression", "method_call_expression"}
NAME_NODE = {"identifier", "field_identifier", "property_identifier", "type_identifier", "constant", "name",
             "qualified_identifier", "destructor_name", "simple_identifier"}


@dataclass
class Sym:
    name: str
    qualname: str
    kind: str
    start: int
    end: int
    signature: str
    calls: set[str] = field(default_factory=set)
    terms: str = ""


def _git(repo: str, *args) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout


def _ls_tree(repo: str, ref: str) -> dict[str, tuple[str, int]]:
    out = {}
    for line in _git(repo, "ls-tree", "-r", "--long", ref).splitlines():
        meta, path = line.split("\t", 1)
        mode, typ, sha, size = meta.split()
        if typ == "blob" and size != "-" and int(size) <= MAX_BYTES:
            out[path] = (sha, int(size))
    return out


def _indexable(path: str) -> str | None:
    p = PurePosixPath(path)
    if set(p.parts) & SKIP_PARTS or p.name.endswith((".min.js", ".lock")) or p.name == "package-lock.json":
        return None
    if p.suffix in LANG_BY_EXT:
        return LANG_BY_EXT[p.suffix]
    if p.suffix in CONFIG_EXT or p.name.startswith(".env"):
        return "config"
    return None


def _cat_blobs(repo: str, items: list[tuple[str, str]]):
    proc = subprocess.Popen(["git", "cat-file", "--batch"], cwd=repo, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        for path, sha in items:
            proc.stdin.write((sha + "\n").encode())
            proc.stdin.flush()
            header = proc.stdout.readline().split()
            size = int(header[2])
            data = proc.stdout.read(size)
            proc.stdout.read(1)
            yield path, data
    finally:
        proc.stdin.close()
        proc.wait()


# ---------------- parsers ----------------
def _node_name(node) -> str | None:
    n = node.child_by_field_name("name")
    if n is not None:
        return n.text.decode(errors="replace")
    d = node.child_by_field_name("declarator")
    while d is not None:
        if d.type in NAME_NODE:
            return d.text.decode(errors="replace").split("::")[-1]
        d = d.child_by_field_name("declarator") or d.child_by_field_name("name")
    for ch in node.children:
        if ch.type in NAME_NODE:
            return ch.text.decode(errors="replace")
    return None


def _call_name(node) -> str | None:
    target = (node.child_by_field_name("function") or node.child_by_field_name("name")
              or node.child_by_field_name("method") or (node.children[0] if node.children else None))
    if target is None:
        return None
    txt = target.text.decode(errors="replace")
    for sep in ("::", "->", "."):
        txt = txt.split(sep)[-1]
    txt = txt.split("(")[0].strip()
    return txt if txt.isidentifier() else None


def _collect_calls(node, out: set[str]):
    for ch in node.children:
        if ch.type in DEF_TYPES:
            continue
        if ch.type in CALL_TYPES:
            n = _call_name(ch)
            if n:
                out.add(n)
        _collect_calls(ch, out)


def _ts_symbols(src: bytes, lang: str, lines: list[str]) -> list[Sym] | None:
    try:
        from tree_sitter_language_pack import get_parser
        parser = get_parser(lang)
    except Exception:
        return None
    tree = parser.parse(src)
    out: list[Sym] = []

    def walk(node, scope: list[str]):
        if node.type in DEF_TYPES:
            name = _node_name(node)
            if name:
                kind = "class" if any(h in node.type for h in CLASS_HINT) else "function"
                s, e = node.start_point[0] + 1, node.end_point[0] + 1
                calls: set[str] = set()
                _collect_calls(node, calls)
                body = "\n".join(lines[s - 1: (s + 25 if kind == "class" else e)])
                out.append(Sym(name, ".".join(scope + [name]), kind, s, e, lines[s - 1].strip()[:160], calls,
                               " ".join(subwords(name + " " + body, limit=400))))
                for ch in node.children:
                    walk(ch, scope + [name])
                return
        for ch in node.children:
            walk(ch, scope)

    walk(tree.root_node, [])
    return out


def _py_ast_symbols(text: str, lines: list[str]) -> list[Sym] | None:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    out: list[Sym] = []

    def calls_of(node) -> set[str]:
        names = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                f = sub.func
                names.add(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))
        return {n for n in names if n}

    def visit(node, scope):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                kind = "class" if isinstance(ch, ast.ClassDef) else "function"
                s, e = ch.lineno, ch.end_lineno or ch.lineno
                body = "\n".join(lines[s - 1: (s + 25 if kind == "class" else e)])
                out.append(Sym(ch.name, ".".join(scope + [ch.name]), kind, s, e, lines[s - 1].strip()[:160],
                               calls_of(ch) if kind == "function" else set(),
                               " ".join(subwords(ch.name + " " + body, limit=400))))
                visit(ch, scope + [ch.name])

    visit(tree, [])
    return out


def _windows(lines: list[str], kind: str, size: int) -> list[Sym]:
    out = []
    for i in range(0, max(1, len(lines)), size):
        chunk = lines[i:i + size]
        if not any(l.strip() for l in chunk):
            continue
        first = next((l.strip() for l in chunk if l.strip()), "")[:160]
        out.append(Sym(f"L{i + 1}", f"L{i + 1}-{i + len(chunk)}", kind, i + 1, i + len(chunk), first, set(),
                       " ".join(subwords("\n".join(chunk), limit=400))))
    return out


def parse_file(path: str, data: bytes, lang: str) -> list[Sym]:
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if lang == "config":
        return _windows(lines, "config", 40)
    syms = _ts_symbols(data, lang, lines)
    if syms is None and lang == "python":
        syms = _py_ast_symbols(text, lines)
    if not syms:
        return _windows(lines, "block", 60)
    return syms


# ---------------- main entry ----------------
def index_repo(repo: str, ref: str, db_path, full: bool = False, fetch: bool = True, log=print) -> dict:
    if fetch:
        subprocess.run(["git", "fetch", "--quiet", "origin"], cwd=repo, capture_output=True)
    commit = _git(repo, "rev-parse", ref).strip()
    con = connect(db_path)
    if full:
        con.executescript("DELETE FROM files; DELETE FROM symbols; DELETE FROM calls; DELETE FROM chunks;")
    tree = {p: v for p, v in _ls_tree(repo, ref).items() if _indexable(p)}
    existing = {r["path"]: r["sha"] for r in con.execute("SELECT path, sha FROM files")}
    removed = [p for p in existing if p not in tree]
    changed = [p for p, (sha, _) in tree.items() if existing.get(p) != sha]

    def drop(path):
        ids = [r[0] for r in con.execute("SELECT id FROM symbols WHERE path=?", (path,))]
        for i in ids:
            con.execute("DELETE FROM calls WHERE caller=?", (i,))
            con.execute("DELETE FROM chunks WHERE symbol_id=?", (i,))
        con.execute("DELETE FROM symbols WHERE path=?", (path,))
        con.execute("DELETE FROM files WHERE path=?", (path,))

    for p in removed + changed:
        drop(p)

    n_sym = 0
    for path, data in _cat_blobs(repo, [(p, tree[p][0]) for p in changed]):
        lang = _indexable(path)
        syms = parse_file(path, data, lang)
        con.execute("INSERT INTO files(path, sha, lang, lines) VALUES (?,?,?,?)",
                    (path, tree[path][0], lang, data.count(b"\n") + 1))
        for s in syms:
            cur = con.execute(
                "INSERT INTO symbols(path, name, qualname, kind, start, end, signature) VALUES (?,?,?,?,?,?,?)",
                (path, s.name, s.qualname, s.kind, s.start, s.end, s.signature))
            sid = cur.lastrowid
            con.executemany("INSERT INTO calls(caller, callee_name) VALUES (?,?)", [(sid, c) for c in s.calls])
            con.execute("INSERT INTO chunks(symbol_id, kind, path, qualname, terms) VALUES (?,?,?,?,?)",
                        (sid, s.kind, " ".join(subwords(path)), " ".join(subwords(s.qualname)), s.terms))
            n_sym += 1

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    con.executemany("INSERT OR REPLACE INTO meta(k, v) VALUES (?,?)",
                    [("commit", commit), ("ref", ref), ("updated", now)])
    con.commit()
    stats = {"commit": commit[:10], "files_total": len(tree), "files_reindexed": len(changed),
             "files_removed": len(removed), "symbols_added": n_sym}
    log(f"index: {stats}")
    return stats


def build_repo_map(db_path, out_path, budget_chars: int = 8000) -> str:
    """Compact, ranked map of the codebase (~2k tokens) that agents read instead of exploring."""
    con = connect(db_path)
    ranked = con.execute("""
        SELECT s.path, COUNT(c.caller) AS refs FROM symbols s
        LEFT JOIN calls c ON c.callee_name = s.name
        WHERE s.kind IN ('function','class') GROUP BY s.path ORDER BY refs DESC""").fetchall()
    commit = (con.execute("SELECT v FROM meta WHERE k='commit'").fetchone() or ["?"])[0]
    lines = [f"# Repo map (auto-generated from {commit[:10]}; files ranked by how often their code is called)", ""]
    used = sum(len(l) for l in lines)
    for r in ranked:
        tops = con.execute("""SELECT name, kind FROM symbols WHERE path=? AND kind IN ('function','class')
                              AND qualname NOT LIKE '%.%' ORDER BY start LIMIT 10""", (r["path"],)).fetchall()
        entry = f"- {r['path']}: " + ", ".join(f"{t['name']}{'()' if t['kind'] == 'function' else ''}" for t in tops)
        if used + len(entry) > budget_chars:
            lines.append(f"- ... {len(ranked) - (len(lines) - 2)} more files (use forge-index search_code)")
            break
        lines.append(entry)
        used += len(entry)
    text = "\n".join(lines) + "\n"
    out_path.write_text(text, encoding="utf-8")
    return text
