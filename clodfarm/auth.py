"""Claude subscription login: detect it, explain it, and prepare Claude Code for unattended use.

clodfarm never reads, copies or prints credentials. It only asks Claude Code
(`claude auth status`) whether it is logged in, and tells you how to log in.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile

from .prompts import farm_guide

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


def claude_name(status: dict) -> str | None:
    """A short name for the person behind a login: the part of its email before '@' (matan@jestr.ai -> matan)."""
    who = (status.get("email") or "").split("@")[0]
    slug = re.sub(r"[^a-z0-9]+", "-", who.lower()).strip("-")[:24].strip("-")
    return slug or None


def seat_id(status: dict) -> str:
    """A stable, readable id for the Claude account this box is logged in to (e.g. ``matan-3f2a``).
    Derived from what `claude auth status` reports; the full email is never stored. FARM_SEAT overrides it."""
    if os.environ.get("FARM_SEAT"):
        return re.sub(r"[^A-Za-z0-9_.-]", "-", os.environ["FARM_SEAT"])[:40]
    if "API" in (status.get("via") or ""):
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        return "api-" + hashlib.sha256(key.encode()).hexdigest()[:6]
    return seat_for(status.get("email") or status.get("orgId") or "")


def seat_for(who: str) -> str:
    """The seat id of an account email (``gil@x.io`` -> ``gil-3f2a``): how the farm matches a person to their Claude
    (e.g. a Slack sender) without storing anyone's email."""
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


