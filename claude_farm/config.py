"""All settings come from environment variables (see .env.example)."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass

from .governor import Policy


def _env(name: str, default: str) -> str:
    v = os.environ.get(name)
    return default if v is None or v == "" else v


def _bool(name: str, default: bool) -> bool:
    return _env(name, "1" if default else "0").lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    name: str  # farm name: shown in the Claude app and in the logs
    store: str  # sqlite (one box, the default) | dynamodb (several boxes / accounts)
    db_path: str  # the SQLite file
    table: str
    region: str
    endpoint: str | None  # DynamoDB Local URL, or None for real DynamoDB
    workspace: str
    repo_url: str | None
    model: str
    permission_mode: str
    claude_bin: str
    task_timeout: int
    lease_seconds: int
    remote_control: bool
    rc_spawn: str
    rc_capacity: int
    planner: bool
    planner_cooldown: int
    planner_max_backoff: int
    max_queue: int
    max_depth: int
    max_attempts: int
    max_resumes: int
    idle_sleep: int
    push: bool
    seat: str  # FARM_SEAT: override the seat id derived from the login ("" = derive it)
    resume_affinity: int  # seconds a resumed task waits for the box that holds its conversation
    effort: str  # --effort for every agent run ("" = Claude Code's default)
    verify_cmd: str  # shell command that must pass before a top-level task lands on main ("" = none)
    verify_timeout: int
    verify_fixes: int  # how many times an agent is resumed to fix a failing verify before the task fails
    timeout_resumes: int  # how many times a timed-out run is resumed in its own session
    stall_threshold: int  # this many failed runs in a row pause the whole farm (0 = never)
    notify_url: str  # webhook for important events: ntfy, Slack or Discord ("" = none)
    task_budget_usd: float  # API mode: --max-budget-usd per agent run (0 = none)
    manage_claude_config: bool  # write the farm guide and folder trust into Claude Code's config (container: yes)
    policy: Policy

    @property
    def farm_id(self) -> str:
        return f"{self.name}@{socket.gethostname()}"

    @property
    def repo_dir(self) -> str:
        return os.path.join(self.workspace, "repo")

    @property
    def mission_paths(self) -> list[str]:
        return [os.path.join(self.repo_dir, "MISSION.md"), os.path.join(self.workspace, "MISSION.md")]


def _store_kind() -> str:
    kind = _env("FARM_STORE", "").lower()
    if kind in ("sqlite", "dynamodb"):
        return kind
    # joining a DynamoDB farm: an endpoint (DynamoDB Local) or an explicit table name implies it
    return "dynamodb" if os.environ.get("FARM_DYNAMODB_ENDPOINT") or os.environ.get("FARM_TABLE") else "sqlite"


def load() -> Config:
    return Config(
        name=_env("FARM_NAME", "claude-farm"),
        store=_store_kind(),
        db_path=_env("FARM_DB", os.path.join(_env("FARM_WORKSPACE", "/workspace"), ".farm", "farm.db")),
        table=_env("FARM_TABLE", "claude-farm"),
        region=_env("AWS_REGION", _env("AWS_DEFAULT_REGION", "us-east-1")),
        endpoint=os.environ.get("FARM_DYNAMODB_ENDPOINT") or None,
        workspace=_env("FARM_WORKSPACE", "/workspace"),
        repo_url=os.environ.get("FARM_REPO_URL") or None,
        model=_env("FARM_MODEL", "opus"),
        permission_mode=_env("FARM_PERMISSION_MODE", "bypassPermissions"),
        claude_bin=_env("FARM_CLAUDE_BIN", "claude"),
        task_timeout=int(_env("FARM_TASK_TIMEOUT", "5400")),
        lease_seconds=int(_env("FARM_LEASE_SECONDS", "300")),
        remote_control=_bool("FARM_REMOTE_CONTROL", True),
        rc_spawn=_env("FARM_RC_SPAWN", "worktree"),
        rc_capacity=int(_env("FARM_RC_CAPACITY", "4")),
        planner=_bool("FARM_PLANNER", True),
        planner_cooldown=int(_env("FARM_PLANNER_COOLDOWN", "600")),
        planner_max_backoff=int(_env("FARM_PLANNER_MAX_BACKOFF", "21600")),
        max_queue=int(_env("FARM_MAX_QUEUE", "25")),
        max_depth=int(_env("FARM_MAX_DEPTH", "3")),
        max_attempts=int(_env("FARM_MAX_ATTEMPTS", "3")),
        max_resumes=int(_env("FARM_MAX_RESUMES", "5")),
        idle_sleep=int(_env("FARM_IDLE_SLEEP", "30")),
        push=_bool("FARM_PUSH", True),
        task_budget_usd=float(_env("FARM_TASK_BUDGET_USD", "0")),
        effort=_env("FARM_EFFORT", ""),
        seat=_env("FARM_SEAT", ""),
        resume_affinity=int(_env("FARM_RESUME_AFFINITY", "600")),
        verify_cmd=_env("FARM_VERIFY_CMD", ""),
        verify_timeout=int(_env("FARM_VERIFY_TIMEOUT", "900")),
        verify_fixes=int(_env("FARM_VERIFY_FIXES", "2")),
        timeout_resumes=int(_env("FARM_TIMEOUT_RESUMES", "2")),
        stall_threshold=int(_env("FARM_STALL_THRESHOLD", "5")),
        notify_url=_env("FARM_NOTIFY_URL", ""),
        manage_claude_config=_bool("FARM_MANAGE_CLAUDE_CONFIG", True),
        policy=Policy(
            max_workers=int(_env("FARM_MAX_WORKERS", "3")),
            weekly_target=float(_env("FARM_WEEKLY_TARGET", "0.80")),
            five_hour_ceiling=float(_env("FARM_FIVE_HOUR_CEILING", "0.85")),
            weekly_band=float(_env("FARM_WEEKLY_BAND", "0.05")),
            five_hour_band=float(_env("FARM_FIVE_HOUR_BAND", "0.30")),
            burst_hours=float(_env("FARM_BURST_HOURS", "0")),
            allow_overage=_bool("FARM_ALLOW_OVERAGE", False),
            api_mode=bool(os.environ.get("ANTHROPIC_API_KEY")) and not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"),
            daily_budget_usd=float(_env("FARM_DAILY_BUDGET_USD", "0")),
        ),
    )
