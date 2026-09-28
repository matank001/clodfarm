"""The farm UI: a pixel-art farm where you watch your Claude agents work and hatch (or release) new ones.

    clodfarm ui            serve it on its own (the farm daemon also serves it when FARM_UI=1, the default)
    clodfarm ui-passwd     set the UI password

Behind a reverse proxy: FARM_UI_BASE=/team serves it under a path prefix, FARM_UI_SECURE=1 marks the cookie Secure,
and FARM_UI_TRUST_PROXY=1 takes the client address from X-Forwarded-For (for the login lockout).

Standard library only. One password, no username (FARM_UI_PASSWORD, or `clodfarm ui-passwd`; with neither, a
password is generated on first start and printed once in the log). Passwords are stored as PBKDF2
hashes; sessions are HMAC-signed, HttpOnly, SameSite=Strict cookies; every write needs a JSON body and the
X-Clodfarm header, so another site can't drive the farm through your browser.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import socket
import threading
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import __version__, bots, browser, dashboards
from . import mcp
from .agents import AgentManager
from .slack import SlackBridge
from .store import Store, now

BASE = "/" + os.environ.get("FARM_UI_BASE", "").strip("/") if os.environ.get("FARM_UI_BASE", "").strip("/") else ""
UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
COOKIE = "clodfarm_session"
SESSION_DAYS = 7
PBKDF2_ROUNDS = 600_000
MAX_BODY = 256 * 1024
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; font-src 'self'; script-src 'self'; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


# ------------------------------------------------------------------ passwords
def _hash(password: str, salt: bytes, rounds: int = PBKDF2_ROUNDS) -> str:
    return base64.b64encode(hashlib.pbkdf2_hmac("sha256", password.encode(), salt, rounds)).decode()


class Auth:
    """The UI's password, and signed session cookies. There is one farmer, so no username."""

    def __init__(self, path: str):
        self.path = path
        self.fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        self.generated: str | None = None
        self.data = self._load()
        self._seen = self._mtime()

    def _mtime(self) -> int:
        try:
            return os.stat(self.path).st_mtime_ns
        except OSError:
            return 0

    def _fresh(self):
        """`clodfarm ui-passwd` rewrites the file from another process: use the new password (and its new secret,
        which signs every old session out) as soon as it's there."""
        m = self._mtime()
        if m and m != self._seen:
            self._seen = m
            self.data = self._load()

    def _load(self) -> dict:
        try:
            d = json.load(open(self.path))
        except (OSError, ValueError):
            d = {}
        env_pw = os.environ.get("FARM_UI_PASSWORD")
        if env_pw:  # the environment wins; keep the stored secret so sessions survive restarts
            salt = base64.b64decode(d["salt"]) if d.get("salt") and d.get("from_env") else secrets.token_bytes(16)
            h = _hash(env_pw, salt)
            if not (d.get("from_env") and d.get("hash") == h):
                d = {"user": "farmer", "salt": base64.b64encode(salt).decode(), "hash": h,
                     "secret": d.get("secret") or secrets.token_hex(32), "from_env": True}
                self._save(d)
        elif not d.get("hash"):
            self.generated = secrets.token_urlsafe(12)
            d = self._make(self.generated)
            self._save(d)
        return d

    @staticmethod
    def _make(password: str) -> dict:
        salt = secrets.token_bytes(16)
        return {"user": "farmer", "salt": base64.b64encode(salt).decode(), "hash": _hash(password, salt),
                "secret": secrets.token_hex(32)}  # a new secret signs out every old session

    def _save(self, d: dict):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(d, f)
        os.replace(tmp, self.path)
        self._seen = self._mtime()

    def set_password(self, password: str):
        if len(password) < 8:
            raise ValueError("use at least 8 characters")
        self.data = self._make(password)
        self._save(self.data)

    def check(self, password: str, ip: str) -> bool:
        self._fresh()
        with self._lock:
            recent = [t for t in self.fails.get(ip, []) if t > time.time() - 300]
            self.fails[ip] = recent
            if len(recent) >= 5:
                return False  # 5 wrong tries in 5 minutes: wait
        salt = base64.b64decode(self.data["salt"])
        ok = hmac.compare_digest(_hash(password, salt).encode(), self.data["hash"].encode())
        if not ok:
            with self._lock:
                self.fails.setdefault(ip, []).append(time.time())
        return ok

    def locked_out(self, ip: str) -> bool:
        return len([t for t in self.fails.get(ip, []) if t > time.time() - 300]) >= 5

    def _sign(self, payload: str) -> str:
        return hmac.new(bytes.fromhex(self.data["secret"]), payload.encode(), hashlib.sha256).hexdigest()

    def issue(self, user: str) -> str:
        payload = f"{user}|{int(time.time() + SESSION_DAYS * 86400)}|{secrets.token_hex(8)}"
        return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=") + "." + self._sign(payload)

    def verify(self, token: str | None) -> str | None:
        if not token or "." not in token:
            return None
        self._fresh()
        b, sig = token.rsplit(".", 1)
        try:
            payload = base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)).decode()
            user, exp, _ = payload.split("|")
        except (ValueError, UnicodeDecodeError):
            return None
        if not hmac.compare_digest(sig, self._sign(payload)) or int(exp) < time.time() or user != self.data["user"]:
            return None
        return user


# ---------------------------------------------------------------------- state
def _from(seen: dict | None) -> str:
    """Where a proxy check came out, for the event log: " (203.0.113.7, US)"."""
    return f" ({seen['ip']}{', ' + seen['country'].upper() if seen.get('country') else ''})" if seen else ""


