"""Claude subscription login: detect it, explain it, and prepare Claude Code for unattended use.

claude-farm never reads, copies or prints credentials. It only asks Claude Code
(`claude auth status`) whether it is logged in, and tells you how to log in.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile

from .prompts import FARM_GUIDE

GUIDE_START = "<!-- claude-farm:guide:start -->"
GUIDE_END = "<!-- claude-farm:guide:end -->"


def claude_home() -> str:
    return os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude")


def claude_json_path() -> str:
    d = os.environ.get("CLAUDE_CONFIG_DIR")
    return os.path.join(os.path.expanduser(d), ".claude.json") if d else os.path.expanduser("~/.claude.json")


def auth_status(claude_bin: str = "claude") -> dict:
    """What `claude auth status` reports, plus how we are authenticated."""
    try:
        p = subprocess.run([claude_bin, "auth", "status"], capture_output=True, text=True, timeout=30,
                           stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return {"loggedIn": False, "error": f"{claude_bin} not found"}
    except subprocess.TimeoutExpired:
        return {"loggedIn": False, "error": "claude auth status timed out"}
    try:
        st = json.loads(p.stdout or "{}")
    except ValueError:
        st = {"loggedIn": False, "error": (p.stdout + p.stderr).strip()[:300]}
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        st["via"] = "CLAUDE_CODE_OAUTH_TOKEN (claude setup-token)"
        st.setdefault("loggedIn", True)
    elif os.environ.get("ANTHROPIC_API_KEY"):
        st["via"] = "ANTHROPIC_API_KEY (pay per token, not a subscription)"
    elif st.get("loggedIn"):
        st["via"] = f"login saved in {claude_home()}"
    return st


def banner(cfg) -> str:
    c = os.environ.get("FARM_CONTAINER_NAME", "claude-farm")
    return f"""
+--------------------------------------------------------------------------+
|  claude-farm: waiting for a Claude subscription login                      |
+--------------------------------------------------------------------------+
Pick one (details: docs/auth.md):

  A) Log in from wherever you are (works on a remote server):
       docker exec -it {c} claude-farm login
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


def _atomic_write(path: str, text: str):
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".claude-farm-")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    try:
        os.chmod(tmp, os.stat(path).st_mode & 0o777)
    except OSError:
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)
