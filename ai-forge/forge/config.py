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
