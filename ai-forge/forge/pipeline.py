"""`forge run`: one scheduled cycle. Implemented: guards, self-update, agent sync, index refresh,
heartbeat. Stages marked TODO are specified step-by-step in PLAN.md (section numbers in brackets)."""
from datetime import datetime

from . import config as C
from .copilot import tokens_today
from .index.indexer import build_repo_map, index_repo
from .shared import atomic_write


def within_work_hours(cfg: C.Config) -> bool:
    wh = cfg.team.get("work_hours") or {}
    now = datetime.now()
    if wh.get("days") and now.isoweekday() not in wh["days"]:
        return False
    start, end = wh.get("start", "00:00"), wh.get("end", "23:59")
    return start <= now.strftime("%H:%M") <= end


def heartbeat(cfg: C.Config, extra: dict) -> None:
    atomic_write(cfg.shared / "runners" / f"{cfg.user_id}.json", {
        "schema": 1, "user": cfg.user_id, "last_run": datetime.now().isoformat(timespec="seconds"),
        "mcp_mode": cfg.mcp_mode, "tokens_today_est": tokens_today(), **extra})


# ---- stages (see PLAN.md) ----
def sync_jira(cfg):            raise NotImplementedError("PLAN §5.3 Jira sync")
def preclassify(cfg):          raise NotImplementedError("PLAN §5.7 pre-classifier & router")
def dedup_and_history(cfg):    raise NotImplementedError("PLAN §5.9 dedup, past-sprint & regression facts")
def group_tickets(cfg):        raise NotImplementedError("PLAN §5.8 grouping")
def analyze_groups(cfg):       raise NotImplementedError("PLAN §5.11 analysis via agents")
def process_decisions(cfg):    raise NotImplementedError("PLAN §5.13 decisions from Teams")
def run_fixes(cfg):            raise NotImplementedError("PLAN §5.14 fix pipeline")
def revalidate_after_merges(cfg): raise NotImplementedError("PLAN §5.15 conflicts & revalidation")


STAGES = [sync_jira, preclassify, dedup_and_history, group_tickets, analyze_groups,
          process_decisions, run_fixes, revalidate_after_merges]


def run_once(force: bool = False) -> None:
    cfg = C.load()
    if not force and not within_work_hours(cfg):
        return
    from .setup_wizard import sync_agents
    sync_agents(cfg)
    stats = index_repo(cfg.repo_path, cfg.base_ref, cfg.index_db, log=lambda *_: None)
    if stats["files_reindexed"]:
        build_repo_map(cfg.index_db, cfg.repo_map)
    done = []
    for stage in STAGES:
        try:
            stage(cfg)
            done.append(stage.__name__)
        except NotImplementedError as e:
            print(f"TODO {stage.__name__}: {e}")
    heartbeat(cfg, {"index_commit": stats["commit"], "stages_ok": done})