def _public_task(t: dict, full: bool = False) -> dict:
    keep = ["id", "title", "status", "priority", "kind", "parent", "children", "depth", "created", "updated",
            "started", "finished", "attempts", "max_attempts", "worker", "branch", "created_by", "children_open", "owner",
            "to"]
    out = {k: t.get(k) for k in keep if t.get(k) is not None}
    if full:
        out.update(prompt=t.get("prompt", ""), result=t.get("result", ""))
    else:
        out["summary"] = (t.get("result") or "")[-240:] if t.get("status") in ("done", "failed") else ""
    return out


class FarmUI:
    def __init__(self, cfg, store: Store | None = None, manager: AgentManager | None = None):
        self.cfg = cfg
        self.store = store or Store.from_config(cfg)
        self.manager = manager or AgentManager(cfg)
        self.auth = Auth(os.path.join(cfg.workspace, ".farm", "ui-auth.json"))
        self.slack = SlackBridge(cfg, self.store)
        self.oauth = mcp.OAuthStore(mcp.oauth_path(cfg.workspace))
        self.browsers = browser.Browsers(cfg.workspace)  # the farm's Chromium profiles: BROWSER on the farm, /browser
        self.stopping = threading.Event()
        self._state_cache: tuple[float, dict] | None = None
        self._lock = threading.Lock()

    def state(self) -> dict:
        with self._lock:
            if self._state_cache and time.time() - self._state_cache[0] < 1.0:
                return self._state_cache[1]
            s = self._build_state()
            self._state_cache = (time.time(), s)
            return s

    def _build_state(self) -> dict:
        from .cli import _seats  # budget per seat, the same numbers `clodfarm agents` shows
        store = self.store
        workers = store.workers()
        seats = {r["seat"]: r for r in _seats(self.cfg, store)}
        events = store.events(now() - 7 * 86400, 5000)
        stats: dict[str, dict] = {}  # per Claude, the last 7 days: sub-agents it ran, finished, failed
        took = {e.get("task"): e["msg"].split(" by ", 1)[1].split("@")[0] for e in events
                if e["type"] == "task.claimed" and " by " in e["msg"]}
        ended = {e.get("task"): e["type"][5:] for e in events if e["type"] in ("task.done", "task.failed")}
        for tid, name in took.items():
            st = stats.setdefault(name, {"ran": 0, "done": 0, "failed": 0})
            st["ran"] += 1
            if tid in ended:
                st[ended[tid]] += 1
        rc = {}  # each Claude's newest Remote Control link: talk to it from the Claude app
        for e in events:
            m = re.match(r"Remote Control '([^']+)' is live: (https://\S+)", e["msg"]) if e["type"] == "rc.connected" else None
            if m:
                rc[m.group(1)] = m.group(2)
        by_name: dict[str, list] = {}
        for w in workers:
            by_name.setdefault(w["SK"].split("/")[0].split("@")[0], []).append(w)
        claudes = [(a, False) for a in self.manager.all()]
        # Claudes on other boxes of a multi-box farm: you can watch them but not manage them here. A box on this host
        # that isn't registered is a Claude that was released: its last heartbeats are not a visitor.
        here, ids = "@" + socket.gethostname(), {a["id"] for a in self.manager.all()}
        for box in sorted({w["SK"].split("/")[0] for w in workers}):
            name = box.split("@")[0]
            if name not in ids and not box.endswith(here) and all(c[0]["id"] != name for c in claudes):
                claudes.append(({"id": name, "name": name, "remote": True, "hat": "cap"}, True))
        talking = self._talking(store)
        agents = []
        for a, remote in claudes:
            ws = by_name.get(a["id"], [])
            seat = next((w.get("seat") for w in ws if w.get("seat")), None)
            agents.append(self._agent_view(a, {"loggedIn": True} if remote else self.manager.auth(a), ws, seat,
                                           seats, stats, rc.get(a["id"]) or rc.get(a.get("name"))))
            agents[-1]["talking"] = talking.get(a["id"])
        primary = self.cfg.name
        subs = []
        for s in ("running", "waiting", "queued"):
            for t in store.list_tasks(s, 60):
                v = _public_task(t)
                v["owner"] = t.get("owner") or (t.get("worker", "").split("@")[0] or primary)
                v["on"] = t.get("worker", "").split("@")[0] if s == "running" else t.get("to")
                subs.append(v)
        recent = [_public_task(t) | {"owner": t.get("owner") or primary}
                  for t in store.list_tasks("done", 12) + store.list_tasks("failed", 4)
                  if not t.get("parent") and now() - float(t.get("finished") or t.get("updated") or 0) < 6 * 3600]
        ctl = store.control()
        return {
            "farm": self.cfg.farm, "version": __version__, "now": now(),
            "paused": bool(ctl.get("paused")), "pause_reason": ctl.get("reason") or "",
            "slack": {"state": self.slack.state, "team": (self.slack.info or {}).get("team")},
            "agents": agents, "subagents": subs, "recent": recent,
            "events": [{"at": e["at"], "type": e["type"], "msg": e["msg"][:240], "task": e.get("task"), "by": e.get("by")}
                       for e in events[-40:]],
        }

    FINISHED_FOR = 86400  # the TASKS page lists sub-agents that finished in the last day

    def tasks_view(self) -> dict:
        """Everything the TASKS page shows: every sub-agent at work or waiting, the ones that finished in the last
        day, every schedule, and whether the farm is paused."""
        from .schedule import describe
        store, t0 = self.store, now()

        def view(t):
            v = _public_task(t)
            v["on"] = t.get("worker", "").split("@")[0] if t.get("status") == "running" else t.get("to")
            v["owner"] = t.get("owner") or self.cfg.name
            return v
        active = [view(t) for s in ("running", "waiting", "queued") for t in store.list_tasks(s, 200)]
        finished = sorted((view(t) for s in ("done", "failed", "cancelled") for t in store.list_tasks(s, 60)
                           if t0 - float(t.get("finished") or t.get("updated") or 0) < self.FINISHED_FOR),
                          key=lambda v: -float(v.get("finished") or v.get("updated") or 0))[:60]
        schedules = [{**{k: v for k, v in r.items() if k not in ("PK", "SK", "ver")}, "when": describe(r)}
                     for r in store.schedules()]
        claudes = sorted({a["id"] for a in self.manager.all()} | {w["SK"].split("/")[0].split("@")[0]
                                                                  for w in store.workers()})
        ctl = store.control()
        return {"now": t0, "farm": self.cfg.farm, "me": self.cfg.name, "paused": bool(ctl.get("paused")),
                "pause_reason": ctl.get("reason") or "", "active": active, "finished": finished,
                "schedules": schedules, "claudes": claudes, "tz": os.environ.get("FARM_TZ") or "UTC"}

    TURN_QUIET = 600  # a turn whose transcript is silent this long was interrupted (no Stop comes then)

    def _talking(self, store) -> dict[str, dict]:
        """Per Claude, its conversations in the middle of a turn: how many, since when, and the newest one's title."""
        out: dict[str, dict] = {}
        for s in store.sessions(None, 200):
            if s.get("kind") != "conversation" or not s.get("busy") or s.get("ended"):
                continue
            seen = float(s.get("last_at") or 0)
            try:  # the transcript grows while the turn runs (same box); a Claude on another box: its last hook
                seen = max(seen, os.path.getmtime(s["transcript"]))
            except (KeyError, TypeError, OSError):
                pass
            if now() - seen > self.TURN_QUIET:
                continue
            t = out.setdefault(s.get("claude") or self.cfg.name, {"n": 0, "since": now(), "title": s.get("title") or ""})
            t["n"] += 1
            t["since"] = min(t["since"], float(s.get("last_at") or now()))
        return out

    def _agent_view(self, a, st, ws, seat, seats, stats, link) -> dict:
        r = seats.get(seat) if seat else None
        snap, d = (r or {}).get("snapshot"), (r or {}).get("decision")
        login = self.manager.login(a["id"]) if not a.get("remote") else None
        states = [w.get("state", "") for w in ws]
        running = sum(s == "running" for s in states)
        beat = next((w for w in ws if w.get("bot")), None)  # a bot on another box: its heartbeats say so
        bot = {"model": a["bot"]["model"], "via": bots.label(a["bot"]), "takes": a["bot"].get("takes") or "sent"} \
            if a.get("bot") else {"model": beat["bot"], "via": beat.get("bot_via") or "",
                                  "takes": beat.get("bot_takes") or "sent"} if beat else None
        resting = bool(states) and all(s.startswith("throttled") for s in states)
        return {
            "id": a["id"], "name": a.get("name") or a["id"], "primary": bool(a.get("primary")),
            "remote": bool(a.get("remote")), "hat": a.get("hat", "straw"), "created": a.get("created", 0),
            "loggedIn": bool(st.get("loggedIn")), "email": st.get("email"), "plan": st.get("subscriptionType"),
            "alive": bool(a.get("remote") or self.manager.alive(a["id"])), "up": bool(ws), "seat": seat,
            "login": login.view() if login else None, "remote_control": link, "bot": bot,
            "running": running, "resting": resting,
            "error": next((s for s in states if s.startswith("error")), None),
            "stats": stats.get(a["id"], {"ran": 0, "done": 0, "failed": 0}),
            "budget": None if not r else {
                "five_hour": snap.five_hour.utilization if snap and snap.five_hour else None,
                "five_hour_resets": snap.five_hour.resets_at if snap and snap.five_hour else None,
                "seven_day": snap.seven_day.utilization if snap and snap.seven_day else None,
                "seven_day_resets": snap.seven_day.resets_at if snap and snap.seven_day else None,
                "can_start": max(0, (d.workers if d else 0) - running), "max": self.cfg.policy.max_workers,
                "reason": d.reason if d else "", "measured": snap.observed_at if snap else None},
        }


