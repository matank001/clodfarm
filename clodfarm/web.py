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

from . import __version__
from .agents import AgentManager
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

    def set_password(self, password: str):
        if len(password) < 8:
            raise ValueError("use at least 8 characters")
        self.data = self._make(password)
        self._save(self.data)

    def check(self, password: str, ip: str) -> bool:
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
        agents = []
        for a, remote in claudes:
            ws = by_name.get(a["id"], [])
            seat = next((w.get("seat") for w in ws if w.get("seat")), None)
            agents.append(self._agent_view(a, {"loggedIn": True} if remote else self.manager.auth(a), ws, seat,
                                           seats, stats, rc.get(a["id"]) or rc.get(a.get("name"))))
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
            "farm": self.cfg.name, "version": __version__, "now": now(),
            "paused": bool(ctl.get("paused")), "pause_reason": ctl.get("reason") or "",
            "agents": agents, "subagents": subs, "recent": recent,
            "events": [{"at": e["at"], "type": e["type"], "msg": e["msg"][:240], "task": e.get("task"), "by": e.get("by")}
                       for e in events[-40:]],
        }

    def _agent_view(self, a, st, ws, seat, seats, stats, link) -> dict:
        r = seats.get(seat) if seat else None
        snap, d = (r or {}).get("snapshot"), (r or {}).get("decision")
        login = self.manager.login(a["id"]) if not a.get("remote") else None
        states = [w.get("state", "") for w in ws]
        running = sum(s == "running" for s in states)
        resting = bool(states) and all(s.startswith("throttled") for s in states)
        return {
            "id": a["id"], "name": a.get("name") or a["id"], "primary": bool(a.get("primary")),
            "remote": bool(a.get("remote")), "hat": a.get("hat", "straw"), "created": a.get("created", 0),
            "loggedIn": bool(st.get("loggedIn")), "email": st.get("email"), "plan": st.get("subscriptionType"),
            "alive": bool(a.get("remote") or self.manager.alive(a["id"])), "up": bool(ws), "seat": seat,
            "login": login.view() if login else None, "remote_control": link,
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

        def _headers(self, status: int, ctype: str, extra: dict | None = None, length: int | None = None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            if length is not None:
                self.send_header("Content-Length", str(length))
            self.send_header("Content-Security-Policy", CSP)
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

        def _static(self, rel: str):
            path = os.path.realpath(os.path.join(UI_DIR, rel))
            if not path.startswith(os.path.realpath(UI_DIR) + os.sep) or not os.path.isfile(path):
                return self._err(404, "not found")
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript",):
                ctype += "; charset=utf-8"
            data = open(path, "rb").read()
            cache = "public, max-age=86400, immutable" if rel.startswith("fonts/") else "no-cache"
            self._headers(200, ctype, {"Cache-Control": cache}, len(data))
            self.wfile.write(data)

        # ----------------------------------------------------------- GET
        def do_GET(self):
            if self.path.split("?", 1)[0] == "/healthz":  # the container health check, with or without a prefix
                return self._json({"ok": True, "version": __version__})
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
                if not path.startswith("/api/"):
                    return self._static(path.lstrip("/"))
                if path == "/api/me":
                    u = self._user()
                    return self._json({"user": u, "farm": ui.cfg.name, "version": __version__}, 200 if u else 401)
                if not self._user():
                    return self._err(401, "log in first")
                if path == "/api/state":
                    return self._json(ui.state())
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


def serve(cfg, store: Store | None = None, manager: AgentManager | None = None, block: bool = True):
    """Start the UI (and keep the added agents running). Returns the server when ``block`` is False."""
    ui = FarmUI(cfg, store, manager)
    host, port = os.environ.get("FARM_UI_HOST", "0.0.0.0"), int(os.environ.get("FARM_UI_PORT", "8080"))
    httpd = ThreadingHTTPServer((host, port), make_handler(ui))
    httpd.daemon_threads = True
    httpd.ui = ui
    threading.Thread(target=ui.manager.keep_alive, name="agents", daemon=True).start()
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
        ui.manager.shutdown()
    return httpd
