"""Connect the Claude Code sessions on your own computer to a farm: `clodfarm attach`.

    clodfarm attach https://<farm>        sign in (once); sessions started in this folder are on the farm
    clodfarm attach --only DIR [DIR ...]  those folders (adds to the ones attached before)
    clodfarm attach --all                 every session (`FARM=0 claude` leaves one out)
    clodfarm attach --manual              no session by itself: only `FARM=1 claude`, or /farm in a session
    clodfarm attach --status              what is connected
    clodfarm detach [--only DIR ...]      stop connecting this folder (those folders)
    clodfarm detach --all                 remove the hooks and end the connection

`attach` signs this computer in to the farm the way `claude mcp add` does (OAuth 2.1 with PKCE, on a page of the farm
where its person, signed in to their Claude there, names the computer). It puts one hook in Claude Code's user
settings, for when a session starts, at each prompt, when a turn ends and when the session ends, and a `/farm`
command. For a connected session the hook sends the conversation's new turns, scrubbed of secrets, to the farm's
/mcp/hook: the farm keeps it as a local session of this computer, under its person's Claude, and hands back the
messages waiting for this computer. A session that isn't connected sends nothing at all.

Which sessions are connected (the first that applies decides):
  1. /farm or /farm off typed in the session (the hook takes it; it never reaches the model);
  2. FARM=1 or FARM=0 in the environment `claude` was started with;
  3. `attach --all`;
  4. the folder the session started in is an attached folder, or inside one.
Turning a session on with /farm sends its conversation so far. Turning it off and on again skips what was said while
it was off.

It never gets in a session's way: a farm that is down or slow costs a session a few seconds at most, and turns that
couldn't be sent wait on disk for the next report. Standard library only (it runs on every prompt).
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import shlex
import socket
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qsl, urlencode, urlsplit

HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd")
HOOK_MARK = " hook --guest"
COMMAND_MARK = "<!-- clodfarm attach -->"
SID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
FARM_CMD = re.compile(r"^/farm(?:\s+(\S+))?\s*$")
TIMEOUT = 5  # seconds per request to the farm; Claude Code gives the hook 15
CHUNK_TURNS, CHUNK_CHARS = 300, 150_000  # per report: the farm takes 400 turns and 256 KB at most
MAX_PENDING = 3000  # turns kept on disk while the farm can't be reached; the oldest go first
MAIL_STOP_BLOCKS = 3  # a turn is kept going for new messages at most this often in a row
KEEP_STATE = 30 * 86400


class AttachError(Exception):
    pass


# ------------------------------------------------------------------- files
def home() -> str:
    """Where the connection and each session's progress are kept: $CLODFARM_HOME, else ~/.config/clodfarm."""
    d = os.environ.get("CLODFARM_HOME") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "clodfarm")
    return os.path.expanduser(d)


def _conf_path() -> str:
    return os.path.join(home(), "attach.json")


def _write(path: str, obj):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def _read(path: str) -> dict:
    try:
        d = json.load(open(path))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def load_conf() -> dict:
    return _read(_conf_path())


def save_conf(conf: dict):
    _write(_conf_path(), conf)


