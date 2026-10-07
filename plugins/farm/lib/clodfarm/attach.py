"""Connect the Claude Code sessions on your own computer to a farm: the `farm` plugin, or `clodfarm attach`.

In Claude Code, with the plugin (plugins/farm, `claude plugin install farm@clodfarm`), whose commands Claude Code
names after it:

    /farm:connect https://<farm> [NAME]   connect this computer (a browser page, once); this session goes on
    /farm:on  ·  /farm:off  ·  /farm:status   this session on the farm, off it, or which it is and why
    /farm:folder [off]                    sessions started in this folder go on the farm (or stop)
    /farm:everywhere [off]                every session does (`FARM=0 claude` leaves one out)
    /farm:sign-out  ·  /farm:help         end this computer's connection · all of this

The same as `/farm connect ...`, `/farm off`, and so on, where the command is the user's own (`clodfarm attach`).

From a shell, with clodfarm installed (its hook goes in Claude Code's user settings instead of a plugin):

    clodfarm attach https://<farm> [--only DIR ... | --all | --manual] · attach --status · detach [--all]

Connecting signs the computer in to the farm the way `claude mcp add` does (OAuth 2.1 with PKCE, on a page of the
farm where its person, signed in to their Claude there, names the computer); after that the hook refreshes its token
by itself, and the browser is needed again only if the connection ends (30 days unused, or disconnected on the farm).
Claude Code calls the hook when a session starts, at each prompt, when a turn ends and when the session ends. For a
connected session it sends the conversation's new turns, scrubbed of secrets, to the farm's /mcp/hook: the farm keeps
it as a local session of this computer, under its person's Claude, and hands back the messages waiting for this
computer. A session that isn't connected sends nothing at all.

Which sessions are connected (the first that applies decides):
  1. /farm:on or /farm:off typed in the session (the hook takes it; it never reaches the model);
  2. FARM=1 or FARM=0 in the environment `claude` was started with;
  3. everywhere (/farm:everywhere, `attach --all`);
  4. the folder the session started in is a connected folder, or inside one.
Turning a session on sends its conversation so far. Turning it off and on again skips what was said while
it was off.

It never gets in a session's way: a farm that is down or slow costs a session a few seconds at most, and turns that
couldn't be sent wait on disk for the next report. Standard library only, Python 3.9 and up (macOS's own python3),
and nothing from the rest of clodfarm but sessions.py and scrub.py: the plugin carries a copy of these three files.
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
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qsl, urlencode, urlsplit

HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolBatch", "Stop", "SessionEnd")
LISTEN_SECONDS = 600  # an idle session is woken for the farm's messages this long after its last turn, as on the farm
# the policy file's path in a hook's shell (PreToolUse starts no Python while nothing is turned off)
POLICY_SH = '${CLODFARM_HOME:-${XDG_CONFIG_HOME:-$HOME/.config}/clodfarm}/policy.json'
HOOK_MARK = " hook --guest"
COMMAND_MARK = "<!-- clodfarm attach -->"
SID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
FARM_CMD = re.compile(r"^/farm(?::([A-Za-z-]+))?(?:\s+(.*))?$")  # the plugin's /farm:off, or /farm off
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")  # a computer's name on the farm (as mcp.NAME_RE)
SIGN_IN_WAIT = 300
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
    """One process at a time: ``attach`` (the connection), or ``sessions/<id>`` (one session's progress)."""
    path = os.path.join(home(), name + ".lock")
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    with open(path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _state_path(sid: str) -> str:
    return os.path.join(home(), "sessions", sid + ".json")


def _policy_path() -> str:
    return os.path.join(home(), "policy.json")


def _listener_path(sid: str) -> str:
    return os.path.join(home(), "sessions", sid + ".listen")


# -------------------------------------------------------------------- HTTP
def _post(url: str, data: dict, token: str | None = None, form: bool = False, timeout: float = TIMEOUT
          ) -> tuple[int, dict]:
    """(status, JSON body); status 0 when the farm can't be reached."""
    body = urlencode(data).encode() if form else json.dumps(data).encode()
    hdrs = {"Content-Type": "application/x-www-form-urlencoded" if form else "application/json",
            "Accept": "application/json", "User-Agent": "clodfarm-attach"}
    if token:
        hdrs["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
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
    s = re.sub(r"[^a-z0-9._-]+", "-", socket.gethostname().split(".")[0].lower()).strip("-._")[:32]
    return s if NAME_RE.match(s) else "laptop"


# ----------------------------------------------------------------- sign in
def sign_in(url: str, name: str = "", open_browser=webbrowser.open, wait: float = SIGN_IN_WAIT, show=None) -> dict:
    """Connect this computer to the farm at ``url``: register, the farm's consent page in the browser (its person
    signs in there and names the computer), then trade the code for tokens. Returns the connection to keep.
    ``show(link)`` tells the person where to go (default: printed)."""
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
        if show:
            show(link)
        else:
            print(f"Opening the farm in your browser to connect this computer. Sign in to your Claude there (MY "
                  f"CLAUDE) if it asks.\nIf no browser opens, go to:\n\n  {link}\n", flush=True)
        def browse():  # beside the wait: a browser named in $BROWSER keeps open() until it exits
            try:
                open_browser(link)
            except Exception:  # noqa: BLE001 - the link is shown
                pass
        threading.Thread(target=browse, daemon=True).start()
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


def _signin_path() -> str:
    return os.path.join(home(), "signin.json")


def keep_connection(got: dict):
    """Save a new connection, keeping this computer's choice of sessions; a connection to another farm ends."""
    with _locked("attach"):
        conf = load_conf()
        if conf.get("refresh") and not conf.get("ended") and conf.get("url") != got["url"]:
            _post(conf["url"] + "/oauth/revoke", {"token": conf["refresh"]}, form=True)
        save_conf({**got, **{k: conf[k] for k in ("folders", "everywhere") if k in conf}})


def start_sign_in(url: str, name: str = "") -> dict:
    """Sign in from a hook, which can't wait for a person: a process of its own does it (the browser, then the farm's
    answer, for up to SIGN_IN_WAIT seconds) and saves the connection. Returns where it got to after a few seconds:
    {state: waiting, link} normally, or {state: failed, error}."""
    pub = farm_url(url)
    _write(_signin_path(), {"url": pub, "state": "starting", "at": time.time()})
    lib = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = f"import sys; sys.path.insert(0, {lib!r}); from clodfarm.attach import _sign_in_child; _sign_in_child()"
    with open(os.path.join(home(), "signin.log"), "w") as log:  # what went wrong, if it does
        subprocess.Popen([sys.executable, "-c", code, pub, name or ""], stdin=subprocess.DEVNULL, stdout=log,
                         stderr=log, start_new_session=True, close_fds=True)
    end = time.time() + 4
    while time.time() < end:
        st = _read(_signin_path())
        if st.get("link") or st.get("state") in ("failed", "done"):
            return st
        time.sleep(0.1)
    return _read(_signin_path())


def _sign_in_child():
    pub, name = sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else ""
    show = lambda link: _write(_signin_path(), {"url": pub, "state": "waiting", "link": link,  # noqa: E731
                                                "at": time.time(), "pid": os.getpid()})
    try:
        got = sign_in(pub, name, show=show)
        keep_connection(got)
        _write(_signin_path(), {"url": pub, "state": "done", "name": got["name"], "at": time.time()})
    except Exception as e:  # noqa: BLE001 - the person reads it with /farm status
        _write(_signin_path(), {"url": pub, "state": "failed", "error": str(e)[:300], "at": time.time()})


def sign_in_state() -> dict:
    """The last background sign-in; one still waiting past its time has failed."""
    st = _read(_signin_path())
    if st.get("state") in ("starting", "waiting") and time.time() - float(st.get("at", 0)) > SIGN_IN_WAIT + 30:
        st = {**st, "state": "failed", "error": "no answer from the browser in time"}
    return st


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


def _authed(conf: dict, path: str, body: dict, timeout: float = TIMEOUT) -> tuple[int, dict]:
    """POST to the farm with the connection's token, refreshed once when the farm turns it down."""
    stale = None
    for _ in range(2):
        tok = _token(conf, stale)
        if not tok:
            return 401, {}
        status, out = _post(conf["url"] + path, body, tok, timeout=timeout)
        if status != 401:
            return status, out
        stale = tok
    return 401, {}


def report(conf: dict, body: dict, timeout: float = TIMEOUT) -> dict | None:
    """Send one report to the farm's /mcp/hook. None when it couldn't be delivered."""
    status, out = _authed(conf, "/mcp/hook", body, timeout)
    if status != 200:
        return None
    _remember(conf, out)
    return out


def _remember(conf: dict, out: dict):
    """What the farm's answer says about this computer: which Claude it is, and what that Claude's person turned
    off (kept in policy.json, which only exists while something is off: then PreToolUse asks this code)."""
    if out.get("as") and out["as"] != conf.get("as"):
        conf["as"] = out["as"]
        _change_conf(lambda c: c.update({"as": out["as"]}) if c.get("url") == conf.get("url") else None)
    deny = sorted((out.get("policy") or {}).get("deny") or [])
    path = _policy_path()
    if deny != sorted(_read(path).get("deny") or []):
        if deny:
            _write(path, {"deny": deny})
        else:
            with contextlib.suppress(OSError):
                os.remove(path)


# --------------------------------------------------------------- the hook
def connected(conf: dict, st: dict, env=os.environ) -> tuple[bool, str]:
    """Whether a session is on the farm, and why (see the module's doc)."""
    if st.get("on") is not None:
        return bool(st["on"]), _c("on") if st["on"] else _c("off")
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
    from .sessions import usage_total
    turns, off, meta = read_transcript(path, int(st.get("offset", 0)))
    st["offset"] = off
    per = meta.get("usage") or {}
    if per:  # tokens used since the last read (a message split over two reads counts once), sent with the turns
        got = usage_total(per, skip=st.get("usage_last") or "")
        st["usage_last"] = list(per)[-1]
        st["usage"] = {k: int((st.get("usage") or {}).get(k, 0)) + v for k, v in got.items()}
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
                "turns": pending[:n], "mail": mail and last, "usage": st.get("usage") or None, **extra}
        out = report(conf, body)
        if out is None:
            return None
        del pending[:n]
        st.pop("usage", None)
        st["sent"] = True
        if last:
            return out


def _c(verb: str = "on") -> str:
    """How the person types a /farm command: the plugin's are /farm:<verb>; `clodfarm attach`'s own is /farm <verb>."""
    if os.environ.get("CLODFARM_PLUGIN"):
        return "/farm:" + verb
    return "/farm" if verb == "on" else "/farm " + verb


def _help() -> str:
    return (f"{_c('on')} puts this session on the farm, {_c('off')} takes it off, {_c('status')} says which and why. "
            f"{_c('connect')} https://<farm> [name] connects this computer (once), {_c('folder')} [off] puts the "
            f"sessions started in this folder on the farm, {_c('everywhere')} [off] every session, and "
            f"{_c('sign-out')} ends this computer's connection.")


def _change_conf(fn) -> dict:
    with _locked("attach"):
        conf = load_conf()
        fn(conf)
        save_conf(conf)
        return conf


def _turn_on(conf: dict, st: dict, sid: str) -> str:
    if st.get("on") is False and st.get("sent"):  # back on: what was said while it was off stays here
        _read_new(st)
        st["pending"] = []
    st["on"] = True
    _read_new(st)
    if _send(conf, st, sid, "Connect"):
        return (f"This session is on the farm ({conf['url']}) as {conf.get('name')}: its conversation shows there "
                f"under your Claude, and messages for {conf.get('name')} arrive here. {_c('off')} ends it.")
    return "This session is on the farm; the farm can't be reached right now, so its conversation goes with the next report."


def _not_connected() -> str:
    si = sign_in_state()
    if si.get("state") in ("starting", "waiting"):
        return ("Waiting for you to allow this computer on the farm's page in your browser"
                + (f": {si['link']}" if si.get("link") else "."))
    if si.get("state") == "failed":
        return f"Connecting to {si.get('url')} failed: {si.get('error')}. {_c('connect')} {si.get('url')} tries again."
    return f"This computer isn't connected to a farm yet: {_c('connect')} https://<your farm>"


def _connect(conf: dict, st: dict, sid: str, args: list) -> str:
    url = args[0] if args else conf.get("url") or ""
    if not url:
        return f"Which farm? {_c('connect')} https://<your farm> [a name for this computer]"
    pub = farm_url(url)
    if pub == conf.get("url") and conf.get("refresh") and not conf.get("ended"):
        return f"This computer is already connected to {pub} as {conf.get('name')}. " + _turn_on(conf, st, sid)
    if len(args) > 1 and not NAME_RE.match(args[1]):
        return "A computer's name is lowercase letters, digits, . _ or -, starting with a letter or digit."
    st["on"] = True  # this session goes on as soon as the computer is connected
    si = start_sign_in(pub, args[1] if len(args) > 1 else "")
    if si.get("state") == "failed":
        return f"Couldn't connect to {pub}: {si.get('error')}"
    return ("Opening the farm in your browser: allow this computer there (sign in to your Claude, MY CLAUDE, if it "
            f"asks). This session goes on the farm once it's connected; {_c('status')} shows how it's going."
            + (f" If no browser opened, go to {si['link']}" if si.get("link") else ""))


def _farm_command(conf: dict, st: dict, sid: str, args: list, env) -> str:
    """A /farm command: done here, and the prompt is blocked (shown to the person, never sent to the model)."""
    verb, rest = (args[0].lower(), args[1:]) if args else ("on", [])
    try:
        if verb in ("help", "?"):
            text = _help()
        elif verb == "connect":
            text = _connect(conf, st, sid, rest)
        elif not conf.get("url"):
            if verb == "on":
                st["on"] = True
            text = _not_connected()
        elif verb in ("sign-out", "signout", "logout"):
            if conf.get("refresh") and not conf.get("ended"):
                _post(conf["url"] + "/oauth/revoke", {"token": conf["refresh"]}, form=True)
            def forget(c):  # this computer's choice of sessions stays, for the next connection
                for k in [k for k in c if k not in ("folders", "everywhere")]:
                    del c[k]
            _change_conf(forget)
            text = f"Signed out: this computer's connection to {conf['url']} is over. {_c('connect')} starts a new one."
        elif conf.get("ended"):
            text = (f"The farm at {conf['url']} ended this computer's connection (unused for 30 days, or "
                    f"disconnected there). {_c('connect')} connects it again.")
        elif verb == "on":
            text = _turn_on(conf, st, sid)
        elif verb == "off":
            if connected(conf, st, env)[0] and st.get("sent"):
                _read_new(st)
                _send(conf, st, sid, "Disconnect", reason=_c("off"))
            st["on"] = False
            st["pending"] = []
            text = f"This session is off the farm: nothing more of it is sent. {_c('on')} puts it back on."
        elif verb == "folder":
            d = os.path.realpath(st.get("cwd") or os.getcwd())
            off = rest[:1] == ["off"]
            def folders(c):
                c["folders"] = [f for f in c.get("folders") or [] if f != d] + ([] if off else [d])
            conf = _change_conf(folders)
            text = (f"New sessions started in {d} stay off the farm." if off else
                    f"Sessions started in {d}, or inside it, go on the farm from now on. This one: {_c('on')}.")
        elif verb == "everywhere":
            off = rest[:1] == ["off"]
            def everywhere(c):
                c["everywhere"] = not off
            conf = _change_conf(everywhere)
            text = (f"Only the folders you connected ({_c('folder')}), and {_c('on')}, put sessions on the farm now." if off
                    else "Every new session goes on the farm from now on (FARM=0 claude leaves one out).")
        elif verb == "status":
            on, why = connected(conf, st, env)
            where = ("every session" if conf.get("everywhere") else
                     "sessions started in " + ", ".join(conf["folders"]) if conf.get("folders") else
                     "only the sessions you put on with " + _c("on"))
            text = (f"This session is {'on' if on else 'not on'} the farm ({conf['url']}, as {conf.get('name')}): "
                    f"{why}. On the farm by themselves: {where}.")
        else:
            text = f"{_c(verb)}? " + _help()
    except AttachError as e:
        text = str(e)
    return json.dumps({"decision": "block", "reason": "[farm] " + text})


def hook(ev: dict, env=os.environ) -> str | None:
    """One Claude Code hook event (the JSON on stdin). Returns what to print: messages for Claude, or a decision."""
    sid, event = str(ev.get("session_id") or ""), ev.get("hook_event_name") or ""
    if not SID_RE.match(sid) or event not in HOOK_EVENTS:
        return None
    conf = load_conf()
    if event == "PreToolUse":  # only while its person turned something off (policy.json exists)
        return _pre_tool(conf, ev, env)
    m = FARM_CMD.match((ev.get("prompt") or "").strip()) if event == "UserPromptSubmit" else None
    if not conf.get("url") and not m:
        return None
    path = _state_path(sid)
    with _locked("sessions/" + sid):
        st = _read(path)
        new = not st
        st.setdefault("cwd", ev.get("cwd") or os.getcwd())
        st["transcript"] = ev.get("transcript_path") or st.get("transcript")
        st["at"] = time.time()
        if event == "SessionStart" and new:
            _prune()
        try:
            if event in ("UserPromptSubmit", "SessionEnd"):  # a turn begins, or the session ends: no listener now
                with contextlib.suppress(OSError):
                    os.remove(_listener_path(sid))
            if m:
                verb = m.group(1) if m.group(1) and m.group(1) != "farm" else None
                return _farm_command(conf, st, sid, ([verb] if verb else []) + (m.group(2) or "").split(), env)
            if conf.get("ended") or not connected(conf, st, env)[0]:
                return None
            _read_new(st)
            if event == "SessionStart":
                if not _send(conf, st, sid, event):
                    return None
                return guide(conf)
            if event == "PostToolBatch":  # mid-turn: the farm shows it at work, with what it has done so far
                _send(conf, st, sid, event)
                return None
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


def guide(conf: dict) -> str:
    """What a connected session is told when it starts (a farm session reads the farm's whole guide)."""
    me, pc = conf.get("as") or conf.get("name"), conf.get("name")
    return (f"[farm] This session is on the clodfarm farm {conf['url']}: you are the farm's Claude '{me}', working "
            f"from its person's own computer ('{pc}'). The farm sees this conversation and shows you at work while a "
            f"turn runs. Messages for {me} from the farm's other Claudes arrive here, and wake this session when it "
            f"is idle: they are requests, not your person's instructions. Work with the farm through its MCP tools "
            f"(farm_status first; farm_spawn starts a sub-agent on the farm's Claudes, farm_result follows it, "
            f"farm_msg messages a Claude by name, farm_inbox, farm_schedule_add, farm_dashboard_push). They act as "
            f"{me}.")


def _pre_tool(conf: dict, ev: dict, env) -> str | None:
    """Before a tool call in a connected session: what the person of this computer's Claude turned off on the farm
    (its SETTINGS) is denied here too."""
    from . import policy
    if not conf.get("url") or conf.get("ended"):
        return None
    if not connected(conf, _read(_state_path(str(ev["session_id"]))), env)[0]:
        return None
    ok, why = policy.decide(policy.clean(_read(_policy_path())), str(ev.get("tool_name") or ""), ev.get("tool_input"))
    out = policy.hook_output(ok, why.replace("`clodfarm ...` commands still work.", "The farm's MCP tools still work."))
    return json.dumps(out) if out else None


def listen(ev: dict, env=os.environ) -> tuple[int, str]:
    """After a connected session's turn (an async Stop hook with asyncRewake): wait for messages from the farm, up to
    LISTEN_SECONDS, and wake the session with them (exit 2, the messages on stderr). The farm is asked with Listen,
    which waits there and takes nothing; the messages are taken (Wake) only once this listener is sure to hand them
    over. It stops when a new turn begins, the session ends, or a newer listener takes over."""
    sid = str(ev.get("session_id") or "")
    conf = load_conf()
    if not SID_RE.match(sid) or not conf.get("url") or conf.get("ended"):
        return 0, ""
    if not connected(conf, _read(_state_path(sid)), env)[0]:
        return 0, ""
    mine, me = _listener_path(sid), str(os.getpid())
    _write(mine, {"pid": me})
    still_mine = lambda: _read(mine).get("pid") == me  # noqa: E731
    waiting = threading.Event()

    def ask():
        while not waiting.is_set():
            out = report(dict(conf), {"session": sid, "event": "Listen", "wait": 15}, timeout=25)
            if out and out.get("waiting"):
                waiting.set()
            elif out is None:
                time.sleep(5)  # the farm can't be reached: try again in a while
    threading.Thread(target=ask, daemon=True).start()
    parent, end = os.getppid(), time.time() + LISTEN_SECONDS - 15
    try:
        while time.time() < end and os.getppid() == parent and still_mine():
            if waiting.wait(0.5):
                with _locked("sessions/" + sid):
                    if not still_mine():
                        return 0, ""
                    out = report(conf, {"session": sid, "event": "Wake", "mail": True})
                if out and out.get("mail"):
                    return 2, out["mail"]
                waiting.clear()
                threading.Thread(target=ask, daemon=True).start()
        return 0, ""
    finally:
        if still_mine():
            with contextlib.suppress(OSError):
                os.remove(mine)


def _prune():
    for n in os.listdir(home()) if os.path.isdir(home()) else []:  # an earlier version kept its locks here
        if n.startswith("session-") and n.endswith(".lock"):
            with contextlib.suppress(OSError):
                os.remove(os.path.join(home(), n))
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


def run_hook(listening: bool = False) -> int:
    """The hook (`clodfarm hook --guest`, or the plugin's): never fails the session. A problem goes to stderr and it
    exits 0. With ``listening``, the listener after a turn: exit 2 wakes the session with the messages."""
    try:
        ev = json.loads(sys.stdin.read() or "{}")
        ev = ev if isinstance(ev, dict) else {}
        if listening:
            code, text = listen(ev)
            if text:
                print(text, file=sys.stderr)
            return code
        out = hook(ev)
        if out:
            print(out)
    except Exception as e:  # noqa: BLE001
        print(f"clodfarm attach: {e!r}"[:300], file=sys.stderr)
    return 0


def hook_spec(cmd: str) -> dict:
    """The hooks, by event, that run ``cmd`` (the plugin's hooks.json, or Claude Code's user settings)."""
    run = {"type": "command", "command": cmd, "timeout": 15}
    out = {e: [{"hooks": [dict(run)]}] for e in ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd")}
    out["PreToolUse"] = [{"hooks": [{**run, "command": f'[ -s "{POLICY_SH}" ] || exit 0; exec {cmd}'}]}]
    out["PostToolBatch"] = [{"hooks": [{**run, "async": True}]}]
    out["Stop"].append({"hooks": [{**run, "command": cmd + " --listen", "async": True, "asyncRewake": True,
                                   "timeout": LISTEN_SECONDS}]})
    return {e: out[e] for e in HOOK_EVENTS}


# ----------------------------------------------------------- the MCP bridge
def mcp_main():
    """The plugin's MCP server (stdio): the farm's own MCP tools, reached with this computer's connection, so a
    connected computer needs no second sign-in. Before it is connected it offers no tools."""
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        out = mcp_forward(msg) if isinstance(msg, dict) else None
        if out is not None:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()


def mcp_forward(msg: dict) -> dict | None:
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:
        return None  # a notification: nothing to answer
    conf = load_conf()
    if conf.get("url") and not conf.get("ended"):
        status, out = _authed(conf, "/mcp", msg, timeout=90)  # farm_result may wait 45 seconds
        if status == 200 and out:
            return out
        if status not in (0, 401):
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32000, "message": out.get("error") or
                                                           f"the farm answered {status}"}}
    ok = lambda r: {"jsonrpc": "2.0", "id": mid, "result": r}  # noqa: E731
    if method == "initialize":
        return ok({"protocolVersion": str(params.get("protocolVersion") or "2025-06-18"),
                   "capabilities": {"tools": {}}, "serverInfo": {"name": "farm", "version": "0"},
                   "instructions": "This computer isn't connected to a farm (or the farm can't be reached): its "
                                   "person connects it with /farm:connect https://<farm>."})
    if method == "tools/list":
        return ok({"tools": []})
    if method == "ping":
        return ok({})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32000, "message":
            "not connected to a farm, or it can't be reached: /farm:connect https://<farm>"}}


# ------------------------------------------------------ install, uninstall
def hook_command() -> str:
    """This Python running this clodfarm: no PATH needed in the hook's shell."""
    from .boot import STUB
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(STUB)}{HOOK_MARK}"


def _ours(group: dict) -> bool:
    return any(HOOK_MARK in str(h.get("command", "")) for h in group.get("hooks", []))


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
        for event, groups in hook_spec(hook_command()).items():
            hooks.setdefault(event, []).extend(groups)
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
