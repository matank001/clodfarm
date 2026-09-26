"""The farm UI: a pixel-art farm where you watch your Claude agents work, read the quest log and hatch new agents.

    clodfarm ui            serve it on its own (the farm daemon also serves it when FARM_UI=1, the default)
    clodfarm ui-passwd     set the UI password

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
import threading
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .agents import AgentManager
from .store import Store, now

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
def _first_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:160]
    return ""


def _public_task(t: dict, full: bool = False) -> dict:
    keep = ["id", "title", "status", "priority", "kind", "parent", "children", "depth", "created", "updated",
            "started", "finished", "attempts", "max_attempts", "worker", "branch", "created_by", "children_open"]
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

    def mission(self) -> str:
        p = next((p for p in self.cfg.mission_paths if os.path.isfile(p)), None)
        return open(p).read() if p else ""

    def state(self) -> dict:
        with self._lock:
            if self._state_cache and time.time() - self._state_cache[0] < 1.0:
                return self._state_cache[1]
            s = self._build_state()
            self._state_cache = (time.time(), s)
            return s

    def _build_state(self) -> dict:
        from .cli import _seats  # budget per seat, the same numbers `clodfarm status` shows
        store, host = self.store, None
        workers = store.workers()
        running = store.list_tasks("running", 50)
        titles = {t["id"]: t["title"] for t in running}
        seats = {r["seat"]: r for r in _seats(self.cfg, store)}
        agents, known = [], set()
        for a in self.manager.all():
            st = self.manager.auth(a)
            mine = [w for w in workers if w["SK"].split("/")[0].split("@")[0] == a["id"]]
            known |= {w["SK"] for w in mine}
            seat = next((w.get("seat") for w in mine if w.get("seat")), None)
            agents.append(self._agent_view(a, st, mine, seat, seats, titles))
        # boxes elsewhere in a multi-box farm: visitors you can watch but not manage here
        others: dict[str, list] = {}
        for w in workers:
            if w["SK"] not in known:
                others.setdefault(w["SK"].split("/")[0], []).append(w)
        for farm_id, ws in sorted(others.items()):
            seat = next((w.get("seat") for w in ws if w.get("seat")), None)
            a = {"id": farm_id, "name": farm_id.split("@")[0], "primary": False, "remote": True, "hat": "cap"}
            agents.append(self._agent_view(a, {"loggedIn": True}, ws, seat, seats, titles))
        ctl = store.control()
        mission = self.mission()
        return {
            "farm": self.cfg.name, "version": __version__, "now": now(),
            "paused": bool(ctl.get("paused")), "pause_reason": ctl.get("reason") or "",
            "mission": mission, "goal": _first_line(mission),
            "counts": {s: store.count(s) for s in ("queued", "running", "waiting", "done", "failed")},
            "agents": agents,
            "tasks": {"running": [_public_task(t) for t in running],
                      "waiting": [_public_task(t) for t in store.list_tasks("waiting", 30)],
                      "queued": [_public_task(t) for t in store.list_tasks("queued", 40)],
                      "done": [_public_task(t) for t in store.list_tasks("done", 30)],
                      "failed": [_public_task(t) for t in store.list_tasks("failed", 20)]},
            "events": [{"at": e["at"], "type": e["type"], "msg": e["msg"][:240], "task": e.get("task")}
                       for e in store.events(now() - 3 * 86400, 40)],
        }

    def _agent_view(self, a, st, mine, seat, seats, titles) -> dict:
        r = seats.get(seat) if seat else None
        snap, d = (r or {}).get("snapshot"), (r or {}).get("decision")
        login = self.manager.login(a["id"]) if not a.get("remote") else None
        return {
            "id": a["id"], "name": a.get("name") or a["id"], "primary": bool(a.get("primary")),
            "remote": bool(a.get("remote")), "hat": a.get("hat", "straw"), "created": a.get("created", 0),
            "loggedIn": bool(st.get("loggedIn")), "email": st.get("email"), "plan": st.get("subscriptionType"),
            "via": st.get("via"), "alive": a.get("remote") or self.manager.alive(a["id"]), "seat": seat,
            "login": login.view() if login else None,
            "workers": [{"id": w["SK"], "name": w["SK"].rsplit("/", 1)[-1], "state": w.get("state", ""),
                         "task": w.get("task"), "task_title": titles.get(w.get("task")), "at": w.get("at")}
                        for w in sorted(mine, key=lambda w: w["SK"])],
            "budget": None if not r else {
                "five_hour": snap.five_hour.utilization if snap and snap.five_hour else None,
                "five_hour_resets": snap.five_hour.resets_at if snap and snap.five_hour else None,
                "seven_day": snap.seven_day.utilization if snap and snap.seven_day else None,
                "seven_day_resets": snap.seven_day.resets_at if snap and snap.seven_day else None,
                "allowed": d.workers if d else None, "max": self.cfg.policy.max_workers,
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
            return self.client_address[0]

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
            return self.headers.get("X-Forwarded-Proto", "") == "https"

        def _cookie(self, value: str, max_age: int) -> str:
            return (f"{COOKIE}={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}"
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
            path = self.path.split("?", 1)[0]
            try:
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
                m = re.fullmatch(r"/api/tasks/([A-Za-z0-9]+)", path)
                if m:
                    t = ui.store.get_task(m.group(1))
                    if not t:
                        return self._err(404, "no such task")
                    runs = [{k: r.get(k) for k in ("started", "duration_s", "ok", "turns", "output_tokens",
                                                   "cost_usd_list_price", "terminal_reason", "worker")}
                            for r in ui.store.runs(t["id"])]
                    return self._json({**_public_task(t, full=True), "runs": runs})
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
            path = self.path.split("?", 1)[0]
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
            if path == "/api/tasks":
                title = str(data.get("title", "")).strip()
                if not title:
                    raise ValueError("a quest needs a title")
                prio = max(0, min(9, int(data.get("priority", 5))))
                t = store.add_task(title[:300], str(data.get("prompt") or title)[:20000], priority=prio,
                                   created_by="human", max_depth=ui.cfg.max_depth, max_attempts=ui.cfg.max_attempts)
                return self._json(_public_task(t))
            m = re.fullmatch(r"/api/tasks/([A-Za-z0-9]+)/(cancel|retry)", path)
            if m:
                ok = store.cancel(m.group(1)) if m.group(2) == "cancel" else store.retry(m.group(1))
                return self._json({"ok": ok}, 200 if ok else 409)
            if path == "/api/mission":
                from . import gitops
                text = str(data.get("text", "")).strip()
                if not text:
                    raise ValueError("the mission is empty")
                if not os.path.isdir(ui.cfg.repo_dir):
                    raise ValueError("the workspace repo doesn't exist yet: log in the first agent so the farm starts")
                gitops.write_mission(ui.cfg.repo_dir, text[:20000])
                store.event("mission.set", _first_line(text)[:200], by="ui")
                return self._json({"ok": True})
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
                store.event("agent.removed", f"{aid} released from the farm UI", by="ui")
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