@contextlib.contextmanager
def _locked(name: str):
    os.makedirs(home(), mode=0o700, exist_ok=True)
    with open(os.path.join(home(), name + ".lock"), "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _state_path(sid: str) -> str:
    return os.path.join(home(), "sessions", sid + ".json")


# -------------------------------------------------------------------- HTTP
def _post(url: str, data: dict, token: str | None = None, form: bool = False) -> tuple[int, dict]:
    """(status, JSON body); status 0 when the farm can't be reached."""
    body = urlencode(data).encode() if form else json.dumps(data).encode()
    hdrs = {"Content-Type": "application/x-www-form-urlencoded" if form else "application/json",
            "Accept": "application/json", "User-Agent": "clodfarm-attach"}
    if token:
        hdrs["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except (OSError, ValueError):
        return 0, {}
    try:
        out = json.loads(raw or b"{}")
    except ValueError:
        out = {}
    return status, out if isinstance(out, dict) else {}


def farm_url(url: str) -> str:
    """The farm's public URL from what the person typed (its page, or its /mcp URL)."""
    u = (url or "").strip().rstrip("/")
    if u.endswith("/mcp"):
        u = u[:-4]
    p = urlsplit(u)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise AttachError(f"'{url}' isn't a farm's address (https://...)")
    if p.scheme == "http" and p.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise AttachError("a farm away from this computer must be reached over https")
    return u


def default_name() -> str:
    from .mcp import slug
    return slug(socket.gethostname().split(".")[0], "laptop")


# ----------------------------------------------------------------- sign in
def sign_in(url: str, name: str = "", open_browser=webbrowser.open, wait: float = 300) -> dict:
    """Connect this computer to the farm at ``url``: register, the farm's consent page in the browser (its person
    signs in there and names the computer), then trade the code for tokens. Returns the connection to keep."""
    pub = farm_url(url)
    got: dict = {}

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            u = urlsplit(self.path)
            if u.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            got.update(parse_qsl(u.query))
            text = ("Connected. You can close this tab." if got.get("code") else
                    f"Not connected ({got.get('error', 'no code')}). You can close this tab.").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(text)))
            self.end_headers()
            self.wfile.write(text)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Callback)
    try:
        redirect = f"http://127.0.0.1:{srv.server_address[1]}/callback"
        status, reg = _post(pub + "/oauth/register", {"client_name": f"clodfarm attach ({socket.gethostname()})"[:80],
                                                      "redirect_uris": [redirect], "token_endpoint_auth_method": "none"})
        if status != 201 or not reg.get("client_id"):
            raise AttachError(f"the farm at {pub} didn't take the connection "
                              f"({reg.get('error_description') or reg.get('error') or status or 'unreachable'})")
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        state = secrets.token_urlsafe(16)
        q = {"response_type": "code", "client_id": reg["client_id"], "redirect_uri": redirect,
             "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
             "scope": "farm:read farm:work", "resource": pub + "/mcp", "name": name or default_name()}
        link = pub + "/oauth/authorize?" + urlencode(q)
        print(f"Opening the farm in your browser to connect this computer. Sign in to your Claude there (MY CLAUDE) "
              f"if it asks.\nIf no browser opens, go to:\n\n  {link}\n", flush=True)
        try:
            open_browser(link)
        except Exception:  # noqa: BLE001 - the link is printed
            pass
        srv.timeout = 1
        end = time.time() + wait
        while not got and time.time() < end:
            srv.handle_request()
    finally:
        srv.server_close()
    if not got:
        raise AttachError("no answer from the browser in time; run it again")
    if got.get("state") != state:
        raise AttachError("the browser's answer doesn't match this sign-in; run it again")
    if got.get("iss") and got["iss"].rstrip("/") != pub:
        raise AttachError(f"the answer came from {got['iss']}, not {pub}")
    if not got.get("code"):
        raise AttachError("not connected: " + ("you turned it down" if got.get("error") == "access_denied"
                                               else got.get("error") or "no code"))
    status, tok = _post(pub + "/oauth/token", {"grant_type": "authorization_code", "code": got["code"],
                                               "code_verifier": verifier, "client_id": reg["client_id"],
                                               "redirect_uri": redirect, "resource": pub + "/mcp"}, form=True)
    if status != 200 or not tok.get("access_token"):
        raise AttachError(f"the farm didn't give a token ({tok.get('error') or status})")
    return {"url": pub, "client_id": reg["client_id"], "name": tok.get("name") or name or default_name(),
            "scope": tok.get("scope", ""), "access": tok["access_token"],
            "access_exp": time.time() + int(tok.get("expires_in") or 3600) - 60,
            "refresh": tok.get("refresh_token", ""), "connected": int(time.time())}


def _token(conf: dict, stale: str | None = None) -> str | None:
    """A working access token, refreshed when it ran out (``stale``: the farm turned this one down). One process
    refreshes at a time: refresh tokens rotate, and the farm ends a connection whose old one is used twice."""
    if conf.get("access") and conf.get("access_exp", 0) > time.time() and conf["access"] != stale:
        return conf["access"]
    with _locked("attach"):
        fresh = load_conf()
        if fresh.get("url") != conf.get("url"):
            return None
        conf.clear()
        conf.update(fresh)
        if conf.get("access") and conf.get("access_exp", 0) > time.time() and conf["access"] != stale:
            return conf["access"]  # another session's hook refreshed it meanwhile
        if not conf.get("refresh") or conf.get("ended"):
            return None
        status, tok = _post(conf["url"] + "/oauth/token", {"grant_type": "refresh_token", "client_id": conf["client_id"],
                                                           "refresh_token": conf["refresh"]}, form=True)
        if status == 200 and tok.get("access_token"):
            conf.update(access=tok["access_token"], refresh=tok.get("refresh_token") or conf["refresh"],
                        access_exp=time.time() + int(tok.get("expires_in") or 3600) - 60)
            save_conf(conf)
            return conf["access"]
        if status == 400:  # disconnected on the farm, or the refresh token ran out: sign in again
            conf.update(ended=int(time.time()), access="", refresh="")
            save_conf(conf)
        return None