def install_guide(config_dir: str | None = None):
    """Put the farm guide in the user-level CLAUDE.md so every session, including the
    ones you open through Remote Control, knows how the farm works (``config_dir``: another Claude's)."""
    path = os.path.join(config_dir or claude_home(), "CLAUDE.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        cur = open(path).read()
    except OSError:
        cur = ""
    block = f"{GUIDE_START}\n{farm_guide()}\n{GUIDE_END}"
    if GUIDE_START in cur and GUIDE_END in cur:
        pre, rest = cur.split(GUIDE_START, 1)
        new = pre + block + rest.split(GUIDE_END, 1)[1]
    else:
        new = (cur.rstrip() + "\n\n" if cur.strip() else "") + block + "\n"
    if new != cur:
        _atomic_write(path, new)


HOOK_CMD = "clodfarm hook"
# after every batch of tool calls; the shell test keeps it free (no Python starts) unless mail is waiting
MAIL_HOOK_CMD = '[ -e "${FARM_MAIL_FLAG:-/nonexistent}" ] && exec clodfarm hook; exit 0'
# the farm's mod (clodfarm/mod, install_mod) sets FARM_MOD_LIVE in the sessions that loaded it: it wakes an idle
# conversation and gates and logs SendMessage itself, so these stand down there (and stay for a session without it)
_UNLESS_MOD = '[ -n "${FARM_MOD_LIVE:-}" ] && exit 0; '
LISTEN_HOOK_CMD = _UNLESS_MOD + "exec clodfarm hook --listen"
# before every tool call; free (no Python starts) unless this Claude's person turned tools off (policy.py)
POLICY_HOOK_CMD = ('[ -s "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/farm-policy.json" ] && exec clodfarm hook --policy; '
                   'exit 0')
SEND_HOOK_CMD = _UNLESS_MOD + "exec clodfarm hook --policy"
SENT_HOOK_CMD = _UNLESS_MOD + "exec clodfarm hook"
LISTEN_SECONDS = 600  # an idle conversation is woken for mail this long after its last turn
_OLD_HOOKS = ("clodfarm inbox --hook", "clodfarm hook --listen", "clodfarm hook --policy")
HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd")


def farm_hooks() -> dict:
    """The hooks the farm puts in each Claude's Claude Code settings, by event.
      * every session event: record the session and its conversation in the farm's store; deliver messages at the
        next prompt, and before a turn ends (a Stop that blocks with the messages, so Claude reads them first)
      * after every batch of tool calls: deliver messages that arrived meanwhile (only when its flag file exists)
      * after SendMessage: log the message in the farm's event log, so the farm shows it
      * after a Remote Control conversation's turn: wait in the background and wake it when a message arrives
        (asyncRewake)
      * before a tool call: the tools its person turned off are denied (policy.py); a SendMessage to a Claude whose
        person approves every mission is sent through `clodfarm msg` instead, which asks them"""
    base = {"type": "command", "command": HOOK_CMD, "timeout": 15}
    out = {event: [{"hooks": [dict(base)]}] for event in HOOK_EVENTS}
    out["PostToolBatch"] = [{"hooks": [{"type": "command", "command": MAIL_HOOK_CMD, "timeout": 15}]}]
    out["PostToolUse"] = [{"matcher": "SendMessage", "hooks": [{"type": "command", "command": SENT_HOOK_CMD,
                                                                "timeout": 15}]}]
    out["PreToolUse"] = [{"matcher": "SendMessage", "hooks": [{"type": "command", "command": SEND_HOOK_CMD,
                                                               "timeout": 15}]},
                         {"hooks": [{"type": "command", "command": POLICY_HOOK_CMD, "timeout": 15}]}]
    out["Stop"].append({"hooks": [{"type": "command", "command": LISTEN_HOOK_CMD, "async": True, "asyncRewake": True,
                                   "timeout": LISTEN_SECONDS}]})
    return out


def _ours(group: dict) -> bool:
    cmds = {HOOK_CMD, MAIL_HOOK_CMD, LISTEN_HOOK_CMD, POLICY_HOOK_CMD, SEND_HOOK_CMD, SENT_HOOK_CMD, *_OLD_HOOKS}
    return any(h.get("command") in cmds for h in group.get("hooks", []))


def install_hooks():
    """Put the farm's hooks (``farm_hooks``) in this Claude's Claude Code settings, replacing those of older versions.
    Other settings and hooks are kept."""
    path = os.path.join(claude_home(), "settings.json")
    try:
        cfg = json.load(open(path))
    except (OSError, ValueError):
        cfg = {}
    hooks = cfg.setdefault("hooks", {})
    before = json.dumps(hooks, sort_keys=True)
    for event in list(hooks):
        hooks[event] = [g for g in hooks[event] if not _ours(g)]
    for event, groups in farm_hooks().items():
        hooks.setdefault(event, []).extend(groups)
    for event in [e for e, g in hooks.items() if not g]:
        del hooks[event]
    if json.dumps(hooks, sort_keys=True) != before:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _atomic_write(path, json.dumps(cfg, indent=2))


MOD_DIR = "farm-mod"  # in the Claude Code config dir: the farm's mod as this Claude's sessions load it


def install_mod() -> str:
    """Put the farm's mod (the ``mod`` folder of this package: a Claude Code plugin of function hooks) in this Claude's
    config dir and return its path, for CLAUDE_CODE_PLUGIN_DIRS. Only what changed is written, so the sessions
    watching it reload only for a new version; its tests stay behind."""
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mod")
    dst = os.path.join(claude_home(), MOD_DIR)
    want = set()
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in ("tests", "node_modules", "__pycache__")]
        for name in files:
            rel = os.path.relpath(os.path.join(root, name), src)
            want.add(rel)
            new = open(os.path.join(src, rel), "rb").read()
            path = os.path.join(dst, rel)
            try:
                if open(path, "rb").read() == new:
                    continue
            except OSError:
                pass
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(new)
            os.replace(tmp, path)
    engine = (os.path.join(".claude-plugin", "types") + os.sep, "tsconfig.json")  # what Claude Code lays beside it
    for root, _dirs, files in os.walk(dst):  # what an older version had and this one doesn't
        for name in files:
            rel = os.path.relpath(os.path.join(root, name), dst)
            if rel not in want and not rel.startswith(engine[0]) and rel != engine[1]:
                os.remove(os.path.join(root, name))
    return dst


def install_messaging():
    """Let this Claude's sessions take messages from the farm's other sessions (Claude Code's cross-session
    messaging) without holding them for an approval nobody is there to give: a sub-agent can't show the dialog, so a
    held message would expire unread. Only sessions of this container's own OS user can reach the inbox socket, and
    a message can never approve anything. A value set by hand (hold, refuse) is kept."""
    path = os.path.join(claude_home(), "settings.json")
    try:
        cfg = json.load(open(path))
    except (OSError, ValueError):
        cfg = {}
    if "crossSessionInbound" in cfg:
        return
    cfg["crossSessionInbound"] = "accept"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _atomic_write(path, json.dumps(cfg, indent=2))


