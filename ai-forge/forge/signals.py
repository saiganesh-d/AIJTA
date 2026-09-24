"""Free, local text signals: scrubbing, log trimming, stack frames, error signatures, search terms."""
import hashlib
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

SECRET = re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|authorization|bearer)\b(\s*[:=]\s*|\s+)[^\s,;)\]}\"']+")
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
VIN = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")  # vehicle identification numbers
SIGNAL = re.compile(r"(error|exception|traceback|fatal|failed|caused by|panic|denied|timeout|refused)", re.I)
EXC = re.compile(r"\b([A-Z][A-Za-z0-9]*(?:Error|Exception|Fault|Panic))\b")

FRAME_PATTERNS = [
    re.compile(r'File "(?P<path>[^"]+)", line (?P<line>\d+), in (?P<func>[\w<>]+)'),                      # Python
    re.compile(r'at (?P<func>[\w.$<>]+)\((?P<path>[\w.$-]+\.(?:java|kt|scala)):(?P<line>\d+)\)'),        # JVM
    re.compile(r'at (?:(?P<func>[\w.$<>]+) \()?(?P<path>[^\s()]+\.(?:js|jsx|ts|tsx|mjs|cjs)):(?P<line>\d+):\d+'),  # Node
    re.compile(r'(?P<path>[\w./\\-]+\.(?:go|c|cc|cpp|h|hpp|rs|cs|rb|php|py)):(?P<line>\d+)'),              # generic
]

STOP = set("""the and for with that this from have has not are was were but you your our their into when then
than there here what which while where would should could error errors exception failed issue ticket please
user users line file none null true false get set new self def return class function value""".split())


@dataclass(frozen=True)
class Frame:
    path: str
    line: int
    func: str = ""


def scrub(text: str) -> str:
    text = SECRET.sub(lambda m: f"{m.group(1)}=<REDACTED>", text or "")
    text = EMAIL.sub("<EMAIL>", text)
    return VIN.sub("<VIN>", text)


def trim_log(text: str, window: int = 5, max_chars: int = 5000) -> str:
    lines = (text or "").splitlines()
    keep: set[int] = set()
    for i, line in enumerate(lines):
        if SIGNAL.search(line):
            keep.update(range(max(0, i - window), min(len(lines), i + window + 1)))
    if not keep:
        return "\n".join(lines[-60:])[-max_chars:]
    out, prev = [], -2
    for i in sorted(keep):
        if i != prev + 1:
            out.append("...")
        out.append(lines[i])
        prev = i
    return "\n".join(out)[-max_chars:]


def parse_frames(text: str, limit: int = 12) -> list[Frame]:
    seen, out = set(), []
    for pat in FRAME_PATTERNS:
        for m in pat.finditer(text or ""):
            path = m.group("path").replace("\\", "/")
            key = (path, m.group("line"))
            if key in seen or "site-packages" in path or "node_modules" in path:
                continue
            seen.add(key)
            out.append(Frame(path, int(m.group("line")), (m.groupdict().get("func") or "")))
    return out[:limit]


def error_signature(text: str) -> str | None:
    """Order-independent fingerprint: exception type + innermost app frames (no line numbers)."""
    exc = EXC.findall(text or "")
    frames = parse_frames(text)
    if not exc and not frames:
        return None
    parts = sorted({f"{PurePosixPath(f.path).name}:{f.func}" for f in frames[:5]})
    norm = (exc[0] if exc else "") + "|" + "|".join(parts)
    return hashlib.sha1(norm.encode()).hexdigest()[:12]


_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def subwords(text: str, limit: int | None = None) -> list[str]:
    """Split identifiers and prose into lowercase search terms (camelCase + snake_case aware)."""
    out: list[str] = []
    seen = set()
    for ident in _IDENT.findall(text or ""):
        pieces = [ident.lower()] if len(ident) <= 40 else []
        for part in ident.split("_"):
            pieces += [p.lower() for p in _CAMEL.findall(part)]
        for p in pieces:
            if len(p) >= 3 and p not in STOP and not p.isdigit() and p not in seen:
                seen.add(p)
                out.append(p)
                if limit and len(out) >= limit:
                    return out
    return out


def estimate_tokens(text: str) -> int:
    return max(1, len(text or "") // 4)
