"""Safe access to the OneDrive/SharePoint-synced shared folder.

Rules: one writer per file, atomic writes, strict file-name filters so OneDrive
conflict copies (e.g. 'SUP-12-LAPTOP-AB12.json') and temp files are ignored."""
import json
import os
import re
import uuid
from pathlib import Path

from .config import HOME

TICKET_FILE = re.compile(r"^[A-Z][A-Z0-9_]*-\d+\.json$")
GROUP_OR_TICKET_FILE = re.compile(r"^(?:[A-Z][A-Z0-9_]*-\d+|G-[0-9a-f]{8})\.json$")
REQUEST_FILE = re.compile(r"^[A-Z][A-Z0-9_]*-\d+__[0-9a-f]{8}\.json$")

FOLDERS = ("analyses", "decisions", "inflight", "runners", "outbox", "config", "lessons", "agents")


def ensure_layout(shared: Path) -> None:
    for f in FOLDERS:
        (shared / f).mkdir(parents=True, exist_ok=True)


def atomic_write(path: Path, obj) -> None:
    """Write to a local temp file, then replace, so teammates never sync half a file."""
    data = json.dumps(obj, indent=2, ensure_ascii=False)
    tmp_dir = HOME / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / f"{path.name}.{uuid.uuid4().hex[:8]}.tmp"
    tmp.write_text(data, encoding="utf-8")
    try:
        os.replace(tmp, path)
    except OSError:  # different volume: fall back to same-folder temp
        local_tmp = path.with_name(f"~{path.name}.{uuid.uuid4().hex[:8]}.tmp")
        local_tmp.write_text(data, encoding="utf-8")
        os.replace(local_tmp, path)
        tmp.unlink(missing_ok=True)


def read_json(path: Path, expect_schema: int = 1) -> dict | None:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None  # still syncing, placeholder or corrupt: try next run
    if not isinstance(obj, dict) or obj.get("schema") != expect_schema:
        return None
    return obj


def read_dir(folder: Path, pattern: re.Pattern = TICKET_FILE) -> dict[str, dict]:
    out = {}
    if not folder.exists():
        return out
    for p in folder.iterdir():
        if p.is_file() and pattern.match(p.name):
            obj = read_json(p)
            if obj is not None:
                out[p.stem] = obj
    return out
