"""Local per-user config (~/.ai-forge/config.json) + team config from the shared folder."""
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

HOME = Path(os.environ.get("AI_FORGE_HOME", Path.home() / ".ai-forge"))
LOCAL_CONFIG = HOME / "config.json"
ASSETS = Path(__file__).parent / "assets"
KEYRING_SERVICE = "ai-forge-jira"


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

    @property
    def me(self) -> dict:
        return (self.team.get("members") or {}).get(self.user_id) or {}

    @property
    def db_path(self) -> Path:
        return HOME / "forge.db"

    def member_by_email(self, email: str) -> str | None:
        email = (email or "").lower()
        for uid, m in (self.team.get("members") or {}).items():
            if (m.get("email") or "").lower() == email:
                return uid
        return None


REQUIRED_TEAM_KEYS = ("jira", "members", "approvers", "lead", "models", "test")
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
    members = team.get("members") or {}
    if user_id and user_id not in members:
        probs.append(f"you ('{user_id}') are not listed in members")
    for uid, m in members.items():
        if "@" not in (m.get("email") or ""):
            probs.append(f"members.{uid}.email is missing")
    approvers = [a.lower() for a in team.get("approvers") or []]
    if not approvers:
        probs.append("approvers is empty: nobody could approve a fix")
    if team.get("lead") and team["lead"].lower() not in approvers:
        probs.append("lead is not in approvers")
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
    import keyring
    return keyring.get_password(KEYRING_SERVICE, email)


def set_jira_token(email: str, token: str) -> None:
    import keyring
    keyring.set_password(KEYRING_SERVICE, email, token)