def report(conf: dict, body: dict) -> dict | None:
    """Send one report to the farm's /mcp/hook. None when it couldn't be delivered."""
    stale = None
    for _ in range(2):
        tok = _token(conf, stale)
        if not tok:
            return None
        status, out = _post(conf["url"] + "/mcp/hook", body, tok)
        if status == 401:
            stale = tok
            continue
        return out if status == 200 else None
    return None


# --------------------------------------------------------------- the hook
def connected(conf: dict, st: dict, env=os.environ) -> tuple[bool, str]:
    """Whether a session is on the farm, and why (see the module's doc)."""
    if st.get("on") is not None:
        return bool(st["on"]), "/farm" if st["on"] else "/farm off"
    if env.get("FARM") in ("0", "1"):
        return env["FARM"] == "1", "FARM=" + env["FARM"]
    if conf.get("everywhere"):
        return True, "attach --all"
    cwd = os.path.realpath(st.get("cwd") or "/nonexistent")
    for d in conf.get("folders") or []:
        if cwd == d or cwd.startswith(d.rstrip(os.sep) + os.sep):
            return True, f"it started in {d}"
    return False, "it didn't start in an attached folder"


def _read_new(st: dict):
    """The transcript's new turns, scrubbed, onto the session's pending list (kept on disk until the farm has them)."""
    from . import scrub
    from .sessions import read_transcript
    path = st.get("transcript")
    if not path:
        return
    turns, off, meta = read_transcript(path, int(st.get("offset", 0)))
    st["offset"] = off
    if meta.get("title"):
        st["title"] = scrub.text(meta["title"], [])
    pending = st.setdefault("pending", [])
    pending += [{**t, "text": scrub.text(t["text"], [])} for t in turns]
    if len(pending) > MAX_PENDING:
        del pending[:len(pending) - MAX_PENDING]


def _send(conf: dict, st: dict, sid: str, event: str, mail: bool = False, **extra) -> dict | None:
    """Report the session, with its pending turns in as many reports as they take. The last carries ``mail``."""
    pending = st.setdefault("pending", [])
    while True:
        n = chars = 0
        while n < len(pending) and n < CHUNK_TURNS and chars + len(pending[n]["text"]) <= CHUNK_CHARS:
            chars += len(pending[n]["text"])
            n += 1
        n = max(n, min(1, len(pending)))
        last = n == len(pending)
        body = {"session": sid, "event": event, "cwd": st.get("cwd"), "title": st.get("title"),
                "turns": pending[:n], "mail": mail and last, **extra}
        out = report(conf, body)
        if out is None:
            return None
        del pending[:n]
        st["sent"] = True
        if last:
            return out


def _farm_command(conf: dict, st: dict, sid: str, arg: str, env) -> str:
    """/farm, /farm off, /farm status: done here, and the prompt is blocked (shown to the person, never sent)."""
    arg = (arg or "on").lower()
    if not conf.get("url"):
        text = "This computer isn't attached to a farm. In a terminal: clodfarm attach https://<your farm>"
    elif conf.get("ended"):
        text = (f"The farm at {conf['url']} ended this computer's connection. Connect again in a terminal: "
                f"clodfarm attach {conf['url']}")
    elif arg in ("on", "connect"):
        if st.get("on") is False and st.get("sent"):  # back on: what was said while it was off stays here
            _read_new(st)
            st["pending"] = []
        st["on"] = True
        _read_new(st)
        ok = _send(conf, st, sid, "Connect")
        text = (f"This session is on the farm ({conf['url']}) as {conf.get('name')}: its conversation shows there "
                f"under your Claude, and messages for {conf.get('name')} arrive here. /farm off ends it."
                if ok else "This session is connected; the farm can't be reached right now, so its conversation goes "
                "with the next report.")
    elif arg in ("off", "disconnect"):
        was = connected(conf, st, env)[0]
        if was and st.get("sent"):
            _read_new(st)
            _send(conf, st, sid, "Disconnect", reason="/farm off")
        st["on"] = False
        st["pending"] = []
        text = "This session is off the farm: nothing more of it is sent. /farm connects it again."
    else:
        on, why = connected(conf, st, env)
        text = (f"This session is {'on' if on else 'not on'} the farm ({conf['url']}, as {conf.get('name')}): {why}. "
                "/farm connects it, /farm off ends it.")
    return json.dumps({"decision": "block", "reason": "[farm] " + text})


