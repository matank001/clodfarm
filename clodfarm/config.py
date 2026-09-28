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
    name: str  # this box's Claude: the person's account it runs (matan, gil, ...); see claude_name()
    farm: str  # the farm's name (FARM_NAME), shared by every Claude on it
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
    claude_update: int  # update Claude Code to the newest release every this many seconds (0 = never; image: 3600)
    task_timeout: int
    lease_seconds: int
    remote_control: bool
    rc_spawn: str
    rc_capacity: int
    max_queue: int
    usage_refresh: int  # measure this account's usage when nothing measured it for this many seconds (0 = off)
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
    ui: bool  # serve the farm UI from the daemon (FARM_UI_PORT, default 8080)
    manage_claude_config: bool  # write the farm guide and folder trust into Claude Code's config (container: yes)
    live_stdin: bool  # sub-agents read stream-json on an open stdin, so an --urgent message can interrupt them
    share_sessions: bool  # the Claudes on one box share Claude Code's session list, so SendMessage reaches them all
    mail_poll: float  # seconds between two looks at this Claude's doorbell (new messages for it or its sub-agents)
    mail_wake_after: int  # a --wake message nobody read after this many seconds starts someone to handle it
    mail_max_hops: int  # message-triggered runs in a row before messages stop waking anyone
    mail_wakes_per_hour: int  # wakes per recipient and hour
    bot: str  # FARM_BOT: this Claude is a bot, Claude Code on this model through another provider (see bots.py)
    bot_via: str  # the bot's provider, as people call it (OpenRouter)
    bot_takes: str  # a bot takes only the sub-agents sent to it ("sent"), or any ("any")
    policy: Policy

    @property
    def mail_dir(self) -> str:
        """Flag files, one per address with unread mail; the hook that delivers mail runs only when its flag exists."""
        return os.path.join(self.workspace, ".farm", "mail")

    @property
    def farm_id(self) -> str:
        return f"{self.name}@{socket.gethostname()}"

    @property
    def repo_dir(self) -> str:
        return os.path.join(self.workspace, "repo")


def _store_kind() -> str:
    kind = _env("FARM_STORE", "").lower()
    if kind in ("sqlite", "dynamodb"):
        return kind
    # joining a DynamoDB farm: an endpoint (DynamoDB Local) or an explicit table name implies it
    return "dynamodb" if os.environ.get("FARM_DYNAMODB_ENDPOINT") or os.environ.get("FARM_TABLE") else "sqlite"


def primary_name_file(workspace: str) -> str:
    return os.path.join(workspace, ".farm", "claude-name.json")


def _claude_name(farm: str, workspace: str) -> str:
    """Who this box's Claude is. An added Claude gets its name from the UI (FARM_NAME, marked FARM_HATCHED). The farm's
    own Claude is named after the account logged in to it (the daemon works that out at login and exports
    FARM_CLAUDE_NAME, and remembers it for commands run from outside); FARM_CLAUDE_NAME set by hand wins."""
    if os.environ.get("FARM_CLAUDE_NAME"):
        return os.environ["FARM_CLAUDE_NAME"]
    if not os.environ.get("FARM_HATCHED"):
        try:
            import json
            d = json.load(open(primary_name_file(workspace)))
            if d.get("farm") == farm and d.get("claude"):
                return d["claude"]
        except (OSError, ValueError):
            pass
    return farm


def load() -> Config:
    farm = _env("FARM_FARM", _env("FARM_NAME", "clodfarm"))  # an added Claude is told its farm's name
    workspace = _env("FARM_WORKSPACE", "/workspace")
    return Config(
        name=_claude_name(_env("FARM_NAME", "clodfarm"), workspace),
        farm=farm,
        store=_store_kind(),
        db_path=_env("FARM_DB", os.path.join(_env("FARM_WORKSPACE", "/workspace"), ".farm", "farm.db")),
        table=_env("FARM_TABLE", "clodfarm"),
        region=_env("AWS_REGION", _env("AWS_DEFAULT_REGION", "us-east-1")),
        endpoint=os.environ.get("FARM_DYNAMODB_ENDPOINT") or None,
        workspace=_env("FARM_WORKSPACE", "/workspace"),
        repo_url=os.environ.get("FARM_REPO_URL") or None,
        model=_env("FARM_MODEL", "opus"),
        permission_mode=_env("FARM_PERMISSION_MODE", "bypassPermissions"),
        claude_bin=_env("FARM_CLAUDE_BIN", "claude"),
        claude_update=int(_env("FARM_CLAUDE_UPDATE", "0")),
        task_timeout=int(_env("FARM_TASK_TIMEOUT", "5400")),
        lease_seconds=int(_env("FARM_LEASE_SECONDS", "300")),
        remote_control=_bool("FARM_REMOTE_CONTROL", True),
        rc_spawn=_env("FARM_RC_SPAWN", "worktree"),
        rc_capacity=int(_env("FARM_RC_CAPACITY", "4")),
        max_queue=int(_env("FARM_MAX_QUEUE", "25")),
        usage_refresh=int(_env("FARM_USAGE_REFRESH", "300")),
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
        ui=_bool("FARM_UI", True),
        manage_claude_config=_bool("FARM_MANAGE_CLAUDE_CONFIG", True),
        live_stdin=_bool("FARM_LIVE_STDIN", True),
        share_sessions=_bool("FARM_SHARE_SESSIONS", True),
        mail_poll=float(_env("FARM_MAIL_POLL", "2")),
        mail_wake_after=int(_env("FARM_MAIL_WAKE_AFTER", "120")),
        mail_max_hops=int(_env("FARM_MAIL_MAX_HOPS", "3")),
        mail_wakes_per_hour=int(_env("FARM_MAIL_WAKES_PER_HOUR", "6")),
        bot=_env("FARM_BOT", ""),
        bot_via=_env("FARM_BOT_VIA", ""),
        bot_takes="any" if _env("FARM_BOT_TAKES", "sent") == "any" else "sent",
        policy=Policy(
            max_workers=int(_env("FARM_MAX_WORKERS", "3")),
            min_workers=int(_env("FARM_MIN_WORKERS", "1")),
            weekly_target=float(_env("FARM_WEEKLY_TARGET", "0.80")),
            five_hour_ceiling=float(_env("FARM_FIVE_HOUR_CEILING", "0.85")),
            weekly_band=float(_env("FARM_WEEKLY_BAND", "0.05")),
            five_hour_band=float(_env("FARM_FIVE_HOUR_BAND", "0.30")),
            burst_hours=float(_env("FARM_BURST_HOURS", "0")),
            allow_overage=_bool("FARM_ALLOW_OVERAGE", False),
            # a bot has no subscription windows either: it is paced like an API key, and pauses when rate-limited
            api_mode=bool(os.environ.get("FARM_BOT")) or (bool(os.environ.get("ANTHROPIC_API_KEY"))
                                                        and not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")),
            daily_budget_usd=float(_env("FARM_DAILY_BUDGET_USD", "0")),
        ),
    )