def share_session_registry(config_dir: str, shared: str):
    """Make a Claude added in the farm UI list its live sessions where the farm's own Claude does.

    Claude Code finds the sessions it can message (ListAgents / SendMessage) in ``<config dir>/sessions``, and every
    Claude on the farm has its own config dir (its own login), so by default they can't see each other; delivery goes
    over a socket any session of the same OS user may use. Pointing each added Claude's ``sessions`` at ``shared``
    (the farm's own Claude's) puts every session on this box in one list. Only config dirs the farm created are
    changed; entries already there are moved over."""
    link = os.path.join(config_dir, "sessions")
    os.makedirs(shared, mode=0o700, exist_ok=True)
    if os.path.islink(link):
        if os.path.realpath(link) == os.path.realpath(shared):
            return
        os.remove(link)
    elif os.path.isdir(link):
        for name in os.listdir(link):
            src, dst = os.path.join(link, name), os.path.join(shared, name)
            if not os.path.exists(dst):
                os.replace(src, dst)
        shutil.rmtree(link, ignore_errors=True)
    os.symlink(shared, link)


def install_model(model: str):
    """Make FARM_MODEL this Claude's default model, so the sessions you open from the Claude app (Remote Control) use
    the same model as its sub-agents (which get `--model`). An alias such as `opus` follows the newest model of that
    family that the installed Claude Code knows. Other settings are kept."""
    path = os.path.join(claude_home(), "settings.json")
    try:
        cfg = json.load(open(path))
    except (OSError, ValueError):
        cfg = {}
    if not model or cfg.get("model") == model:
        return
    cfg["model"] = model
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _atomic_write(path, json.dumps(cfg, indent=2))


def install_browser_mcp(path: str | None = None, claude: str | None = "", unowned: bool = False) -> bool:
    """Give this Claude the farm's browser profiles as MCP tools: one server per profile (``browser`` for the default
    one, ``browser-<name>`` for the others), each Playwright attached to that profile's Chromium, so it works in the
    logins made from the farm UI. Servers of removed profiles go; a server of the same name set up by hand is kept.
    ``path``: another Claude's .claude.json (the farm UI updates every Claude when a profile is added or removed).
    Returns True if the config changed."""
    from . import browser, connectors
    p = path or claude_json_path()
    try:
        cfg = json.load(open(p))
    except (OSError, ValueError):
        cfg = {}
    servers = dict(cfg.get("mcpServers") or {})
    workspace = os.environ.get("FARM_WORKSPACE") or "/workspace"
    # its own profiles only (``claude``); the farm's own Claude ("") also gets the profiles nobody owns; and the
    # connectors every Claude shares (Stripe)
    want = {**browser.mcp_servers(claude=claude, unowned=unowned), **connectors.mcp_servers(workspace)}
    ours = lambda n, srv: ((n == browser.MCP_NAME or n.startswith(browser.MCP_NAME + "-")) and browser.is_ours(srv)) \
        or connectors.is_ours(n, srv)  # noqa: E731
    for name, server in list(servers.items()):
        if ours(name, server) and name not in want:
            del servers[name]
    for name, server in want.items():
        if servers.get(name) is None or ours(name, servers[name]):
            servers[name] = server
    if servers == (cfg.get("mcpServers") or {}):
        return False
    cfg["mcpServers"] = servers
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    _atomic_write(p, json.dumps(cfg, indent=2))
    return True


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


FARM_LOGIN_COMMAND = """---
description: Sign your person in to you on the farm UI (a one-time link for their phone)
---
Run `clodfarm pair` with Bash and give me the link it prints (I tap it on my phone and I'm signed in to you on the
farm) and the code (for another device: MY CLAUDE on the farm's page). Say it works once, for 10 minutes.
"""


def install_commands():
    """This Claude's `/farm-login`: in the Claude app, its person types it to sign in to it on the farm UI."""
    d = os.path.join(claude_home(), "commands")
    path = os.path.join(d, "farm-login.md")
    try:
        if open(path).read() == FARM_LOGIN_COMMAND:
            return
    except OSError:
        pass
    os.makedirs(d, exist_ok=True)
    _atomic_write(path, FARM_LOGIN_COMMAND)