def hook(ev: dict, env=os.environ) -> str | None:
    """One Claude Code hook event (the JSON on stdin). Returns what to print: messages for Claude, or a decision."""
    sid, event = str(ev.get("session_id") or ""), ev.get("hook_event_name") or ""
    if not SID_RE.match(sid) or event not in HOOK_EVENTS:
        return None
    conf = load_conf()
    m = FARM_CMD.match((ev.get("prompt") or "").strip()) if event == "UserPromptSubmit" else None
    if not conf.get("url") and not m:
        return None
    path = _state_path(sid)
    with _locked("session-" + sid):
        st = _read(path)
        new = not st
        st.setdefault("cwd", ev.get("cwd") or os.getcwd())
        st["transcript"] = ev.get("transcript_path") or st.get("transcript")
        st["at"] = time.time()
        if event == "SessionStart" and new:
            _prune()
        try:
            if m:
                return _farm_command(conf, st, sid, m.group(1) or "", env)
            if conf.get("ended") or not connected(conf, st, env)[0]:
                return None
            _read_new(st)
            if event == "SessionStart":
                if not _send(conf, st, sid, event):
                    return None
                return (f"[farm] This session is connected to the farm '{conf['url']}' as '{conf.get('name')}': its "
                        "conversation shows there, and messages from the farm's Claudes arrive here. Answer one with "
                        "the farm MCP server's farm_msg tool, if it is set up.")
            if event == "UserPromptSubmit":
                out = _send(conf, st, sid, event, mail=True)
                return (out or {}).get("mail") or None
            if event == "Stop":
                blocks = int(st.get("blocks", 0)) if ev.get("stop_hook_active") else 0
                out = _send(conf, st, sid, event, mail=blocks < MAIL_STOP_BLOCKS)
                st["blocks"] = blocks + 1 if (out or {}).get("mail") else 0
                if (out or {}).get("mail"):
                    return json.dumps({"decision": "block", "reason": out["mail"]})
                return None
            _send(conf, st, sid, event, reason=str(ev.get("reason") or "")[:80] or None)
            return None
        finally:
            _write(path, st)


def _prune():
    d = os.path.join(home(), "sessions")
    try:
        names = os.listdir(d)
    except OSError:
        return
    old = time.time() - KEEP_STATE
    for n in names:
        p = os.path.join(d, n)
        try:
            if os.path.getmtime(p) < old:
                os.remove(p)
        except OSError:
            pass


def run_hook() -> int:
    """`clodfarm hook --guest`: never fails the session. A problem goes to stderr and it exits 0."""
    try:
        ev = json.loads(sys.stdin.read() or "{}")
        out = hook(ev if isinstance(ev, dict) else {})
        if out:
            print(out)
    except Exception as e:  # noqa: BLE001
        print(f"clodfarm attach: {e!r}"[:300], file=sys.stderr)
    return 0


# ------------------------------------------------------ install, uninstall
def hook_command() -> str:
    """This Python running this clodfarm: no PATH needed in the hook's shell."""
    from .boot import STUB
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(STUB)}{HOOK_MARK}"


def _ours(group: dict) -> bool:
    return any(str(h.get("command", "")).endswith(HOOK_MARK) for h in group.get("hooks", []))


def _settings_path() -> str:
    from .auth import claude_home
    return os.path.join(claude_home(), "settings.json")


def _command_path() -> str:
    from .auth import claude_home
    return os.path.join(claude_home(), "commands", "farm.md")


COMMAND_TEXT = f"""---
description: Put this session on the farm (/farm), take it off (/farm off), or see whether it is (/farm status)
argument-hint: "[on|off|status]"
---
{COMMAND_MARK}
The clodfarm hook didn't take this command, so this computer's Claude Code isn't attached to a farm any more. Tell the
user in one sentence to run `clodfarm attach https://<their farm>` in a terminal to attach it again. Do nothing else.
"""


def _edit_settings(add: bool):
    from .auth import _atomic_write
    path = _settings_path()
    try:
        cfg = json.load(open(path))
    except FileNotFoundError:
        cfg = {}
    except ValueError as e:
        raise AttachError(f"{path} isn't valid JSON ({e}); fix it first") from None
    hooks = cfg.setdefault("hooks", {})
    before = json.dumps(hooks, sort_keys=True)
    for event in list(hooks):
        hooks[event] = [g for g in hooks[event] if not _ours(g)]
    if add:
        for event in HOOK_EVENTS:
            hooks.setdefault(event, []).append({"hooks": [{"type": "command", "command": hook_command(), "timeout": 15}]})
    for event in [e for e, g in hooks.items() if not g]:
        del hooks[event]
    if not hooks:
        del cfg["hooks"]
    if json.dumps(hooks, sort_keys=True) != before:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.exists(path):
            open(path, "w").close()
        _atomic_write(path, json.dumps(cfg, indent=2) + "\n")


