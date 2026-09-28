"""Local per-user config (~/.ai-forge/config.json) + team config from the shared folder."""
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

HOME = Path(os.environ.get("AI_FORGE_HOME", Path.home() / ".ai-forge"))
LOCAL_CONFIG = HOME / "config.json"
ASSETS = Path(__file__).parent / "assets"
KEYRING_SERVICE = "ai-forge-jira"


def load_env_file(path: Path | None = None) -> None:
    """Load KEY=VALUE lines from a .env file into os.environ (never overrides variables already set).
    Looks in the current folder, ~/.ai-forge/.env, then ~/.env. Supported keys: JIRA_BASE_URL,
    JIRA_EMAIL, JIRA_API_TOKEN (or JIRA_TOKEN / JIRA_PAT)."""
    if path is None:
        for c in (Path(".env"), HOME / ".env", Path.home() / ".env"):
            if c.is_file():
                path = c
                break
    if not path or not path.is_file():
        return
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = (x.strip() for x in line.split("=", 1))
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                v = v[1:-1]
            if k:
                os.environ.setdefault(k, v)
    except Exception:
        pass


load_env_file()


@dataclass
class Config:
    user_id: str                 # short id used in shared files, e.g. "sai"
    user_email: str
    repo_path: str
    shared_dir: str              # synced SharePoint/OneDrive folder "AI-Forge-Shared"
    base_ref: str = "origin/main"
    mcp_mode: str = "agent"      # agent | global | off   (decided by `forge doctor --live`)
    team: dict = field(default_factory=dict)

    @property
    def shared(self) -> Path:
        return Path(self.shared_dir)

    @property
    def repo_key(self) -> str:
        return Path(self.repo_path).resolve().name

    @property
    def index_dir(self) -> Path:
        d = HOME / "index" / self.repo_key
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def index_db(self) -> Path:
        return self.index_dir / "index.db"

    @property
    def repo_map(self) -> Path:
        return self.index_dir / "REPO_MAP.md"

    @property
    def worktree_root(self) -> Path:
        d = HOME / "worktrees"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def model_for(self, agent: str) -> str:
        m = (self.team.get("models") or {}).get(agent, "")
        if not m or m.startswith("REPLACE"):
            raise RuntimeError(f"No model configured for {agent} in team.json")
        return m

    def threshold(self, key: str, default):
        return (self.team.get("thresholds") or {}).get(key, default)

    def index_ext(self) -> dict:
        """team.json → index: extra file types, {".ext": language} for code and a list for config files."""
        ix = self.team.get("index") or {}
        out = {e.lower(): lang for e, lang in (ix.get("code_ext") or {}).items()}
        out.update({e.lower(): "config" for e in ix.get("config_ext") or []})
        return out

    def ctx(self, key: str):
        """team.json → context: what goes into the pack Copilot gets (see CONTEXT_DEFAULTS)."""
        return (self.team.get("context") or {}).get(key, CONTEXT_DEFAULTS[key])

    @property
    def me(self) -> dict:
        return (self.team.get("members") or {}).get(self.user_id) or {}

    @property
    def my_email(self) -> str:
        """The only identity allowed to approve or reject: single-user mode, you own the code and the decisions."""
        return (self.me.get("email") or self.user_email or "").lower()

    def budget(self, key: str, default: int) -> int:
        """team.json → budget.{daily_tokens, monthly_tokens}; 0 disables a limit.
        Falls back to the older members.<me>.daily_token_budget."""
        b = self.team.get("budget") or {}
        if key in b:
            return int(b[key] or 0)
        if key == "daily_tokens" and "daily_token_budget" in self.me:
            return int(self.me["daily_token_budget"])
        return default

    @property
    def db_path(self) -> Path:
        return HOME / "forge.db"

    def member_by_email(self, email: str) -> str | None:
        email = (email or "").lower()
        for uid, m in (self.team.get("members") or {}).items():
            if (m.get("email") or "").lower() == email:
                return uid
        return None


REQUIRED_TEAM_KEYS = ("jira", "models", "test")

