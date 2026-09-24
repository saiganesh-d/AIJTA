"""Git plumbing shared by analysis, fixer and revalidation: worktrees, test runs, diff → symbols."""
import re
import shutil
import subprocess
from pathlib import Path

TEST_FILE = re.compile(r"(^|/)(tests?|__tests__|spec|specs)/|(^|/)test_[^/]+$|_test\.\w+$|\.(test|spec)\.\w+$|Tests?\.\w+$")


class GitError(RuntimeError):
    pass


def git(cwd, *args, check: bool = True, timeout: int = 300) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, errors="replace",
                       timeout=timeout)
    if check and r.returncode != 0:
        raise GitError(f"git {' '.join(args[:3])}: {(r.stderr or r.stdout).strip()[:400]}")
    return r


def out(cwd, *args) -> str:
    return git(cwd, *args).stdout.strip()


def worktree_add(repo: str, path: Path, ref: str, branch: str | None = None) -> Path:
    worktree_remove(repo, path)
    if branch:
        git(repo, "worktree", "add", "-B", branch, str(path), ref)
    else:
        git(repo, "worktree", "add", "--detach", str(path), ref)
    exclude = Path(out(path, "rev-parse", "--git-path", "info/exclude"))
    if not exclude.is_absolute():
        exclude = path / exclude
    exclude.parent.mkdir(parents=True, exist_ok=True)
    text = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if ".forge/" not in text.split():
        exclude.write_text(text.rstrip("\n") + "\n.forge/\n", encoding="utf-8")
    (path / ".forge").mkdir(exist_ok=True)
    return path


def worktree_remove(repo: str, path: Path) -> None:
    if path.exists():
        git(repo, "worktree", "remove", "--force", str(path), check=False)
        shutil.rmtree(path, ignore_errors=True)
    git(repo, "worktree", "prune", check=False)


def run_cmd(cmd: str, cwd, timeout: int = 1800) -> tuple[int, str]:
    """Run a team-configured test command (from team.json, trusted) and return (rc, trimmed output)."""
    try:
        r = subprocess.run(cmd, cwd=str(cwd), shell=True, capture_output=True, text=True, errors="replace",
                           timeout=timeout)
        return r.returncode, (r.stdout + "\n" + r.stderr)[-4000:]
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s"


def changed_files(wt) -> list[str]:
    """Tracked modifications + untracked files, relative paths, .forge excluded."""
    files = []
    for line in git(wt, "status", "--porcelain", "--untracked-files=all").stdout.splitlines():  # no strip: XY column
        p = line[3:].split(" -> ")[-1].strip().strip('"')
        if p and not p.startswith(".forge/"):
            files.append(p)
    return files


def is_test_file(path: str) -> bool:
    return bool(TEST_FILE.search(path.replace("\\", "/")))


HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def diff_lines(wt, base: str) -> dict[str, list[int]]:
    """{path: [changed line numbers in the new version]} for base..HEAD."""
    res: dict[str, list[int]] = {}
    cur = None
    for line in git(wt, "diff", "-U0", f"{base}..HEAD").stdout.splitlines():
        if line.startswith("+++ "):
            cur = line[6:] if line.startswith("+++ b/") else None
        elif cur and (m := HUNK.match(line)):
            start, n = int(m.group(1)), int(m.group(2) or 1)
            res.setdefault(cur, []).extend(range(start, start + max(n, 1)))
    return res


def changed_symbols(idx, wt, base: str) -> list[str]:
    """Map diff hunks to indexed symbols (approximate: the index is of base_ref)."""
    syms = []
    for path, lines in diff_lines(wt, base).items():
        for ln in lines[:: max(1, len(lines) // 10)]:
            s = idx.symbol_at(path, ln)
            if s:
                key = f"{s['path']}::{s['qualname']}"
                if key not in syms:
                    syms.append(key)
    return syms


def branch_name(base_ref: str) -> str:
    """origin/main → main (the PR base on GitHub)."""
    return base_ref.split("/", 1)[1] if base_ref.startswith("origin/") else base_ref