def install():
    """The hook in Claude Code's user settings (other settings and hooks are kept) and the /farm command."""
    _edit_settings(True)
    path = _command_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path) or COMMAND_MARK in open(path).read():
        with open(path, "w") as f:
            f.write(COMMAND_TEXT)


def uninstall():
    _edit_settings(False)
    path = _command_path()
    try:
        if COMMAND_MARK in open(path).read():
            os.remove(path)
    except OSError:
        pass


def installed() -> bool:
    try:
        hooks = json.load(open(_settings_path())).get("hooks", {})
    except (OSError, ValueError):
        return False
    return all(any(_ours(g) for g in hooks.get(e, [])) for e in HOOK_EVENTS)


# ------------------------------------------------------------------ commands
def _status(conf: dict) -> str:
    if not conf.get("url"):
        return "This computer isn't attached to a farm: clodfarm attach https://<your farm>"
    lines = [f"Farm:      {conf['url']}", f"As:        {conf.get('name')} ({conf.get('scope') or '?'})"]
    if conf.get("ended"):
        lines.append(f"           the farm ended this connection: clodfarm attach {conf['url']}")
    if conf.get("everywhere"):
        lines.append("Sessions:  every one (FARM=0 claude leaves one out)")
    elif conf.get("folders"):
        lines.append("Sessions:  the ones started in")
        lines += [f"             {d}" for d in conf["folders"]]
    else:
        lines.append("Sessions:  only FARM=1 claude, or /farm in a session")
    lines.append(f"Hooks:     {'in ' + _settings_path() if installed() else 'missing: run clodfarm attach again'}")
    return "\n".join(lines)


def cmd_attach(a) -> int:
    conf = load_conf()
    if a.status:
        print(json.dumps({k: v for k, v in conf.items() if k not in ("access", "refresh")}, indent=1)
              if a.json else _status(conf))
        return 0
    try:
        if a.url:
            pub = farm_url(a.url)
            if pub != conf.get("url") or conf.get("ended") or not conf.get("refresh"):
                if conf.get("refresh") and not conf.get("ended"):  # another farm before: end that connection
                    _post(conf["url"] + "/oauth/revoke", {"token": conf["refresh"]}, form=True)
                keep = {k: conf[k] for k in ("folders", "everywhere") if k in conf}  # this computer's choice
                conf = {**sign_in(pub, a.name or ""), **keep}
        elif not conf.get("url") or conf.get("ended"):
            raise AttachError("give the farm's address: clodfarm attach https://<your farm>")
        if a.all:
            conf["everywhere"] = True
        elif a.manual:
            conf["everywhere"] = False
        else:
            folders = conf.setdefault("folders", [])
            for d in a.only or [os.getcwd()]:
                d = os.path.realpath(os.path.expanduser(d))
                if not os.path.isdir(d):
                    raise AttachError(f"{d} isn't a folder")
                if d not in folders:
                    folders.append(d)
        save_conf(conf)
        install()
    except AttachError as e:
        print(f"clodfarm attach: {e}", file=sys.stderr)
        return 1
    print(_status(conf))
    print("\nNew sessions follow this. In a session that's already running, type /farm (or /farm off).")
    return 0


def cmd_detach(a) -> int:
    conf = load_conf()
    if a.all:
        if conf.get("refresh") and not conf.get("ended"):
            _post(conf["url"] + "/oauth/revoke", {"token": conf["refresh"]}, form=True)
        uninstall()
        for p in (_conf_path(),):
            with contextlib.suppress(OSError):
                os.remove(p)
        d = os.path.join(home(), "sessions")
        for n in (os.listdir(d) if os.path.isdir(d) else []):
            with contextlib.suppress(OSError):
                os.remove(os.path.join(d, n))
        print("Detached: the hooks and /farm are gone, and the farm ended this computer's connection.")
        return 0
    folders = conf.get("folders") or []
    gone = []
    for d in a.only or [os.getcwd()]:
        d = os.path.realpath(os.path.expanduser(d))
        if d in folders:
            folders.remove(d)
            gone.append(d)
    if not gone:
        print("That folder isn't attached (clodfarm attach --status). `clodfarm detach --all` removes everything.",
              file=sys.stderr)
        return 1
    save_conf(conf)
    print(f"New sessions in {', '.join(gone)} stay off the farm.")
    if conf.get("everywhere"):
        print("Every session is still connected (attach --all): `clodfarm attach --manual` stops that.")
    return 0