# -------------------------------------------------------------------- handler
def make_handler(ui: FarmUI):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"clodfarm/{__version__}"
        sys_version = ""

        def log_message(self, fmt, *args):  # quiet: the farm log is for the farm
            pass

        # -------------------------------------------------------- helpers
        def _ip(self) -> str:
            if os.environ.get("FARM_UI_TRUST_PROXY") == "1":
                # the proxy appends the address it saw, so the last entry is the one to trust
                xff = [x.strip() for x in (self.headers.get("X-Forwarded-For") or "").split(",") if x.strip()]
                if xff:
                    return xff[-1]
            return self.client_address[0]

        def _path(self) -> str | None:
            """The request path without the FARM_UI_BASE prefix; None when it is outside the prefix."""
            path = self.path.split("?", 1)[0]
            if not BASE:
                return path
            if path == BASE:
                return None  # redirected to BASE/ by the caller, so relative URLs resolve
            return path[len(BASE):] if path.startswith(BASE + "/") else ""

        def _headers(self, status: int, ctype: str, extra: dict | None = None, length: int | None = None,
                     csp: str = CSP):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            if length is not None:
                self.send_header("Content-Length", str(length))
            self.send_header("Content-Security-Policy", csp)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()

        def _json(self, obj, status: int = 200, extra: dict | None = None):
            body = json.dumps(obj, default=str).encode()
            self._headers(status, "application/json; charset=utf-8",
                          {"Cache-Control": "no-store", **(extra or {})}, len(body))
            self.wfile.write(body)

        def _err(self, status: int, msg: str):
            self._json({"error": msg}, status)

        def _user(self) -> str | None:
            c = SimpleCookie(self.headers.get("Cookie") or "")
            return ui.auth.verify(c[COOKIE].value if COOKIE in c else None)

        def _secure(self) -> bool:
            return os.environ.get("FARM_UI_SECURE") == "1" or self.headers.get("X-Forwarded-Proto", "") == "https"

        def _cookie(self, value: str, max_age: int) -> str:
            return (f"{COOKIE}={value}; Path={BASE or ''}/; HttpOnly; SameSite=Strict; Max-Age={max_age}"
                    + ("; Secure" if self._secure() else ""))

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ValueError("request too large")
            raw = self.rfile.read(n) if n else b"{}"
            data = json.loads(raw or b"{}")
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
            return data

        def _static(self, rel: str, root: str = UI_DIR):
            path = os.path.realpath(os.path.join(root, rel))
            if not path.startswith(os.path.realpath(root) + os.sep) or not os.path.isfile(path):
                return self._err(404, "not found")
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript",):
                ctype += "; charset=utf-8"
            data = open(path, "rb").read()
            cache = "public, max-age=86400, immutable" if rel.startswith("fonts/") else "no-cache"
            self._headers(200, ctype, {"Cache-Control": cache}, len(data))
            self.wfile.write(data)


        # ------------------------------------------------------ MCP + OAuth
        def _pub(self) -> str:
            return mcp.public_url(self, BASE)

        def _form(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ValueError("request too large")
            raw = self.rfile.read(n).decode() if n else ""
            if (self.headers.get("Content-Type") or "").startswith("application/json"):
                d = json.loads(raw or "{}")
                return d if isinstance(d, dict) else {}
            return {k: v[-1] for k, v in parse_qs(raw, keep_blank_values=True).items()}

        def _html(self, status: int, page: str, csp: str):
            body = page.encode()
            self._headers(status, "text/html; charset=utf-8", {"Cache-Control": "no-store"}, len(body), csp=csp)
            self.wfile.write(body)

        def _page_csp(self, nonce: str, redirect: str = "") -> str:
            u = urlsplit(redirect) if redirect else None
            to = f" {u.scheme}://{u.netloc}" if u and u.scheme and u.netloc else ""
            return (f"default-src 'none'; style-src 'nonce-{nonce}'; font-src 'self'; img-src 'self' data:; "
                    f"form-action 'self'{to}; frame-ancestors 'none'; base-uri 'none'")

        def _oauth_get(self, raw: str) -> bool:
            """Discovery documents (also at the root, path-inserted as RFC 8414 and 9728 say) and the consent page."""
            prm, asm = mcp.metadata(self._pub())
            rel = raw[len(BASE):] if BASE and raw.startswith(BASE + "/") else raw if not BASE else None
            if raw in (f"/.well-known/oauth-protected-resource{BASE}/mcp", f"/.well-known/oauth-protected-resource{BASE}") \
                    or rel in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"):
                self._json(prm)
                return True
            if raw == f"/.well-known/oauth-authorization-server{BASE}" or rel == "/.well-known/oauth-authorization-server":
                self._json(asm)
                return True
            if rel == "/mcp":
                self._headers(405, "application/json", {"Allow": "POST"}, 2)
                self.wfile.write(b"{}")
                return True
            if rel == "/oauth/authorize":
                q = {k: v[-1] for k, v in parse_qs(urlsplit(self.path).query).items()}
                self._consent(q)
                return True
            return False

        def _consent(self, q: dict, error: str = ""):
            nonce = secrets.token_urlsafe(12)
            p, err, can_redirect = mcp.check_authorize(ui.oauth, q, self._pub())
            if err and not can_redirect:
                return self._html(400, mcp.error_page(err, nonce, BASE), self._page_csp(nonce))
            if err:
                return self._redirect(mcp.redirect_with(p["redirect_uri"], error=err, state=p["state"], iss=self._pub()))
            c = ui.oauth.client(p["client_id"])
            page = mcp.consent_page(p, c, bool(self._user()), ui.oauth.form_token(p),
                                    q.get("name") or mcp.slug(f"{ui.cfg.name}-laptop"), nonce, BASE, error)
            self._html(200 if not error else 400, page, self._page_csp(nonce, p["redirect_uri"]))

        def _redirect(self, url: str):
            self.send_response(302)
            self.send_header("Location", url)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _farm_names(self) -> set[str]:
            from .cli import _claudes
            return {c["name"] for c in _claudes(ui.cfg, ui.store)} | {a["id"] for a in ui.manager.all()} | \
                {a.get("name") or "" for a in ui.manager.all()} | {ui.cfg.name}

        def _oauth_post(self, path: str):
            oa, pub = ui.oauth, self._pub()
            nostore = {"Cache-Control": "no-store", "Pragma": "no-cache"}
            try:
                if path == "/oauth/register":
                    try:
                        return self._json(oa.register(self._form()), 201, nostore)
                    except ValueError as e:
                        return self._json({"error": "invalid_client_metadata", "error_description": str(e)}, 400)
                if path == "/oauth/token":
                    status, out = mcp.token_response(oa, self._form(), pub)
                    return self._json(out, status, nostore)
                if path == "/oauth/revoke":
                    oa.revoke_token(self._form().get("token", ""))
                    return self._json({}, 200, nostore)
                if path == "/oauth/authorize":
                    return self._authorize_post(self._form())
                return self._mcp()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except ValueError as e:
                return self._json({"error": "invalid_request", "error_description": str(e)[:200]}, 400)

        def _authorize_post(self, f: dict):
            p, err, can_redirect = mcp.check_authorize(ui.oauth, f, self._pub())
            nonce = secrets.token_urlsafe(12)
            if err and not can_redirect:
                return self._html(400, mcp.error_page(err, nonce, BASE), self._page_csp(nonce))
            if err or not ui.oauth.form_ok(f.get("form", ""), p):
                return self._html(400, mcp.error_page(err or "this page expired: start the connection again from "
                                                      "your MCP client", nonce, BASE), self._page_csp(nonce))
            if f.get("decision") != "allow":
                return self._redirect(mcp.redirect_with(p["redirect_uri"], error="access_denied", state=p["state"],
                                                        iss=self._pub()))
            if not self._user():
                if ui.auth.locked_out(self._ip()):
                    return self._consent({**p, "name": f.get("name", "")}, "too many tries: wait five minutes")
                if not ui.auth.check(str(f.get("password", ""))[:1000], self._ip()):
                    return self._consent({**p, "name": f.get("name", "")}, "wrong password")
            name = str(f.get("name", "")).strip().lower()
            if not mcp.NAME_RE.match(name):
                return self._consent({**p, "name": name}, "a name is lowercase letters, digits, . _ or -")
            if name in self._farm_names():
                return self._consent({**p, "name": name}, f"'{name}' is a Claude on the farm: pick another name")
            scope = "farm:read" if f.get("access") == "read" else p["scope"]
            code = ui.oauth.new_code(client_id=p["client_id"], redirect_uri=p["redirect_uri"],
                                     challenge=p["code_challenge"], scope=scope, name=name)
            ui.store.event("mcp.connected", f"{name} connected over MCP ({ui.oauth.client(p['client_id'])['client_name']},"
                           f" {scope})", by="ui")
            return self._redirect(mcp.redirect_with(p["redirect_uri"], code=code, state=p["state"], iss=self._pub()))

        def _mcp(self):
            pub = self._pub()
            origin = self.headers.get("Origin")
            po = urlsplit(pub)
            if origin and origin.rstrip("/") != f"{po.scheme}://{po.netloc}":
                return self._json({"error": "origin not allowed"}, 403)  # DNS rebinding / cross-site browsers
            auth = self.headers.get("Authorization") or ""
            token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
            grant = ui.oauth.check(token) if token else None
            if not grant or grant.get("resource") != pub + "/mcp":
                www = f'Bearer resource_metadata="{pub}/.well-known/oauth-protected-resource", scope="{" ".join(mcp.SCOPES)}"'
                if token:
                    www += ', error="invalid_token"'
                return self._json({"error": "sign in: connect this MCP client to the farm"}, 401,
                                  {"WWW-Authenticate": www})
            try:
                msg = self._body()
            except ValueError:
                return self._json({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}, 400)
            if not msg.get("method"):
                self._headers(202, "application/json", None, 0)  # a response or notification from the client
                return
            ui._state_cache = None
            out = mcp.rpc(ui, grant, msg)
            if out is None:
                self._headers(202, "application/json", None, 0)
                return
            self._json(out)

        def do_DELETE(self):
            self._headers(405, "application/json", {"Allow": "POST"}, 2)
            self.wfile.write(b"{}")

        def _page(self, rel: str, csp: str = CSP):
            """An HTML page at a nested path: its asset links are written against the UI's base, not relative."""
            data = open(os.path.join(UI_DIR, rel), "rb").read().replace(b"{{BASE}}", BASE.encode())
            self._headers(200, "text/html; charset=utf-8", {"Cache-Control": "no-cache"}, len(data), csp=csp)
            self.wfile.write(data)

        # -------------------------------------------------------- the browser
        def _same_origin(self) -> bool:
            """A WebSocket has no CORS: only a page of this farm may open one (the cookie alone is not enough)."""
            origin = urlsplit(self.headers.get("Origin") or "")
            hosts = {self.headers.get("Host"), self.headers.get("X-Forwarded-Host"), urlsplit(self._pub()).netloc}
            return origin.scheme in ("http", "https") and bool(origin.netloc) and origin.netloc in hosts - {None, ""}

        def _browser_screen(self):
            """The farm's browser screen: a WebSocket bridged to its VNC server, for noVNC on /browser."""
            if (self.headers.get("Upgrade") or "").lower() != "websocket" or not self.headers.get("Sec-WebSocket-Key"):
                return self._err(400, "a WebSocket upgrade is expected here")
            if not self._same_origin():
                return self._err(403, "origin not allowed")
            if not self._user():
                return self._err(401, "log in first")
            q = {k: v[-1] for k, v in parse_qs(urlsplit(self.path).query).items()}
            try:
                slot = ui.browsers.slot(q.get("profile") or browser.DEFAULT)
            except ValueError as e:
                return self._err(404, str(e))
            try:
                vnc = socket.create_connection(("127.0.0.1", browser.vnc_port(slot)), timeout=3)
            except OSError:
                return self._err(503, "the browser is not running: start it first")
            self.send_response(101, "Switching Protocols")
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", browser.ws_accept(self.headers["Sec-WebSocket-Key"].strip()))
            protos = [p.strip() for p in (self.headers.get("Sec-WebSocket-Protocol") or "").split(",") if p.strip()]
            if "binary" in protos:
                self.send_header("Sec-WebSocket-Protocol", "binary")
            self.end_headers()
            self.close_connection = True
            browser.bridge(self.rfile, self.wfile, self.connection, vnc)

        def _browser_csp(self) -> str:
            host = self.headers.get("Host") or ""
            ws = f" ws://{host} wss://{host}" if re.fullmatch(r"[A-Za-z0-9.:\[\]-]{1,200}", host) else ""
            return CSP.replace("connect-src 'self'", "connect-src 'self'" + ws)

        # ----------------------------------------------------------- GET
        def do_GET(self):
            if self.path.split("?", 1)[0] == "/healthz":  # the container health check, with or without a prefix
                return self._json({"ok": True, "version": __version__})
            if self._oauth_get(self.path.split("?", 1)[0]):
                return
            path = self._path()
            if path is None:
                self.send_response(308)
                self.send_header("Location", BASE + "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            try:
                if path == "":
                    return self._err(404, "not found")
                if path in ("/", "/index.html"):
                    return self._static("index.html")
                if path == "/healthz":
                    return self._json({"ok": True, "version": __version__})
                if path in ("/dashboards", "/dashboards/") or re.fullmatch(r"/dashboards/[a-z0-9-]{1,48}", path):
                    return self._page("dash.html")  # the list and each dashboard: one page, routed by dash.js
                if path in ("/browser", "/browser/"):
                    return self._page("browser.html", self._browser_csp())
                if path in ("/tasks", "/tasks/"):
                    return self._page("tasks.html")
                if path.startswith("/browser/novnc/"):  # noVNC, the VNC client the browser page draws with
                    return self._static(path[len("/browser/novnc/"):], browser.novnc_dir())
                if path == "/api/browser/screen":
                    return self._browser_screen()
                if not path.startswith("/api/"):
                    return self._static(path.lstrip("/"))
                if path == "/api/me":
                    u = self._user()
                    return self._json({"user": u, "farm": ui.cfg.farm, "version": __version__}, 200 if u else 401)
                if not self._user():
                    return self._err(401, "log in first")
                if path == "/api/state":
                    return self._json(ui.state())
                if path == "/api/slack":
                    return self._json(ui.slack.view())
                if path == "/api/browser":
                    return self._json(ui.browsers.status())
                if path == "/api/tasks":
                    return self._json(ui.tasks_view())
                m = re.fullmatch(r"/api/tasks/([a-z0-9]{6,40})", path)
                if m:  # one sub-agent: its instructions, its result so far and its runs
                    t = ui.store.get_task(m.group(1))
                    if not t:
                        return self._err(404, "no such sub-agent")
                    runs = [{k: r.get(k) for k in ("worker", "started", "duration_s", "ok", "turns", "terminal_reason")}
                            for r in ui.store.runs(t["id"])]
                    return self._json({**_public_task(t, full=True), "runs": runs[-10:],
                                       "on": t.get("worker", "").split("@")[0] if t.get("status") == "running"
                                       else t.get("to")})
                m = re.fullmatch(r"/api/agents/([a-z0-9@._-]+)/tools", path)
                if m:  # what that Claude can use, as its last run saw it
                    t = ui.store.tools().get(m.group(1))
                    return self._json({k: v for k, v in t.items() if k not in ("PK", "SK", "ver")} if t else {"tools": None})
                if path == "/api/dashboards":
                    return self._json([dashboards.summary(ui.store, d) for d in dashboards.all_(ui.store)])
                m = re.fullmatch(r"/api/dashboards/([a-z0-9-]{1,48})", path)
                if m:
                    d = dashboards.get(ui.store, m.group(1))
                    if not d:
                        return self._err(404, "no such dashboard")
                    q = {k: v[-1] for k, v in parse_qs(urlsplit(self.path).query).items()}
                    days = max(1, min(365, int(q.get("days") or 30))) if str(q.get("days") or "30").isdigit() else 30
                    return self._json(dashboards.view(ui.store, d, days))
                if path == "/api/sessions":  # every Claude session on the farm, newest first
                    q = {k: v[-1] for k, v in parse_qs(urlsplit(self.path).query).items()}
                    keep = ("id", "claude", "runs_on", "kind", "task", "title", "turns", "started", "last_at", "ended")
                    return self._json([{k: s.get(k) for k in keep} for s in ui.store.sessions(q.get("claude"), 50)])
                m = re.fullmatch(r"/api/sessions/([A-Za-z0-9-]+)", path)
                if m:  # one session's whole conversation
                    s = ui.store.session(m.group(1))
                    if not s:
                        return self._err(404, "no such session")
                    return self._json({"id": s["id"], "claude": s.get("claude"), "kind": s.get("kind"),
                                       "title": s.get("title"), "task": s.get("task"), "started": s.get("started"),
                                       "conversation": [{k: t.get(k) for k in ("role", "kind", "text", "at")}
                                                        for t in ui.store.turns(s["id"])]})
                m = re.fullmatch(r"/api/agents/([a-z0-9@._-]+)/login", path)
                if m:
                    s = ui.manager.login(m.group(1))
                    return self._json(s.view() if s else {"state": "none"})
                return self._err(404, "not found")
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:  # noqa: BLE001 - one bad request must never take the UI down
                return self._err(500, f"{type(e).__name__}: {str(e)[:200]}")

        # ---------------------------------------------------------- POST
        def do_POST(self):
            path = self._path() or ""
            try:
                if path in ("/mcp", "/oauth/register", "/oauth/token", "/oauth/revoke", "/oauth/authorize"):
                    return self._oauth_post(path)  # bearer tokens and OAuth forms: no cookie, no X-Clodfarm
                if self.headers.get("X-Clodfarm") != "1" or \
                        not (self.headers.get("Content-Type") or "").startswith("application/json"):
                    return self._err(403, "missing X-Clodfarm header or JSON body")
                data = self._body()
                if path == "/api/login":
                    if ui.auth.locked_out(self._ip()):
                        return self._err(429, "too many tries: wait five minutes")
                    pw, user = str(data.get("password", ""))[:1000], ui.auth.data["user"]
                    if not ui.auth.check(pw, self._ip()):
                        return self._err(401, "wrong password")
                    return self._json({"user": user}, extra={"Set-Cookie": self._cookie(ui.auth.issue(user),
                                                                                         SESSION_DAYS * 86400)})
                if path == "/api/logout":
                    return self._json({"ok": True}, extra={"Set-Cookie": self._cookie("", 0)})
                if not self._user():
                    return self._err(401, "log in first")
                return self._post(path, data)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except (ValueError, KeyError) as e:
                return self._err(400, str(e)[:300])
            except Exception as e:  # noqa: BLE001
                return self._err(500, f"{type(e).__name__}: {str(e)[:200]}")

        def _post(self, path: str, data: dict):
            store, mgr = ui.store, ui.manager
            ui._state_cache = None
            if path == "/api/pause":
                store.set_paused(True, str(data.get("reason") or "paused from the farm UI")[:200], by="ui")
                return self._json({"ok": True})
            if path == "/api/resume":
                store.set_paused(False, "resumed from the farm UI", by="ui")
                return self._json({"ok": True})
            if path == "/api/slack":  # connect: check both tokens with Slack, keep them, open the connection
                allow = [x.strip() for x in re.split(r"[,\s]+", str(data.get("allow") or "")) if x.strip()][:200]
                origin = self.headers.get("Origin") or ""
                ui_url = origin + BASE + "/" if re.fullmatch(r"https?://[A-Za-z0-9.:-]+", origin) else ""
                return self._json(ui.slack.connect(str(data.get("bot_token", ""))[:300],
                                                   str(data.get("app_token", ""))[:300], allow, ui_url))
            if path in ("/api/browser/start", "/api/browser/stop"):
                on, name = path.endswith("start"), str(data.get("profile") or browser.DEFAULT)
                if on and not browser.available():
                    raise ValueError("this image has no browser: " + ", ".join(browser.missing() or ["FARM_BROWSER=0"]))
                if ui.browsers.want(name, on, by="ui"):
                    store.event("browser.started" if on else "browser.stopped",
                                f"browser profile {name} {'started' if on else 'stopped'} from the farm UI", by="ui")
                threading.Thread(target=ui.browsers.sync, name="browser-sync", daemon=True).start()
                return self._json(ui.browsers.status())
            if path == "/api/browser/proxy":  # a profile through the proxy (checked first, from a country) or direct
                name, on = str(data.get("profile") or browser.DEFAULT), bool(data.get("on"))
                country = str(data["country"])[:8] if data.get("country") is not None else None
                changed, seen = ui.browsers.proxy(name, on, by="ui", country=country)
                if changed:
                    store.event("browser.proxy", f"browser profile {name} " + (f"through the proxy{_from(seen)}" if on
                                else "direct") + " from the farm UI", by="ui")
                threading.Thread(target=ui.browsers.sync, name="browser-sync", daemon=True).start()
                return self._json({**ui.browsers.status(), "seen": seen})
            if path == "/api/browser/proxy/address":  # set the farm's proxy (checked first), or "" to forget it
                if os.environ.get(browser.PROXY_ENV):
                    raise ValueError(f"{browser.PROXY_ENV} is set in the farm's environment: change it there")
                text = str(data.get("address") or "")[:2000].strip()
                if not text:
                    browser.save_proxy(ui.cfg.workspace, None)
                    store.event("browser.proxy", "the browser's proxy removed from the farm UI", by="ui")
                    threading.Thread(target=ui.browsers.sync, name="browser-sync", daemon=True).start()
                    return self._json(ui.browsers.status())
                p, name = browser.parse_proxy(text), str(data.get("profile") or "")
                seen = ui.browsers.set_proxy(p, name, by="ui")  # set from a profile: that profile goes through it
                store.event("browser.proxy", f"the browser's proxy set to {p['host']}:{p['port']} from the farm UI"
                            + (f"; profile {name} through it{_from(seen)}" if name else ""), by="ui")
                threading.Thread(target=ui.browsers.sync, name="browser-sync", daemon=True).start()
                return self._json({**ui.browsers.status(), "seen": seen})
            if path == "/api/browser/open":
                url = browser.normalize_url(str(data.get("url", ""))[:2000])
                slot = ui.browsers.slot(str(data.get("profile") or browser.DEFAULT))
                try:
                    return self._json(browser.open_url(url, slot))
                except OSError:
                    return self._err(503, "that profile's browser is not running: start it first")
            if path in ("/api/browser/add", "/api/browser/remove"):
                name = str(data.get("profile") or "")
                if path.endswith("add"):
                    ui.browsers.registry.add(name, by="ui")
                    store.event("browser.added", f"browser profile {name} added from the farm UI", by="ui")
                elif ui.browsers.registry.remove(name):
                    store.event("browser.removed", f"browser profile {name} and its logins removed from the farm UI",
                                by="ui")
                ui.manager.share_browser_tools()
                threading.Thread(target=ui.browsers.sync, name="browser-sync", daemon=True).start()
                return self._json(ui.browsers.status())
            m = re.fullmatch(r"/api/tasks/([a-z0-9]{6,40})/(cancel|retry)", path)
            if m:
                tid, action = m.groups()
                if not store.get_task(tid):
                    return self._err(404, "no such sub-agent")
                ok = store.cancel(tid) if action == "cancel" else store.retry(tid)
                if not ok:
                    raise ValueError("it already finished" if action == "cancel" else "it is still at work")
                return self._json(ui.tasks_view())
            if path == "/api/schedules":  # a new schedule from the TASKS page
                from .schedule import parse_at, parse_every
                title, prompt = str(data.get("title") or "").strip()[:200], str(data.get("prompt") or "").strip()
                whens = [k for k in ("cron", "every", "at") if str(data.get(k) or "").strip()]
                if not title:
                    raise ValueError("give it a title")
                if len(whens) != 1:
                    raise ValueError("give exactly one of cron, every or at")
                tz = str(data.get("tz") or os.environ.get("FARM_TZ") or "UTC").strip()[:64]
                on = str(data.get("on") or "").strip()[:64] or None
                w = str(data[whens[0]]).strip()
                try:
                    spec = {"cron": w[:100]} if whens == ["cron"] else {"every": parse_every(w[:20])} \
                        if whens == ["every"] else {"at": parse_at(w[:40], tz)}
                    store.add_schedule(title, prompt[:100_000] or title, tz=tz, to=on, created_by="ui",
                                       owner=on or ui.cfg.name, **spec)
                except (ValueError, KeyError) as e:  # an unknown time zone is a KeyError
                    raise ValueError(f"bad schedule: {str(e).strip(chr(39))}") from None
                return self._json(ui.tasks_view())
            m = re.fullmatch(r"/api/schedules/([a-z0-9]{6,40})/(pause|resume|run|remove)", path)
            if m:
                sid, action = m.groups()
                if not store.b.get("SCHEDULE", sid):
                    return self._err(404, "no such schedule")
                if action == "remove":
                    store.remove_schedule(sid)
                elif action == "run":
                    store.run_schedule(sid, ui.cfg.max_depth, ui.cfg.max_attempts, by="ui")
                else:
                    store.pause_schedule(sid, action == "pause", by="ui")
                return self._json(ui.tasks_view())
            if path == "/api/slack/allow":
                ui.slack.set_allow([x.strip() for x in re.split(r"[,\s]+", str(data.get("allow") or "")) if x.strip()][:200])
                return self._json(ui.slack.view())
            if path == "/api/slack/disconnect":
                ui.slack.disconnect()
                return self._json(ui.slack.view())
            if path == "/api/agents" and isinstance(data.get("bot"), dict):  # a bot: kept once its provider answers
                bot = bots.parse(data["bot"])
                key = bots.check_key(bot["provider"], str(data["bot"].get("key") or ""))
                said = bots.check(bot, key)
                a = mgr.create(str(data.get("name", "")), bot=bot, key=key)
                store.event("agent.added", f"{a['id']} added from the farm UI: a bot on {bot['model']} via "
                            f"{bots.label(bot)}", by="ui")
                return self._json({"id": a["id"], "name": a["name"], "said": said})
            if path == "/api/agents":
                a = mgr.create(str(data.get("name", "")))
                store.event("agent.added", f"{a['id']} hatched from the farm UI; waiting for its login", by="ui")
                mgr.start_login(a["id"])
                return self._json({"id": a["id"], "name": a["name"]})
            m = re.fullmatch(r"/api/agents/([a-z0-9._-]+)/(login|code|remove|cancel-login)", path)
            if m:
                aid, action = m.groups()
                if not mgr.get(aid):
                    return self._err(404, "no such agent")
                if action == "login":
                    return self._json(mgr.start_login(aid).view())
                if action == "code":
                    s = mgr.login(aid)
                    if not s:
                        raise ValueError("no login in progress")
                    s.submit(str(data.get("code", "")))
                    return self._json(s.view())
                if action == "cancel-login":
                    s = mgr.logins.pop(aid, None)
                    if s:
                        s.kill()
                    return self._json({"ok": True})
                mgr.remove(aid)
                back = store.forget_box(mgr.farm_id(aid))  # its workers leave with it; its tasks go back
                store.event("agent.removed", f"{aid} released from the farm UI"
                            + (f"; {back} task(s) back in the queue" if back else ""), by="ui")
                return self._json({"ok": True})
            return self._err(404, "not found")

    return Handler


class FarmHTTPServer(ThreadingHTTPServer):
    """The farm UI's server. A page opens many connections at once (the browser page's noVNC is ~55 modules, and a
    proxy in front opens one upstream connection per request): with socketserver's backlog of 5, Linux drops the
    rest, the proxy answers 502 and the page's script never runs."""
    request_queue_size = 256
    daemon_threads = True


def serve(cfg, store: Store | None = None, manager: AgentManager | None = None, block: bool = True):
    """Start the UI (and keep the added agents running). Returns the server when ``block`` is False."""
    ui = FarmUI(cfg, store, manager)
    host, port = os.environ.get("FARM_UI_HOST", "0.0.0.0"), int(os.environ.get("FARM_UI_PORT", "8080"))
    httpd = FarmHTTPServer((host, port), make_handler(ui))
    httpd.ui = ui
    threading.Thread(target=ui.manager.keep_alive, name="agents", daemon=True).start()
    threading.Thread(target=ui.browsers.keep, args=(ui.stopping,), name="browser", daemon=True).start()
    ui.slack.start()  # talk to the farm from Slack, once it's connected (the SLACK button)
    print(f"farm UI on http://{'localhost' if host in ('0.0.0.0', '::') else host}:{port}", flush=True)
    if ui.auth.generated:
        print("+----------------------------------------------------------------+\n"
              f"|  farm UI password: {ui.auth.generated:<44}|\n"
              "|  shown once; change it with `clodfarm ui-passwd`               |\n"
              "+----------------------------------------------------------------+", flush=True)
    if not block:
        threading.Thread(target=httpd.serve_forever, name="ui", daemon=True).start()
        return httpd
    try:
        httpd.serve_forever()
    finally:
        ui.slack.stop()
        ui.stopping.set()
        ui.browsers.shutdown()
        ui.manager.shutdown()
    return httpd
