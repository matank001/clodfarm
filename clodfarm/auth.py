"""Claude subscription login: detect it, explain it, and prepare Claude Code for unattended use.

clodfarm never reads, copies or prints credentials. It only asks Claude Code
(`claude auth status`) whether it is logged in, and tells you how to log in.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile

from .prompts import FARM_GUIDE

GUIDE_START = "<!-- clodfarm:guide:start -->"
GUIDE_END = "<!-- clodfarm:guide:end -->"


def claude_home() -> str:
    return os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude")


def claude_json_path() -> str:
    d = os.environ.get("CLAUDE_CONFIG_DIR")
    return os.path.join(os.path.expanduser(d), ".claude.json") if d else os.path.expanduser("~/.claude.json")


def auth_status(claude_bin: str = "claude", config_dir: str | None = None) -> dict:
    """What `claude auth status` reports, plus how we are authenticated.
    With ``config_dir``: the login saved in that Claude config dir only (an agent added in the farm UI)."""
    env = None
    if config_dir:
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY",
                                                                  "ANTHROPIC_AUTH_TOKEN")}
        env["CLAUDE_CONFIG_DIR"] = config_dir
    try:
        p = subprocess.run([claude_bin, "auth", "status"], capture_output=True, text=True, timeout=30,
                           stdin=subprocess.DEVNULL, env=env)
    except FileNotFoundError:
        return {"loggedIn": False, "error": f"{claude_bin} not found"}
    except subprocess.TimeoutExpired:
        return {"loggedIn": False, "error": "claude auth status timed out"}
    try:
        st = json.loads(p.stdout or "{}")
    except ValueError:
        st = {"loggedIn": False, "error": (p.stdout + p.stderr).strip()[:300]}
    if config_dir:
        if st.get("loggedIn"):
            st["via"] = f"login saved in {config_dir}"
    elif os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        st["via"] = "CLAUDE_CODE_OAUTH_TOKEN (claude setup-token)"
        st.setdefault("loggedIn", True)
    elif os.environ.get("ANTHROPIC_API_KEY"):
        st["via"] = "ANTHROPIC_API_KEY (pay per token, not a subscription)"
    elif st.get("loggedIn"):
        st["via"] = f"login saved in {claude_home()}"
    return st


def seat_id(status: dict) -> str:
    """A stable, readable id for the Claude account this box is logged in to (e.g. ``matan-3f2a``).
    Derived from what `claude auth status` reports; the full email is never stored. FARM_SEAT overrides it."""
    if os.environ.get("FARM_SEAT"):
        return re.sub(r"[^A-Za-z0-9_.-]", "-", os.environ["FARM_SEAT"])[:40]
    if "API" in (status.get("via") or ""):
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        return "api-" + hashlib.sha256(key.encode()).hexdigest()[:6]
    who = status.get("email") or status.get("orgId") or ""
    if not who:
        return "default"
    local = re.sub(r"[^a-z0-9]", "", who.split("@")[0].lower())[:16] or "seat"
    return f"{local}-{hashlib.sha256(who.lower().encode()).hexdigest()[:4]}"


def banner(cfg) -> str:
    c = os.environ.get("FARM_CONTAINER_NAME", "clodfarm")
    return f"""
+--------------------------------------------------------------------------+
|  clodfarm: waiting for a Claude subscription login                      |
+--------------------------------------------------------------------------+
Pick one (details: docs/auth.md):

  A) Log in from wherever you are (works on a remote server):
       docker exec -it {c} clodfarm login
     It prints a URL. Open it on any device, approve, paste the code back.
     The login is saved in the claude-home volume and survives restarts.

  B) Use a long-lived token made on your own computer:
       claude setup-token            # on your laptop, prints a token
     then set CLAUDE_CODE_OAUTH_TOKEN=<token> in .env and restart.

  C) Reuse an existing Linux login: mount that machine's ~/.claude into
     /home/farm/.claude (macOS keeps its login in the Keychain: use A or B).

Checking again every few seconds...
"""


def install_guide():
    """Put the farm guide in the user-level CLAUDE.md so every session, including the
    ones you open through Remote Control, knows how the farm works."""
    path = os.path.join(claude_home(), "CLAUDE.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        cur = open(path).read()
    except OSError:
        cur = ""
    block = f"{GUIDE_START}\n{FARM_GUIDE}\n{GUIDE_END}"
    if GUIDE_START in cur and GUIDE_END in cur:
        pre, rest = cur.split(GUIDE_START, 1)
        new = pre + block + rest.split(GUIDE_END, 1)[1]
    else:
        new = (cur.rstrip() + "\n\n" if cur.strip() else "") + block + "\n"
    if new != cur:
        _atomic_write(path, new)


HOOK_CMD = "clodfarm inbox --hook"


def install_hooks():
    """Show a Claude the messages other Claudes sent it: a hook in its Claude Code settings runs `clodfarm inbox --hook`
    when a conversation starts and before each prompt; it prints new messages (nothing when there are none) and
    Claude Code adds them to the conversation. Other settings and hooks are kept."""
    path = os.path.join(claude_home(), "settings.json")
    try:
        cfg = json.load(open(path))
    except (OSError, ValueError):
        cfg = {}
    hooks, changed = cfg.setdefault("hooks", {}), False
    for event in ("SessionStart", "UserPromptSubmit"):
        groups = hooks.setdefault(event, [])
        if not any(h.get("command") == HOOK_CMD for g in groups for h in g.get("hooks", [])):
            groups.append({"hooks": [{"type": "command", "command": HOOK_CMD, "timeout": 15}]})
            changed = True
    if changed:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _atomic_write(path, json.dumps(cfg, indent=2))


def trust_directory(path: str):
    """Mark a folder as trusted and onboarding as done, so unattended sessions
    (Remote Control, headless workers) never stop at a first-run dialog."""
    p = claude_json_path()
    try:
        cfg = json.load(open(p))
    except (OSError, ValueError):
        cfg = {}
    proj = cfg.setdefault("projects", {}).setdefault(os.path.abspath(path), {})
    if proj.get("hasTrustDialogAccepted") and cfg.get("hasCompletedOnboarding"):
        return
    proj["hasTrustDialogAccepted"] = True
    cfg["hasCompletedOnboarding"] = True
    _atomic_write(p, json.dumps(cfg, indent=2))


def accept_remote_control():
    """Answer Claude Code's one-time "Enable Remote Control? (y/n)" prompt, which nobody can answer in a container.
    Only called when FARM_REMOTE_CONTROL is on, i.e. when you asked for Remote Control."""
    p = claude_json_path()
    try:
        cfg = json.load(open(p))
    except (OSError, ValueError):
        cfg = {}
    if cfg.get("remoteDialogSeen"):
        return
    cfg["remoteDialogSeen"] = True
    _atomic_write(p, json.dumps(cfg, indent=2))


def _atomic_write(path: str, text: str):
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".clodfarm-")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    try:
        os.chmod(tmp, os.stat(path).st_mode & 0o777)
    except OSError:
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)