# What the context pack contains. Every source is local and free; each costs pack tokens only when it
# finds something. Switch a source off (false / 0) in team.json → context if it doesn't pay off.
CONTEXT_DEFAULTS = {
    "past_tickets": True,            # related past tickets with root cause, fix and git facts
    "max_past_tickets": 3,
    "min_past_score": 0.3,           # tiers: same error 1.0 > same function ≥0.7 > same file ≥0.5 > wording (0.5 × similarity)
    "git_ticket_map": True,          # learn ticket → files from commit messages ("SUP-12: ...") on base_ref
    "recent_changes_days": 14,       # commits on the relevant files in the last N days (0 = off)
    "recent_changes_max_commits": 5,
    "recent_changes_diff_lines": 20,  # diff lines per commit (0 = one line per commit only)
    "all_comments": True,            # all ticket comments, not only the reporter's
    "past_evidence": True,           # current code at a strongly related past ticket's evidence (start where it ended)
    "past_evidence_min_score": 0.5,  # same file or stronger (see min_past_score tiers)
    "past_evidence_max_blocks": 2,
    "learn_gaps": True,              # record code Copilot needed that the pack missed (forge gaps)
    "attachment_files": True,        # raw attachments in .forge/attachments; Copilot opens them only when needed
    "attachment_files_max_mb": 10,   # per file; larger ones are listed but not copied
}
REQUIRED_MODELS = ("forge-doctor", "forge-config", "forge-analyst", "forge-fixer", "forge-adapter")


def validate_team(team: dict, user_id: str | None = None) -> list[str]:
    """Problems in team.json that would make a run fail or behave unsafely. Empty list = OK."""
    if not team:
        return ["config/team.json is missing or empty"]
    probs = [f"missing key: {k}" for k in REQUIRED_TEAM_KEYS if k not in team]
    jira = team.get("jira") or {}
    if jira.get("mode", "api") == "api":
        for k in ("base_url", "scope_jql"):
            if not jira.get(k) or "yourorg" in str(jira.get(k)):
                probs.append(f"jira.{k} is not set")
    models = team.get("models") or {}
    for agent in REQUIRED_MODELS:
        m = models.get(agent, "")
        if not m or str(m).startswith("REPLACE"):
            probs.append(f"models.{agent} is a placeholder ({m or 'empty'})")
    esc = models.get("escalation", "")
    if esc and str(esc).startswith("REPLACE"):
        probs.append("models.escalation is a placeholder (remove it or set \"auto\" to disable escalation)")
    for uid, m in (team.get("members") or {}).items():  # optional: only needed for PR reviewers
        if "@" not in (m.get("email") or ""):
            probs.append(f"members.{uid}.email is missing")
    targeted = (team.get("test") or {}).get("targeted", "")
    if targeted and "{test_path}" not in targeted:
        probs.append("test.targeted must contain {test_path}")
    return probs


def load() -> Config:
    if not LOCAL_CONFIG.exists():
        raise SystemExit("AI Forge is not set up yet. Run: forge setup")
    data = json.loads(LOCAL_CONFIG.read_text(encoding="utf-8"))
    cfg = Config(**{k: v for k, v in data.items() if k in Config.__dataclass_fields__ and k != "team"})
    team_file = cfg.shared / "config" / "team.json"
    if team_file.exists():
        cfg.team = json.loads(team_file.read_text(encoding="utf-8"))
        cfg.base_ref = cfg.team.get("base_ref", cfg.base_ref)
    return cfg


def save(cfg: Config) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    data = asdict(cfg)
    data.pop("team", None)
    LOCAL_CONFIG.write_text(json.dumps(data, indent=2), encoding="utf-8")


def jira_token(email: str) -> str | None:
    """Environment (.env) first, then the OS keychain. A keychain that is unavailable is not fatal."""
    env_token = os.environ.get("JIRA_API_TOKEN") or os.environ.get("JIRA_TOKEN") or os.environ.get("JIRA_PAT")
    if env_token:
        return env_token
    try:
        import keyring
        return keyring.get_password(KEYRING_SERVICE, email)
    except Exception:
        return None


def set_jira_token(email: str, token: str) -> None:
    try:
        import keyring
        keyring.set_password(KEYRING_SERVICE, email, token)
    except Exception:
        pass
    os.environ["JIRA_API_TOKEN"] = token


def jira_email(cfg: "Config") -> str:
    """The Jira login: JIRA_EMAIL (.env) overrides the Forge user email."""
    return (os.environ.get("JIRA_EMAIL") or cfg.user_email or (cfg.team.get("jira") or {}).get("email", "")).strip()
