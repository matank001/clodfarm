"""The farm UI: a pixel-art farm where you watch your Claude agents work and hatch (or release) new ones.

    clodfarm ui            serve it on its own (the farm daemon also serves it when FARM_UI=1, the default)

Who sees what:
  * the public (a public farm, the default) watch: the farm, every
    Claude, the tasks' titles, schedules, tokens burned and the planner's goal; never prompts, results, conversations,
    tools, logins or the browser. They can hatch a Claude of their own (once per browser).
  * an owner (the `clodfarm_owner` cookie, set when they hatched their Claude, or by the pairing link their Claude
    gives them in the Claude app) sees and manages their own Claude: its conversations, results, tools, skin and
    settings, the missions waiting for their approval, and their browser profiles.
  * the farm manager, the person of a manager Claude (the farm's first, until a manager hands it on), sees and manages
    everything: who runs the farm, the planner, a private farm, hatching, every Claude.

Behind a reverse proxy: FARM_UI_BASE=/team serves it under a path prefix, FARM_UI_SECURE=1 marks the cookie Secure,
and FARM_UI_TRUST_PROXY=1 takes the client address from X-Forwarded-For (for the login lockout).

An invite (MANAGE -> INVITE A CLAUDE, or `clodfarm invite`) is a link that lets one person hatch a Claude of their own
here, once, whether the farm is private or its hatching is closed: it's used up at their login, not when it's opened
(a chat app's preview doesn't spend it).

Hosted for someone else: FARM_UI_PRIVATE=1 keeps the farm private whatever its settings say, FARM_UI_SSO_KEY lets the
host sign its customer in with a one-time /sso link (sso.py), and FARM_MAX_CLAUDES caps the Claudes the farm hatches
(agents.py), the manager's included.

Standard library only. No passwords: people sign in with their Claude (a pairing link or code), and cookies are
HMAC-signed and HttpOnly; every write needs a JSON body and the X-Clodfarm header, so another site can't drive the
farm through your browser.
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

from . import __version__, boot, bots, browser, connectors, dashboards, policy, sso
from . import mcp
from .agents import AgentManager, room_note
from .slack import SlackBridge
from .store import Store, now

BASE = "/" + os.environ.get("FARM_UI_BASE", "").strip("/") if os.environ.get("FARM_UI_BASE", "").strip("/") else ""
UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
COOKIE = "clodfarm_session"  # the farm manager
OWNER_COOKIE = "clodfarm_owner"  # the person a Claude belongs to
VIEWER_COOKIE = "clodfarm_viewer"  # an old viewer-password session (logout clears it; nothing reads it now)
INVITE_COOKIE = "clodfarm_invite"  # an invite this device holds (until its login spends it)
SESSION_DAYS = 7
OWNER_DAYS = 365
PBKDF2_ROUNDS = 600_000
MAX_BODY = 256 * 1024
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; font-src 'self'; script-src 'self'; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


# ------------------------------------------------------------------ passwords
def _hash(password: str, salt: bytes, rounds: int = PBKDF2_ROUNDS) -> str:
    return base64.b64encode(hashlib.pbkdf2_hmac("sha256", password.encode(), salt, rounds)).decode()


class Lockout:
    """Five wrong passwords (or pairing codes) from one address in five minutes: that address waits."""

    def __init__(self):
        self.fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def locked_out(self, ip: str) -> bool:
        with self._lock:
            return len([t for t in self.fails.get(ip, []) if t > time.time() - 300]) >= 5

    def fail(self, ip: str):
        with self._lock:
            self.fails[ip] = [t for t in self.fails.get(ip, []) if t > time.time() - 300] + [time.time()]

    def clear(self, ip: str):
        with self._lock:
            self.fails.pop(ip, None)


class Keys:
    """Signs owner and viewer cookies. Its own secret, kept in the workspace volume (a new one signs everyone out).
    out, not every person who owns a Claude."""

    def __init__(self, path: str):
        self.path = path
        try:
            self.key = bytes.fromhex(json.load(open(path))["key"])
        except (OSError, ValueError, KeyError):
            self.key = secrets.token_bytes(32)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump({"key": self.key.hex()}, f)
            os.replace(path + ".tmp", path)

    def make(self, kind: str, fields: list[str], days: int) -> str:
        payload = "|".join([kind, *fields, str(int(time.time() + days * 86400)), secrets.token_hex(6)])
        sig = hmac.new(self.key, payload.encode(), hashlib.sha256).hexdigest()
        return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=") + "." + sig

    def read(self, token: str | None, kind: str) -> list[str] | None:
        if not token or "." not in token:
            return None
        b, sig = token.rsplit(".", 1)
        try:
            payload = base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)).decode()
        except (ValueError, UnicodeDecodeError):
            return None
        if not hmac.compare_digest(sig, hmac.new(self.key, payload.encode(), hashlib.sha256).hexdigest()):
            return None
        parts = payload.split("|")
        if len(parts) < 3 or parts[0] != kind or not parts[-2].isdigit() or int(parts[-2]) < time.time():
            return None
        return parts[1:-2]


def _pw_hash(password: str, salt_b64: str | None = None) -> tuple[str, str]:
    salt = base64.b64decode(salt_b64) if salt_b64 else secrets.token_bytes(16)
    return base64.b64encode(salt).decode(), _hash(password, salt)


class Who:
    """Who is asking: the manager, the owner of one Claude, someone who may watch, or someone who may not."""

    def __init__(self, manager: bool = False, owner: str | None = None, viewer: bool = False, public: bool = False):
        self.manager, self.owner, self.viewer, self.public = manager, owner, viewer, public

    @property
    def can_view(self) -> bool:
        return self.manager or bool(self.owner) or self.viewer or self.public

    def owns(self, claude: str | None) -> bool:
        return self.manager or (bool(self.owner) and claude == self.owner)

    @property
    def key(self) -> str:
        """What the state it gets depends on (the cache key)."""
        return "manager" if self.manager else f"owner:{self.owner}" if self.owner else "public"

    def view(self) -> dict:
        return {"manager": self.manager, "owner": self.owner, "viewer": self.viewer, "can_view": self.can_view,
                "role": "manager" if self.manager else "owner" if self.owner else "viewer" if self.can_view else None}


HATS = ("straw", "beanie", "cap", "flower", "headphones", "bow", "crown", "sprout", "leaf", "wizard", "chef", "none")
ACCESSORIES = ("", "scarf", "glasses", "bowtie", "backpack", "cape")
COLOR = re.compile(r"#[0-9a-fA-F]{6}")


def _skin(d) -> dict:
    """A Claude's look, from the hatch or SETTINGS form: its hat, the hat's colours, its body tint, an accessory."""
    if not isinstance(d, dict):
        return {}
    out: dict = {}
    if d.get("hat") in HATS:
        out["hat"] = d["hat"]
    cols = d.get("colors") if isinstance(d.get("colors"), dict) else {}
    cols = {k: str(v) for k, v in cols.items() if k in ("hat", "band", "body") and COLOR.fullmatch(str(v))}
    if cols:
        out["colors"] = cols
    if d.get("accessory") in ACCESSORIES:
        out["accessory"] = d["accessory"]
    return out


def _redact_event(e: dict) -> dict:
    """Events as the public sees them: what happened and who, not what was said."""
    out = {"at": e["at"], "type": e["type"], "task": e.get("task"), "by": e.get("by")}
    if e["type"].startswith(("msg.", "rc.", "mcp.", "slack.", "browser.", "approval.")) or e["type"] in (
            "task.failed", "task.retry", "verify.failed"):
        who = re.match(r"^(\S+ -> \S+):", e["msg"])
        out["msg"] = (who.group(1) + ": (a message)") if who else e["type"].replace(".", " ")
    else:
        out["msg"] = e["msg"][:240]
    return out


# ---------------------------------------------------------------------- state
def _from(seen: dict | None) -> str:
    """Where a proxy check came out, for the event log: " (203.0.113.7, US)"."""
    return f" ({seen['ip']}{', ' + seen['country'].upper() if seen.get('country') else ''})" if seen else ""


def _public_task(t: dict, full: bool = False, summary: bool = True) -> dict:
    keep = ["id", "title", "status", "priority", "kind", "parent", "children", "depth", "created", "updated",
            "started", "finished", "attempts", "max_attempts", "worker", "branch", "created_by", "children_open", "owner",
            "to"]
    out = {k: t.get(k) for k in keep if t.get(k) is not None}
    if t.get("approval"):
        out["approval"] = {k: t["approval"].get(k) for k in ("asked_at", "from", "expires_at", "decided_by", "ok")
                           if t["approval"].get(k) is not None}
    if full:
        out.update(prompt=t.get("prompt", ""), result=t.get("result", ""))
    elif summary:
        out["summary"] = (t.get("result") or "")[-240:] if t.get("status") in ("done", "failed") else ""
    return out


class FarmUI:
    def __init__(self, cfg, store: Store | None = None, manager: AgentManager | None = None):
        self.cfg = cfg
        self.store = store or Store.from_config(cfg)
        self.manager = manager or AgentManager(cfg)
        self.lock = Lockout()
        self.keys = Keys(os.path.join(cfg.workspace, ".farm", "ui-keys.json"))
        self.hatches: dict[str, list[float]] = {}  # per client address: when it hatched (the per-hour limit)
        self.slack = SlackBridge(cfg, self.store)
        self.oauth = mcp.OAuthStore(mcp.oauth_path(cfg.workspace))
        self.browsers = browser.Browsers(cfg.workspace)  # the farm's Chromium profiles: BROWSER on the farm, /browser
        self.stopping = threading.Event()
        self._state_cache: tuple[float, dict] | None = None
        self._views: dict[str, tuple[float, bytes, str]] = {}  # per viewer kind: (at, JSON, ETag)
        self._tasks_cache: dict[str, tuple[float, bytes, str]] = {}
        self.inflight = 0  # requests being answered (a roll waits for them)
        self._inflight_lock = threading.Lock()
        self._tasks_lock = threading.Lock()
        self._lock = threading.Lock()

    def state(self) -> dict:
        with self._lock:
            if self._state_cache and time.time() - self._state_cache[0] < 1.0:
                return self._state_cache[1]
            s = self._build_state()
            self._state_cache = (time.time(), s)
            return s

    def state_for(self, who: "Who") -> tuple[bytes, str]:
        """The farm as ``who`` may see it, as JSON and its ETag: built once a second per kind of viewer, however
        many people watch."""
        key = who.key
        hit = self._views.get(key)
        if hit and time.time() - hit[0] < 1.0 and self._state_cache and hit[0] >= self._state_cache[0]:
            return hit[1], hit[2]
        st = self.state()
        with self._tasks_lock:  # many watchers asking at once: one of them builds it
            hit, built = self._views.get(key), self._state_cache
            if hit and built and time.time() - hit[0] < 1.0 and hit[0] >= built[0]:
                return hit[1], hit[2]
            body = json.dumps(self.redact(st, who), default=str).encode()
            tag = '"' + hashlib.sha256(body).hexdigest()[:20] + '"'
            self._views[key] = (time.time(), body, tag)
            return body, tag

    TASKS_FOR = 2.0

    def tasks_for(self, who: "Who", q: dict) -> tuple[bytes, str]:
        """The TASKS page's data, built once per two seconds per kind of viewer and query, however many watch."""
        q = {k: str(q.get(k) or "")[:100] for k in ("status", "claude", "q", "page", "mine") if q.get(k)}
        key = who.key + "|" + json.dumps(q, sort_keys=True)
        with self._tasks_lock:
            hit = self._tasks_cache.get(key)
            if hit and time.time() - hit[0] < self.TASKS_FOR:
                return hit[1], hit[2]
            body = json.dumps(self.tasks_view(who, q), default=str).encode()
            tag = '"' + hashlib.sha256(body).hexdigest()[:20] + '"'
            if len(self._tasks_cache) > 500:
                self._tasks_cache.clear()
            self._tasks_cache[key] = (time.time(), body, tag)
            return body, tag

    def redact(self, st: dict, who: "Who") -> dict:
        out = dict(st)
        agents = []
        for a in st["agents"]:
            if who.owns(a["id"]):
                agents.append({**a, "mine": a["id"] == who.owner})
                continue
            agents.append({**{k: v for k, v in a.items() if k not in ("email", "login", "remote_control", "seat")},
                           "talking": {k: v for k, v in (a.get("talking") or {}).items() if k != "title"} or None,
                           "login": {"state": a["login"]["state"]} if a.get("login") else None, "mine": False})
        out["agents"] = agents
        if not who.manager:
            def task(t):
                return t if who.owns(t.get("owner")) or who.owns(t.get("to")) else \
                    {k: v for k, v in t.items() if k != "summary"}
            out["subagents"] = [task(t) for t in st["subagents"]]
            out["recent"] = [task(t) for t in st["recent"]]
            out["events"] = [e if who.owner and who.owner in (e.get("by") or "") else _redact_event(e)
                             for e in st["events"]]
            out.pop("slack", None)
        tok = st.get("tokens") or {}
        out["tokens"] = {"total": tok.get("total"), "today": tok.get("today"),
                         "by_claude": {k: v.get("total", 0) for k, v in (tok.get("by_claude") or {}).items()}}
        me = None
        if who.owner:
            mine = next((a for a in st["agents"] if a["id"] == who.owner), None)
            me = {"claude": who.owner, "name": (mine or {}).get("name") or who.owner,
                  "tokens": (tok.get("by_claude") or {}).get(who.owner) or {},
                  "tokens_today": (st.get("tokens_today_by") or {}).get(who.owner) or {},
                  "budget": (mine or {}).get("budget"),
                  "pending": len([p for p in st.get("pending", []) if p.get("to") == who.owner])}
        out["me"] = {**who.view(), **(me or {}),
                     "pending_all": len(st.get("pending", [])) if who.manager else None}
        out.pop("pending", None)
        out.pop("tokens_today_by", None)
        return out

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
            st = {"loggedIn": True} if remote else self.manager.auth(
                a, max_age=300 if ws else 20, wait=False, guess={"loggedIn": True} if ws else None)
            agents.append(self._agent_view(a, st, ws, seat,
                                           seats, stats, rc.get(a["id"]) or rc.get(a.get("name"))))
            agents[-1]["talking"] = talking.get(a["id"])
        recs = {c["id"]: c for c in store.claudes()}
        for v in agents:  # its skin and its person's choices (store CLAUDE/<id>)
            rec = recs.get(v["id"]) or {}
            v.update(hat=rec.get("hat") or v["hat"], colors=rec.get("colors"), accessory=rec.get("accessory"),
                     name=rec.get("name") or v["name"], approve_missions=bool(rec.get("approve_missions")),
                     tools_off=policy.clean(rec.get("tools"))["deny"], owned=bool(rec.get("owned")),
                     planner_host_ok=bool(rec.get("planner_host_ok")) or v["primary"])
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
        settings, pl = store.settings(), store.planner()
        return {
            "farm": self.cfg.farm, "version": __version__, "release": boot.running(), "now": now(),
            "paused": bool(ctl.get("paused")), "pause_reason": ctl.get("reason") or "",
            "tokens": store.tokens(), "tokens_today_by": store.tokens_today_by(),
            "pending": [{"id": p.get("id"), "to": p.get("to")} for p in store.pending()],
            "private": self.private(settings), "hatch_open": bool(settings.get("hatch_open")),
            "planner": {k: pl.get(k) for k in ("on", "goal", "host", "state", "last_at", "next_at", "cycles",
                                                "every_s", "idle_until", "task")},
            "slack": {"state": self.slack.state, "team": (self.slack.info or {}).get("team")},
            "connectors": {"stripe": bool(connectors.stripe_load(self.cfg.workspace)),
                           "blender": bool(connectors.blender_load(self.cfg.workspace)),
                           "google_ads": bool(connectors.gads_load(self.cfg.workspace))},
            "agents": agents, "subagents": subs, "recent": recent,
            "events": [{"at": e["at"], "type": e["type"], "msg": e["msg"][:240], "task": e.get("task"), "by": e.get("by")}
                       for e in events[-40:]],
        }

    FINISHED_FOR = 86400  # the TASKS page lists sub-agents that finished in the last day
    PAGE = 100

    def health(self) -> dict:
        """The container's health check: the UI answers, and the farm daemon's workers beat."""
        try:
            beats = [float(w.get("at", 0)) for w in self.store.workers(300)]
        except Exception:  # noqa: BLE001
            beats = []
        return {"ok": True, "version": __version__, "release": boot.running(),
                "farm_beat_age": round(now() - max(beats), 1) if beats else None}

    def tasks_view(self, who: "Who | None" = None, q: dict | None = None) -> dict:
        """Everything the TASKS page shows: every sub-agent at work, waiting for approval or waiting, the ones that
        finished in the last day, every schedule, and whether the farm is paused. Filtered, searched and paged on
        the server (``q``: status, claude, q, page), so a farm of a hundred Claudes stays quick to read."""
        from .schedule import describe
        who = who or Who(manager=True)
        q = q or {}
        store, t0 = self.store, now()

        def view(t):
            mine = who.owns(t.get("owner")) or who.owns(t.get("to"))
            v = _public_task(t, summary=mine)
            v["on"] = t.get("worker", "").split("@")[0] if t.get("status") == "running" else t.get("to")
            v["owner"] = t.get("owner") or self.cfg.name
            v["mine"] = bool(who.owner) and who.owner in (t.get("owner"), t.get("to"))
            return v
        active = [view(t) for s in ("running", "pending", "waiting", "queued") for t in store.list_tasks(s, 500)]
        finished = sorted((view(t) for s in ("done", "failed", "cancelled", "denied") for t in store.list_tasks(s, 300)
                           if t0 - float(t.get("finished") or t.get("updated") or 0) < self.FINISHED_FOR),
                          key=lambda v: -float(v.get("finished") or v.get("updated") or 0))
        claude, text, status = q.get("claude") or "", (q.get("q") or "").strip().lower(), q.get("status") or ""
        if q.get("mine") and who.owner:
            claude = who.owner

        def match(v):  # the Claude and the search: the tabs count what they leave
            if claude and claude not in (v.get("owner"), v.get("on"), v.get("to")):
                return False
            return not text or text in (v.get("title") or "").lower() or text in v["id"]
        active, finished = [v for v in active if match(v)], [v for v in finished if match(v)]
        counts: dict[str, int] = {}
        for v in active + finished:
            counts[v["status"]] = counts.get(v["status"], 0) + 1
        if status:
            active, finished = [v for v in active if v["status"] == status], [v for v in finished if v["status"] == status]
        page = max(0, int(q.get("page") or 0)) if str(q.get("page") or "0").isdigit() else 0
        schedules = [{**{k: v for k, v in r.items() if k not in ("PK", "SK", "ver")
                         and (who.manager or who.owns(r.get("owner")) or k not in ("prompt",))},
                      "when": describe(r), "mine": who.owns(r.get("owner")) or who.owns(r.get("to"))}
                     for r in store.schedules()]
        claudes = sorted({a["id"] for a in self.manager.all()} | {w["SK"].split("/")[0].split("@")[0]
                                                                  for w in store.workers()})
        ctl = store.control()
        return {"now": t0, "farm": self.cfg.farm, "me": who.owner or (self.cfg.name if who.manager else None),
                "role": who.view()["role"], "paused": bool(ctl.get("paused")),
                "pause_reason": ctl.get("reason") or "", "counts": counts,
                "active": active[page * self.PAGE:(page + 1) * self.PAGE], "active_total": len(active),
                "finished": finished[page * self.PAGE:(page + 1) * self.PAGE], "finished_total": len(finished),
                "page": page, "page_size": self.PAGE,
                "schedules": schedules, "claudes": claudes, "tz": os.environ.get("FARM_TZ") or "UTC"}

    def browser_view(self, who: "Who") -> dict:
        """The farm's browser as this person sees it: only their own Claude's profiles."""
        st = self.browsers.status()
        # the browser is a Claude's tool: everyone (the manager too) sees only their own Claude's profiles; the
        # manager gives a profile to another Claude from a shell (`clodfarm browser assign`)
        for p in st["profiles"]:
            p["mine"] = self.profile_mine(p, who)
        st["profiles"] = [p for p in st["profiles"] if p["mine"]]
        if not who.manager:  # the proxy's address and login stay the manager's
            st["proxy"] = {k: v for k, v in (st.get("proxy") or {}).items() if k in ("set", "host", "countries")} \
                if st.get("proxy") else st.get("proxy")
        st["can_set_proxy"] = who.manager
        st["per_claude"] = browser.PER_CLAUDE
        st["me"] = who.owner
        return st

    def first_profile(self, who: "Who") -> str:
        """The profile a request means when it names none: the viewer's own Claude's first (the manager: any)."""
        profs = self.browsers.registry.all()
        mine = [p["name"] for p in profs if self.profile_mine(p, who)]
        return (mine or ([p["name"] for p in profs] if who.manager else []) or [""])[0]

    def private(self, st: dict | None = None) -> bool:
        """Only signed-in people watch: the manager made it private, or its host did (FARM_UI_PRIVATE=1)."""
        st = st if st is not None else self.store.settings()
        return bool(st.get("private")) or os.environ.get("FARM_UI_PRIVATE") == "1"

    def managers(self, st: dict | None = None) -> list[str]:
        """The Claudes whose persons run the farm: the farm's own (first) Claude until a manager changes it."""
        st = st if st is not None else self.store.settings()
        return [m for m in (st.get("managers") or []) if m] or [self.cfg.name]

    def connectors_view(self, who: "Who") -> dict:
        """The CONNECTORS menu: Slack, Stripe, Blender and Google Ads, as each viewer may see them (the manager manages
        them)."""
        stripe = connectors.stripe_view(self.cfg.workspace)
        if not who.manager:  # a person sees that it's there and what it's for, not who connected it or the key's end
            stripe = {k: v for k, v in stripe.items() if k in ("connected", "mode", "account", "tools")}
        blender = connectors.blender_view(self.cfg.workspace)
        if not who.manager:  # a person sees that it's there and what it is, not where it runs or the token's end
            blender = {k: v for k, v in blender.items() if k in ("connected", "server", "tools")}
        gads = connectors.gads_view(self.cfg.workspace)
        gads.pop("yaml", None)
        if not who.manager:
            gads = {k: v for k, v in gads.items() if k in ("connected", "customers", "more", "api_version")}
        return {"manage": who.manager, "slack": {"state": self.slack.state, "team": (self.slack.info or {}).get("team")},
                "stripe": stripe, "blender": blender, "google_ads": gads}

    def profile_mine(self, p: dict, who: "Who") -> bool:
        """Is this browser profile the viewer's Claude's? One nobody owns is the farm's own Claude's."""
        return bool(who.owner) and (p.get("owner") or self.cfg.name) == who.owner

    def settings_view(self, cid: str) -> dict:
        rec = self.store.claude(cid)
        a = self.manager.get(cid) or {}
        return {"id": cid, "name": rec.get("name") or a.get("name") or cid, "hat": rec.get("hat") or a.get("hat"),
                "colors": rec.get("colors"), "accessory": rec.get("accessory"),
                "approve_missions": bool(rec.get("approve_missions")), "tools": policy.clean(rec.get("tools")),
                "groups": policy.groups_view(), "notify_topic": rec.get("notify_topic") or "",
                "planner_host_ok": bool(rec.get("planner_host_ok")), "primary": bool(a.get("primary"))}

    def save_settings(self, cid: str, data: dict, by: str):
        """A person changes their Claude: its skin, name, approvals, tools. The tools apply at its next tool call."""
        a = self.manager.get(cid)
        ch: dict = {}
        if "name" in data:
            name = str(data["name"] or "").strip()[:24]
            if not name:
                raise ValueError("give it a name")
            ch["name"] = name
        if "skin" in data:
            sk = _skin(data["skin"])
            ch.update({k: v for k, v in sk.items()})
        for k in ("approve_missions", "planner_host_ok"):
            if k in data:
                ch[k] = bool(data[k])
        if "notify_topic" in data:
            topic = str(data["notify_topic"] or "").strip()[:200]
            if topic and not re.fullmatch(r"(https://[A-Za-z0-9.-]+/)?[A-Za-z0-9_-]{4,64}", topic):
                raise ValueError("an ntfy topic (letters, digits, - and _) or https://your-ntfy-server/topic")
            ch["notify_topic"] = topic
        if "tools" in data:
            ch["tools"] = policy.clean(data["tools"])
            policy.save(a["config_dir"], ch["tools"], claude=cid)
        self.store.put_claude(cid, **ch)
        self.store.event("agent.settings", f"{cid}: " + ", ".join(sorted(ch)) + f" changed ({by})", by="ui")
        self._views.clear()
        self._state_cache = None

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

        def _uncount(self):
            u = getattr(self.server, "uncount", None)
            if u:
                u(self.request, early=True)

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

        def _who(self) -> Who:
            if getattr(self, "_who_cache", None) is not None:
                return self._who_cache
            c = SimpleCookie(self.headers.get("Cookie") or "")
            owner = None
            got = ui.keys.read(c[OWNER_COOKIE].value if OWNER_COOKIE in c else None, "owner")
            if got and len(got) == 2:
                rec = ui.store.claude(got[0])
                if str(rec.get("owner_ver", 1)) == got[1] and ui.manager.get(got[0]):
                    owner = got[0]
            st = ui.store.settings()
            manager = bool(owner) and owner in ui.managers(st)  # the person of a manager Claude runs the farm
            viewer = False
            private = ui.private(st)  # only the people of its Claudes (and its manager) see it: no passwords
            self._who_cache = Who(manager, owner, viewer, public=not private)
            return self._who_cache

        def _owner_cookie(self, cid: str) -> str:
            ver = str(ui.store.claude(cid).get("owner_ver", 1))
            return self._named_cookie(OWNER_COOKIE, ui.keys.make("owner", [cid, ver], OWNER_DAYS), OWNER_DAYS * 86400)

        def _named_cookie(self, name: str, value: str, max_age: int) -> str:
            # Lax, not Strict: the pairing link opens from the Claude app (another site) and must sign in there
            same = "Lax" if name == OWNER_COOKIE else "Strict"
            return (f"{name}={value}; Path={BASE or ''}/; HttpOnly; SameSite={same}; Max-Age={max_age}"
                    + ("; Secure" if self._secure() else ""))

        def _public_base(self) -> str:
            """Where people reach this farm: FARM_PUBLIC_URL, or the address this request came to."""
            base = (os.environ.get("FARM_PUBLIC_URL") or os.environ.get("FARM_UI_PUBLIC_URL") or "").rstrip("/")
            if base:
                return base
            host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "localhost:8080"
            host = host if re.fullmatch(r"[A-Za-z0-9.:\[\]-]{1,200}", host) else "localhost:8080"
            return f"{'https' if self._secure() else 'http'}://{host}{BASE}"

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
            page = mcp.consent_page(p, c, bool(self._who().owner), ui.oauth.form_token(p),
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
            if not self._who().owner:  # a person signed in to their Claude on this farm (MY CLAUDE) connects
                return self._consent({**p, "name": f.get("name", "")},
                                     "sign in to your Claude on this farm first (MY CLAUDE), then connect again")
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
            who = self._who()
            q = {k: v[-1] for k, v in parse_qs(urlsplit(self.path).query).items()}
            q["profile"] = q.get("profile") or ui.first_profile(who)
            prof = ui.browsers.registry.get(q["profile"])
            if not (prof and ui.profile_mine(prof, who)):  # a Claude's own browser: its person only
                return self._err(401 if not who.can_view else 403, "only that profile's Claude's person sees it")
            try:
                slot = ui.browsers.slot(q.get("profile") or "")
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
            self._uncount()  # a screen stays open for as long as it's watched: a roll doesn't wait for it
            browser.bridge(self.rfile, self.wfile, self.connection, vnc)

        def _browser_csp(self) -> str:
            host = self.headers.get("Host") or ""
            ws = f" ws://{host} wss://{host}" if re.fullmatch(r"[A-Za-z0-9.:\[\]-]{1,200}", host) else ""
            return CSP.replace("connect-src 'self'", "connect-src 'self'" + ws)

        # ----------------------------------------------------------- GET
        def do_GET(self):
            if self.path.split("?", 1)[0] == "/healthz":  # the container health check, with or without a prefix
                return self._json(ui.health())
            if self.path.split("?", 1)[0] == f"{BASE}/pair" or self.path.startswith(f"{BASE}/pair/"):
                return self._pair_link()
            if self.path.split("?", 1)[0] == f"{BASE}/sso":
                return self._sso()
            if self.path.split("?", 1)[0].startswith(f"{BASE}/invite/"):
                return self._invite_link()
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
                    return self._json(ui.health())
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
                who = self._who()
                if path == "/api/me":
                    st = ui.store.settings()
                    return self._json({**who.view(), "user": "farmer" if who.manager else None, "farm": ui.cfg.farm,
                                       "version": __version__, "private": ui.private(st),
                                       "invite": bool(self._invited()),
                                       "hatch": self._hatch_view(who)}, 200 if who.can_view else 401)
                if not who.can_view:
                    return self._err(401, "this farm is private: log in first")
                if path == "/api/state":
                    body, tag = ui.state_for(who)
                    if self.headers.get("If-None-Match") == tag:
                        self._headers(304, "application/json", {"ETag": tag, "Cache-Control": "no-cache"})
                        return
                    self._headers(200, "application/json; charset=utf-8", {"ETag": tag, "Cache-Control": "no-cache"},
                                  len(body))
                    self.wfile.write(body)
                    return
                if path == "/api/tasks":
                    body, tag = ui.tasks_for(who, {k: v[-1] for k, v in parse_qs(urlsplit(self.path).query).items()})
                    if self.headers.get("If-None-Match") == tag:
                        self._headers(304, "application/json", {"ETag": tag, "Cache-Control": "no-cache"})
                        return
                    self._headers(200, "application/json; charset=utf-8", {"ETag": tag, "Cache-Control": "no-cache"},
                                  len(body))
                    self.wfile.write(body)
                    return
                m = re.fullmatch(r"/api/tasks/([a-z0-9]{6,40})", path)
                if m:  # one sub-agent: its instructions, its result so far and its runs
                    t = ui.store.get_task(m.group(1))
                    if not t:
                        return self._err(404, "no such sub-agent")
                    full = who.owns(t.get("owner")) or who.owns(t.get("to"))
                    runs = [{k: r.get(k) for k in ("worker", "started", "duration_s", "ok", "turns", "terminal_reason",
                                                   "tokens")}
                            for r in ui.store.runs(t["id"])]
                    return self._json({**_public_task(t, full=full, summary=full), "runs": runs[-10:], "full": full,
                                       "on": t.get("worker", "").split("@")[0] if t.get("status") == "running"
                                       else t.get("to")})
                if path == "/api/approvals":  # the missions and messages waiting for this person's OK
                    if not (who.manager or who.owner):
                        return self._err(403, "only a Claude's person approves its missions")
                    return self._json(self._approvals(who))
                if path == "/api/tools":  # the groups a person can turn off, for the hatch and SETTINGS forms
                    return self._json(policy.groups_view())
                if not (who.manager or who.owner):
                    return self._err(403, "the farm manager or a Claude's person only")
                if path == "/api/manager":
                    if not who.manager:
                        return self._err(403, "the farm manager only")
                    return self._json(self._manager_view())
                if path == "/api/slack":
                    if not who.manager:
                        return self._err(403, "the farm manager only")
                    return self._json(ui.slack.view())
                if path == "/api/connectors":  # the CONNECTORS menu: what the farm is connected to (never a key)
                    return self._json(ui.connectors_view(who))
                if path == "/api/browser":
                    return self._json(ui.browser_view(who))
                m = re.fullmatch(r"/api/agents/([a-z0-9@._-]+)/tools", path)
                if m:  # what that Claude can use, as its last run saw it
                    if not who.owns(m.group(1)):
                        return self._err(403, "only its person sees what a Claude can use")
                    t = ui.store.tools().get(m.group(1))
                    return self._json({k: v for k, v in t.items() if k not in ("PK", "SK", "ver")} if t else {"tools": None})
                m = re.fullmatch(r"/api/agents/([a-z0-9@._-]+)/settings", path)
                if m:
                    if not who.owns(m.group(1)):
                        return self._err(403, "only its person changes a Claude's settings")
                    return self._json(ui.settings_view(m.group(1)))
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
                    claude = q.get("claude") if who.manager else who.owner
                    keep = ("id", "claude", "runs_on", "kind", "task", "title", "turns", "started", "last_at", "ended")
                    return self._json([{k: s.get(k) for k in keep} for s in ui.store.sessions(claude, 50)])
                m = re.fullmatch(r"/api/sessions/([A-Za-z0-9-]+)", path)
                if m:  # one session's whole conversation
                    s = ui.store.session(m.group(1))
                    if not s or not who.owns(s.get("claude")):
                        return self._err(404, "no such session")
                    return self._json({"id": s["id"], "claude": s.get("claude"), "kind": s.get("kind"),
                                       "title": s.get("title"), "task": s.get("task"), "started": s.get("started"),
                                       "conversation": [{k: t.get(k) for k in ("role", "kind", "text", "at")}
                                                        for t in ui.store.turns(s["id"])]})
                m = re.fullmatch(r"/api/agents/([a-z0-9@._-]+)/login", path)
                if m:
                    if not who.owns(m.group(1)):
                        return self._err(403, "only its person logs a Claude in")
                    s = ui.manager.login(m.group(1))
                    return self._json(s.view() if s else {"state": "none"})
                return self._err(404, "not found")
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:  # noqa: BLE001 - one bad request must never take the UI down
                return self._err(500, f"{type(e).__name__}: {str(e)[:200]}")

        # ------------------------------------------------ owners and approvals
        def _paired(self, cid: str):
            ui.store.put_claude(cid, owned=True)
            ui.store.event("owner.paired", f"{cid}'s person signed in on a new device", by="ui")
            ui._views.clear()
            return self._json({"ok": True, "claude": cid}, extra={"Set-Cookie": self._owner_cookie(cid)})

        def _pair_link(self):
            """GET /pair/<token>: the link a Claude gives its person in the Claude app. Signs this device in to that
            Claude (once; the link then stops working) and opens the farm."""
            token = self.path.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
            cid = ui.store.take_pairing(token_hash=hashlib.sha256(token.encode()).hexdigest()) \
                if re.fullmatch(r"[A-Za-z0-9_-]{16,80}", token) else None
            self.send_response(302)
            self.send_header("Location", f"{BASE}/" + ("?paired=1" if cid else "?paired=0"))
            self.send_header("Cache-Control", "no-store")
            if cid:
                ui.store.put_claude(cid, owned=True)
                ui.store.event("owner.paired", f"{cid}'s person signed in with a pairing link", by="ui")
                self.send_header("Set-Cookie", self._owner_cookie(cid))
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _sso(self):
            """GET /sso?t=<token>: a link from the farm's host (FARM_UI_SSO_KEY, see sso.py) signs this device in as
            the person of the farm's manager Claude. Once: the link then stops working."""
            ip, key = self._ip(), os.environ.get("FARM_UI_SSO_KEY", "")
            token = (parse_qs(urlsplit(self.path).query).get("t") or [""])[0]
            claims = None if ui.lock.locked_out(ip) else sso.read(key, ui.cfg.farm, token)
            cid = ui.managers()[0] if claims else None
            if not (claims and ui.manager.get(cid) and ui.store.use_sso(claims["n"], claims["exp"])):
                ui.lock.fail(ip)
                return self._err(403, "that sign-in link is wrong, used or expired: open your farm again from where "
                                      "you got it")
            ui.store.put_claude(cid, owned=True)
            ui.store.event("owner.sso", f"{cid}'s person signed in with a link from the farm's host", by="ui")
            ui._views.clear()
            self.send_response(302)
            self.send_header("Location", f"{BASE}/")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Set-Cookie", self._owner_cookie(cid))
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _invite_link(self):
            """GET /invite/<token>: keeps the invite on this device (a cookie) and opens the farm, which shows only
            LOG IN WITH YOUR CLAUDE. The invite is spent at the login (_hatch_invited), not here."""
            token = self.path.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
            th = hashlib.sha256(token.encode()).hexdigest() if re.fullmatch(r"[A-Za-z0-9_-]{16,80}", token) else ""
            ok = bool(th) and bool(ui.store.invite(th))
            self.send_response(302)
            self.send_header("Location", f"{BASE}/" + ("?invited=1" if ok else "?invited=0"))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            if ok:
                self.send_header("Set-Cookie", self._named_cookie(INVITE_COOKIE, ui.keys.make("invite", [th], 7), 7 * 86400))
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _invited(self) -> str | None:
            """The invite this device holds, while it still works: its token's hash."""
            c = SimpleCookie(self.headers.get("Cookie") or "")
            got = ui.keys.read(c[INVITE_COOKIE].value if INVITE_COOKIE in c else None, "invite")
            return got[0] if got and ui.store.invite(got[0]) else None

        def _hatch_invited(self, data: dict):
            """POST /api/agents {invite: true}: the invited person's own Claude, waiting for their login."""
            th = self._invited()
            if not th:
                return self._err(410, "that invite was used or has expired: ask for a new one")
            store, mgr = ui.store, ui.manager
            a = mgr.create(str(data.get("name", "")), start=False)  # the plan's cap first (ValueError: 400)
            if not store.take_invite(th):  # someone was quicker with the same link
                mgr.remove(a["id"])
                return self._err(410, "that invite was just used: ask for a new one")
            tools = policy.clean(None)
            store.put_claude(a["id"], name=a["name"], hat=a.get("hat"), approve_missions=True, tools=tools, owned=True,
                             hatched_by="invite")
            policy.save(a["config_dir"], tools, claude=a["id"])
            store.event("agent.added", f"{a['id']} hatched with an invite (waiting for its login); its person approves "
                        "every mission", by="ui")
            ui._state_cache = None
            ui._views.clear()
            mgr.start_login(a["id"])
            self.send_response(200)
            body = json.dumps({"id": a["id"], "name": a["name"]}).encode()
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Set-Cookie", self._owner_cookie(a["id"]))
            self.send_header("Set-Cookie", self._named_cookie(INVITE_COOKIE, "", 0))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _hatch_view(self, who: Who) -> dict:
            st = ui.store.settings()
            n = len([a for a in ui.manager.all() if not a.get("primary")])
            cap = ui.manager.max_claudes()  # the host's plan: a ceiling for everyone, the manager too
            most = int(st.get("max_claudes") or 100) if cap is None else min(int(st.get("max_claudes") or 100), cap)
            why = room_note(cap) if cap is not None and n >= cap else \
                "" if who.manager else "you have a Claude on this farm already" if who.owner else \
                "hatching is closed on this farm" if not st.get("hatch_open") else \
                "this farm is full" if n >= most else \
                "" if who.can_view else "log in first"
            return {"can": not why, "why": why, "claudes": n, "max": most}

        def _approvals(self, who: Who) -> list[dict]:
            out = []
            for p in ui.store.pending(None if who.manager else who.owner):
                if p["type"] == "task":
                    out.append({"type": "task", "id": p["id"], "to": p.get("to"), "title": p.get("title"),
                                "prompt": (p.get("prompt") or "")[:20000], "from": (p.get("approval") or {}).get("from")
                                or p.get("owner") or p.get("created_by"), "at": p.get("created"),
                                "expires_at": (p.get("approval") or {}).get("expires_at")})
                else:
                    out.append({"type": "message", "id": p["SK"], "to": p.get("to"), "title": "a message",
                                "prompt": p.get("text", ""), "from": p.get("from"), "at": p.get("at"),
                                "expires_at": p.get("expires_at_held")})
            return sorted(out, key=lambda x: float(x.get("at") or 0))

        def _manager_view(self) -> dict:
            st = ui.store.settings()
            owners = [{"id": c["id"], "owned": bool(c.get("owned")), "approve_missions": bool(c.get("approve_missions"))}
                      for c in ui.store.claudes()]
            return {"settings": {k: st.get(k) for k in ("private", "hatch_open", "max_claudes", "hatch_per_ip_hour")}
                    | {"private": ui.private(st),
                       "private_by_host": os.environ.get("FARM_UI_PRIVATE") == "1", "plan_claudes": ui.manager.max_claudes()},
                    "planner": ui.store.planner(), "claudes": owners, "release": boot.running(),
                    "managers": ui.managers(st),
                    "version": __version__, "hosts": [a["id"] for a in ui.manager.all()]}

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
                if path == "/api/login":  # there are no passwords: people sign in with their Claude
                    return self._err(410, "this farm has no password: sign in with your Claude (\"farm login\" in the Claude app)")
                if path == "/api/logout":
                    self.send_response(200)
                    for c in (self._cookie("", 0), self._named_cookie(VIEWER_COOKIE, "", 0)):  # old manager cookies too
                        self.send_header("Set-Cookie", c)
                    body = b'{"ok": true}'
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if path == "/api/owner/forget":  # this device forgets which Claude is mine
                    return self._json({"ok": True}, extra={"Set-Cookie": self._named_cookie(OWNER_COOKIE, "", 0)})
                if path == "/api/pair":  # the code a Claude gave its person in the Claude app
                    if ui.lock.locked_out(self._ip()):
                        return self._err(429, "too many tries: wait five minutes")
                    cid = ui.store.take_pairing(code=str(data.get("code", ""))[:20])
                    if not cid:
                        ui.lock.fail(self._ip())
                        return self._err(401, "that code is wrong or used up: ask your Claude for a new one")
                    return self._paired(cid)
                if path == "/api/agents" and data.get("invite"):
                    return self._hatch_invited(data)
                if not self._who().can_view:
                    return self._err(401, "this farm is private: log in first")
                return self._post(path, data)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except (ValueError, KeyError) as e:
                return self._err(400, str(e)[:300])
            except Exception as e:  # noqa: BLE001
                return self._err(500, f"{type(e).__name__}: {str(e)[:200]}")

        def _post(self, path: str, data: dict):
            store, mgr, who = ui.store, ui.manager, self._who()
            ui._state_cache = None
            ui._views.clear()
            ui._tasks_cache.clear()
            if path in ("/api/agents",):  # hatching: anyone who may watch, once (see _hatch_view)
                return self._hatch(data, who)
            m = re.fullmatch(r"/api/approvals/([A-Za-z0-9]{6,40})/(approve|deny)", path)
            if m:
                pid, action = m.groups()
                item = store.get_task(pid) if store.get_task(pid) else store.b.get("HELD", pid)
                if not item:
                    return self._err(404, "nothing waits under that id (decided already?)")
                if not who.owns(item.get("to")):
                    return self._err(403, "only its Claude's person decides")
                by = f"owner:{who.owner}" if who.owner == item.get("to") else "manager"
                ok = store.approve(pid, by) if action == "approve" else store.deny(pid, by, str(data.get("reason") or "")[:200])
                if not ok:
                    raise ValueError("it was decided already")
                return self._json(self._approvals(who))
            m = re.fullmatch(r"/api/agents/([a-z0-9._-]+)/settings", path)
            if m:
                if not who.owns(m.group(1)) or not mgr.get(m.group(1)):
                    return self._err(403, "only its person changes a Claude's settings")
                ui.save_settings(m.group(1), data, by=f"owner:{who.owner}" if who.owner == m.group(1) else "manager")
                return self._json(ui.settings_view(m.group(1)))
            m = re.fullmatch(r"/api/agents/([a-z0-9._-]+)/(login|code|remove|cancel-login)", path)
            if m and not who.owns(m.group(1)):
                return self._err(403, "only its person (or the farm manager) does that")
            m = re.fullmatch(r"/api/tasks/([a-z0-9]{6,40})/(cancel|retry)", path)
            if m:
                t = store.get_task(m.group(1)) or {}
                if not (who.owns(t.get("owner")) or who.owns(t.get("to"))):
                    return self._err(403, "only the person of the Claude it belongs to (or the farm manager)")
            if path.startswith("/api/browser/"):
                return self._browser_post(path, data, who)
            if path == "/api/schedules":
                if not (who.manager or who.owner):
                    return self._err(403, "the farm manager or a Claude's person only")
                if not who.manager:
                    data = {**data, "on": who.owner}  # a person schedules work for their own Claude
            m = re.fullmatch(r"/api/schedules/([a-z0-9]{6,40})/(pause|resume|run|remove)", path)
            if m:
                sc = store.b.get("SCHEDULE", m.group(1)) or {}
                if not (who.owns(sc.get("owner")) or who.owns(sc.get("to"))):
                    return self._err(403, "only its Claude's person (or the farm manager)")
            if path.startswith("/api/manager/"):
                if not who.manager:
                    return self._err(403, "the farm manager only")
                return self._manager_post(path, data)
            manager_only = path in ("/api/pause", "/api/resume", "/api/dashboards/rename-folder") or \
                path.startswith("/api/slack") or re.fullmatch(r"/api/dashboards/[a-z0-9-]{1,48}/move", path)
            if manager_only and not who.manager:
                return self._err(403, "the farm manager only")
            if not (who.manager or who.owner):
                return self._err(403, "the farm manager or a Claude's person only")
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
            return self._rest(path, data, who)

        def _browser_post(self, path: str, data: dict, who: Who):
            """The farm's browser: a person manages their own Claude's profiles; the manager, every profile."""
            store = ui.store
            name = str(data.get("profile") or (ui.first_profile(who) if path != "/api/browser/add" else ""))
            if path == "/api/browser/add":
                if not (who.manager or who.owner):
                    return self._err(403, "a Claude's person adds profiles for it")
                # the manager says whose it is: a Claude, or blank for the farm's own (never "mine" by default)
                owner = ((str(data["owner"] or "") or None) if "owner" in data else who.owner) if who.manager \
                    else who.owner
                ui.browsers.registry.add(name, by="ui", owner=owner)
                store.event("browser.added", f"browser profile {name} added from the farm UI"
                            + (f" for {owner}" if owner else ""), by="ui")
                ui.manager.share_browser_tools()
                threading.Thread(target=ui.browsers.sync, name="browser-sync", daemon=True).start()
                return self._json(ui.browser_view(who))
            if path == "/api/browser/proxy/address":
                if not who.manager:
                    return self._err(403, "the farm manager sets the farm's proxy")
            else:
                prof = ui.browsers.registry.get(name)
                if not prof:
                    return self._err(404, f"no browser profile named {name}")
                # the manager runs the profiles (on/off, proxy, whose, remove); only its Claude's person looks inside
                admin = who.manager and path != "/api/browser/open"
                if not (admin or ui.profile_mine(prof, who)):
                    return self._err(403, "that profile is another Claude's: only its person uses it")
            if path == "/api/browser/assign":
                if not who.manager:
                    return self._err(403, "the farm manager assigns profiles")
                ui.browsers.registry.set_owner(name, str(data.get("owner") or "") or None)
                ui.manager.share_browser_tools()
                return self._json(ui.browser_view(who))
            if path in ("/api/browser/start", "/api/browser/stop"):
                on = path.endswith("start")
                if on and not browser.available():
                    raise ValueError("this image has no browser: " + ", ".join(browser.missing() or ["FARM_BROWSER=0"]))
                if ui.browsers.want(name, on, by="ui"):
                    store.event("browser.started" if on else "browser.stopped",
                                f"browser profile {name} {'started' if on else 'stopped'} from the farm UI", by="ui")
                threading.Thread(target=ui.browsers.sync, name="browser-sync", daemon=True).start()
                return self._json(ui.browser_view(who))
            if path == "/api/browser/proxy":  # a profile through the proxy (checked first, from a country) or direct
                on = bool(data.get("on"))
                country = str(data["country"])[:8] if data.get("country") is not None else None
                changed, seen = ui.browsers.proxy(name, on, by="ui", country=country)
                if changed:
                    store.event("browser.proxy", f"browser profile {name} " + (f"through the proxy{_from(seen)}" if on
                                else "direct") + " from the farm UI", by="ui")
                threading.Thread(target=ui.browsers.sync, name="browser-sync", daemon=True).start()
                return self._json({**ui.browser_view(who), "seen": seen})
            if path == "/api/browser/proxy/address":  # set the farm's proxy (checked first), or "" to forget it
                if os.environ.get(browser.PROXY_ENV):
                    raise ValueError(f"{browser.PROXY_ENV} is set in the farm's environment: change it there")
                text = str(data.get("address") or "")[:2000].strip()
                if not text:
                    browser.save_proxy(ui.cfg.workspace, None)
                    store.event("browser.proxy", "the browser's proxy removed from the farm UI", by="ui")
                    threading.Thread(target=ui.browsers.sync, name="browser-sync", daemon=True).start()
                    return self._json(ui.browser_view(who))
                p, name = browser.parse_proxy(text), str(data.get("profile") or "")
                seen = ui.browsers.set_proxy(p, name, by="ui")  # set from a profile: that profile goes through it
                store.event("browser.proxy", f"the browser's proxy set to {p['host']}:{p['port']} from the farm UI"
                            + (f"; profile {name} through it{_from(seen)}" if name else ""), by="ui")
                threading.Thread(target=ui.browsers.sync, name="browser-sync", daemon=True).start()
                return self._json({**ui.browser_view(who), "seen": seen})
            if path == "/api/browser/type":  # what you paste (or SEND) goes in where the cursor is, any language
                text = str(data.get("text") or "")[:20000]
                try:
                    return self._json(browser.insert_text(text, ui.browsers.slot(name)))
                except OSError as e:
                    return self._err(503, f"couldn't type into that profile's browser: {e}")
            if path == "/api/browser/open":
                url = browser.normalize_url(str(data.get("url", ""))[:2000])
                slot = ui.browsers.slot(name)
                try:
                    return self._json(browser.open_url(url, slot))
                except OSError:
                    return self._err(503, "that profile's browser is not running: start it first")
            if path == "/api/browser/remove":
                if ui.browsers.registry.remove(name):
                    store.event("browser.removed", f"browser profile {name} and its logins removed from the farm UI",
                                by="ui")
                ui.manager.share_browser_tools()
                threading.Thread(target=ui.browsers.sync, name="browser-sync", daemon=True).start()
                return self._json(ui.browser_view(who))
            return self._err(404, "not found")

        def _hatch(self, data: dict, who: Who):
            """A new Claude (or bot). Anyone who may watch the farm hatches one, once: the browser that hatched it
            gets its owner cookie. The person chooses its skin, whether they approve every mission sent to it, and
            which tools it may use."""
            store, mgr = ui.store, ui.manager
            hv = self._hatch_view(who)
            if not hv["can"]:
                return self._err(409 if who.owner else 403, hv["why"])
            ip = self._ip()
            if not who.manager:
                limit = int(store.settings().get("hatch_per_ip_hour") or 3)
                recent = [t for t in ui.hatches.get(ip, []) if t > time.time() - 3600]
                if len(recent) >= limit:
                    return self._err(429, f"{limit} Claudes an hour from one address: try again later")
                ui.hatches[ip] = recent + [time.time()]
            tools = policy.clean(data.get("tools"))
            approve = bool(data.get("approve_missions", not who.manager))
            skin = _skin(data.get("skin"))
            if isinstance(data.get("bot"), dict):  # a bot: kept once its provider answers
                bot = bots.parse(data["bot"])
                key = bots.check_key(bot["provider"], str(data["bot"].get("key") or ""))
                said = bots.check(bot, key)
                a = mgr.create(str(data.get("name", "")), bot=bot, key=key, start=False)
                what = f"a bot on {bot['model']} via {bots.label(bot)}"
            else:
                a, said = mgr.create(str(data.get("name", "")), start=False), None
                what = "waiting for its login"
            owned = not who.owner  # the manager hatching for someone else still gets it on this device
            store.put_claude(a["id"], name=a["name"], hat=skin.get("hat") or a.get("hat"), colors=skin.get("colors"),
                             accessory=skin.get("accessory"), approve_missions=approve, tools=tools, owned=owned,
                             hatched_by="manager" if who.manager else "public")
            policy.save(a["config_dir"], tools, claude=a["id"])
            store.event("agent.added", f"{a['id']} hatched from the farm UI ({what})"
                        + ("; its person approves every mission" if approve else "")
                        + (f"; tools off: {', '.join(tools['deny'])}" if tools["deny"] else ""), by="ui")
            if not a.get("bot"):
                mgr.start_login(a["id"])
            out = {"id": a["id"], "name": a["name"], "said": said}
            return self._json(out, extra={"Set-Cookie": self._owner_cookie(a["id"])} if owned else None)

        def _manager_post(self, path: str, data: dict):
            store = ui.store
            if path == "/api/manager/settings":
                ch = {}
                if "private" in data:
                    ch["private"] = bool(data["private"])
                # there are no viewer passwords: a private farm is its Claudes' people (and its manager) only
                for k in ("hatch_open",):
                    if k in data:
                        ch[k] = bool(data[k])
                for k, lo, hi in (("max_claudes", 1, 1000), ("hatch_per_ip_hour", 1, 100)):
                    if k in data:
                        ch[k] = max(lo, min(hi, int(data[k])))
                store.set_settings(**ch)
                store.event("farm.settings", "farm settings changed by the manager: " +
                            ", ".join(f"{k}={v}" for k, v in ch.items() if "viewer_" not in k), by="ui")
                return self._json(self._manager_view())
            if path == "/api/manager/planner":
                ch = {}
                if "on" in data:
                    ch["on"] = bool(data["on"])
                if "goal" in data:
                    ch["goal"] = str(data["goal"] or "")[:4000]
                if "host" in data:
                    host = str(data["host"] or "")
                    if host and host not in {a["id"] for a in ui.manager.all()}:
                        raise ValueError(f"no Claude named {host} on this farm")
                    ch["host"] = host
                if data.get("every"):
                    from .schedule import parse_every
                    ch["every_s"] = max(60, parse_every(str(data["every"])[:20]))
                if ch.get("on") and not (ch.get("goal") or store.planner().get("goal")):
                    raise ValueError("give the planner a goal first")
                store.set_planner(**ch)
                store.event("planner.changed", "planner " + ", ".join(f"{k}={str(v)[:80]}" for k, v in ch.items()),
                            by="ui")
                return self._json(self._manager_view())
            if path == "/api/manager/managers":  # who runs the farm: add a Claude, remove one, or hand it over
                cur, ids = ui.managers(), {a["id"] for a in ui.manager.all()}
                cid = str(data.get("claude") or "")
                if cid not in ids:
                    raise ValueError(f"no Claude named {cid!r} on this farm")
                action = str(data.get("action") or "add")
                new = cur + [cid] if action == "add" else [m for m in cur if m != cid] if action == "remove" \
                    else [cid] if action == "set" else None
                if new is None:
                    raise ValueError("action: add, remove or set")
                new = list(dict.fromkeys(new))
                if not new:
                    raise ValueError("the farm needs a manager: give the role to another Claude first")
                store.set_settings(managers=new)
                store.event("farm.managers", f"the farm's managers: {', '.join(new)} ({action} {cid})", by="ui")
                return self._json(self._manager_view())
            m = re.fullmatch(r"/api/manager/owners/([a-z0-9._-]+)/signout", path)
            if m:  # every device signed in to that Claude signs out
                rec = store.claude(m.group(1))
                store.put_claude(m.group(1), owner_ver=int(rec.get("owner_ver", 1)) + 1, owned=False)
                store.event("owner.signout", f"{m.group(1)}'s person signed out everywhere by the manager", by="ui")
                return self._json(self._manager_view())
            if path == "/api/manager/invite":  # a link that lets one person hatch their own Claude here, once
                token = secrets.token_urlsafe(24)
                store.add_invite(hashlib.sha256(token.encode()).hexdigest(), by="manager")
                store.event("farm.invite", "the manager made an invite link (works once, for 7 days)", by="ui")
                cap = ui.manager.max_claudes()
                added = len([a for a in ui.manager.all() if not a.get("primary")])
                return self._json({"link": f"{self._public_base()}/invite/{token}", "expires_in": 7 * 86400,
                                   "room": cap is None or added < cap})
            if path == "/api/manager/roll-ui":
                from . import procs
                os.makedirs(procs.pids_dir(ui.cfg.workspace), exist_ok=True)
                open(os.path.join(procs.pids_dir(ui.cfg.workspace), "ui-roll"), "a").close()
                return self._json({"ok": True})
            return self._err(404, "not found")

        def _rest(self, path: str, data: dict, who: Who):
            store, mgr = ui.store, ui.manager
            m = re.fullmatch(r"/api/tasks/([a-z0-9]{6,40})/(cancel|retry)", path)
            if m:
                tid, action = m.groups()
                if not store.get_task(tid):
                    return self._err(404, "no such sub-agent")
                ok = store.cancel(tid) if action == "cancel" else store.retry(tid)
                if not ok:
                    raise ValueError("it already finished" if action == "cancel" else "it is still at work")
                return self._json(ui.tasks_view(who))
            m = re.fullmatch(r"/api/dashboards/([a-z0-9-]{1,48})/move", path)
            if m or path == "/api/dashboards/rename-folder":  # organizing the dashboards list
                if m:
                    dashboards.move(store, m.group(1), str(data.get("folder") or "")[:200], by="ui")
                else:
                    dashboards.rename_folder(store, str(data.get("from") or "")[:200], str(data.get("to") or "")[:200],
                                             by="ui")
                return self._json([dashboards.summary(store, d) for d in dashboards.all_(store)])
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
                    store.add_schedule(title, prompt[:100_000] or title, tz=tz, to=on,
                                       created_by="ui" if who.manager else f"owner:{who.owner}",
                                       owner=on or ui.cfg.name, **spec)
                except (ValueError, KeyError) as e:  # an unknown time zone is a KeyError
                    raise ValueError(f"bad schedule: {str(e).strip(chr(39))}") from None
                return self._json(ui.tasks_view(who))
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
                return self._json(ui.tasks_view(who))
            if path == "/api/connectors/stripe":  # connect (checked with Stripe first), or change the key
                if not who.manager:
                    return self._err(403, "the farm manager connects the farm's Stripe")
                v = connectors.stripe_connect(ui.cfg.workspace, str(data.get("key") or "")[:300],
                                              by=f"owner:{who.owner}" if who.owner else "manager")
                ui.manager.share_connectors()
                acct = (v.get("account") or {}).get("name")
                store.event("connector.stripe", f"Stripe connected ({v['mode']} mode" + (f", {acct}" if acct else "")
                            + f", key …{v['last4']}): every Claude gets mcp__stripe__*", by="ui")
                return self._json(ui.connectors_view(who))
            if path == "/api/connectors/stripe/disconnect":
                if not who.manager:
                    return self._err(403, "the farm manager disconnects the farm's Stripe")
                if connectors.stripe_disconnect(ui.cfg.workspace):
                    ui.manager.share_connectors()
                    store.event("connector.stripe", "Stripe disconnected: the Claudes' Stripe tools are gone", by="ui")
                return self._json(ui.connectors_view(who))
            if path == "/api/connectors/blender":  # connect (the farm opens an MCP session with it first), or change it
                if not who.manager:
                    return self._err(403, "the farm manager connects the farm's Blender")
                v = connectors.blender_connect(ui.cfg.workspace, str(data.get("url") or "")[:1000],
                                               str(data.get("token") or "")[:2000],
                                               by=f"owner:{who.owner}" if who.owner else "manager")
                ui.manager.share_connectors()
                srv = v.get("server") or {}
                store.event("connector.blender", f"Blender connected ({srv.get('title') or srv.get('name') or 'an MCP server'}"
                            f"): every Claude gets {v['tools']}", by="ui")
                return self._json(ui.connectors_view(who))
            if path == "/api/connectors/blender/disconnect":
                if not who.manager:
                    return self._err(403, "the farm manager disconnects the farm's Blender")
                if connectors.blender_disconnect(ui.cfg.workspace):
                    ui.manager.share_connectors()
                    store.event("connector.blender", "Blender disconnected: the Claudes' Blender tools are gone", by="ui")
                return self._json(ui.connectors_view(who))
            if path == "/api/connectors/google-ads":  # connect (checked with Google first), or change the credentials
                if not who.manager:
                    return self._err(403, "the farm manager connects the farm's Google Ads")
                creds = {k: str(data.get(k) or "")[:600] for k in (*connectors.GADS_FIELDS, *connectors.GADS_OPTIONAL)}
                v = connectors.gads_connect(ui.cfg.workspace, creds, by=f"owner:{who.owner}" if who.owner else "manager")
                ui.manager.share_connectors()
                tok = f", developer token …{v['developer_token_last4']}" if v.get("developer_token_last4") else ""
                store.event("connector.google_ads", f"Google Ads connected ({len(v['customers'])} account(s){tok}): "
                            "every Claude can use `clodfarm gads`", by="ui")
                return self._json(ui.connectors_view(who))
            if path == "/api/connectors/google-ads/disconnect":
                if not who.manager:
                    return self._err(403, "the farm manager disconnects the farm's Google Ads")
                if connectors.gads_disconnect(ui.cfg.workspace):
                    ui.manager.share_connectors()
                    store.event("connector.google_ads", "Google Ads disconnected", by="ui")
                return self._json(ui.connectors_view(who))
            if path == "/api/slack/allow":
                ui.slack.set_allow([x.strip() for x in re.split(r"[,\s]+", str(data.get("allow") or "")) if x.strip()][:200])
                return self._json(ui.slack.view())
            if path == "/api/slack/disconnect":
                ui.slack.disconnect()
                return self._json(ui.slack.view())
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
                store.forget_claude(aid)  # its settings, and every owner cookie for it
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
    _uncounted: set = set()

    def get_request(self):
        conn, addr = self.socket.accept()
        conn.setblocking(True)  # on BSD/macOS it would inherit the shared listener's non-blocking mode
        return conn, addr

    # every connection it took counts until it's answered, from the moment it's accepted: a roll waits for them
    def process_request(self, request, client_address):
        with self.ui._inflight_lock:
            self.ui.inflight += 1
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.uncount(request)

    def uncount(self, request, early: bool = False):
        """Done with it (``early``: a browser screen, which stays open while watched, stops counting at once)."""
        with self.ui._inflight_lock:
            key = id(request)
            if early:
                self._uncounted.add(key)
                self.ui.inflight -= 1
            elif key in self._uncounted:
                self._uncounted.discard(key)
            else:
                self.ui.inflight -= 1

    def server_bind(self):
        # a new UI process binds next to the old one while it takes over (uikeeper.py)
        if os.environ.get("FARM_UI_REUSEPORT") == "1" and hasattr(socket, "SO_REUSEPORT"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        super().server_bind()


def serve(cfg, store: Store | None = None, manager: AgentManager | None = None, block: bool = True):
    """Start the UI (and keep the added agents running). Returns the server when ``block`` is False."""
    ui = FarmUI(cfg, store, manager)
    host, port = os.environ.get("FARM_UI_HOST", "0.0.0.0"), int(os.environ.get("FARM_UI_PORT", "8080"))
    fd = os.environ.pop("FARM_UI_FD", "")
    if fd.isdigit():  # the farm daemon's socket, shared with the UI process this one replaces (uikeeper.py)
        httpd = FarmHTTPServer((host, port), make_handler(ui), bind_and_activate=False)
        httpd.socket.close()
        httpd.socket = socket.fromfd(int(fd), socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
        os.close(int(fd))
        httpd.server_address = httpd.socket.getsockname()[:2]
        # two processes accept from it during a roll: one that loses a race must not sit blocked in accept()
        httpd.socket.setblocking(False)
    else:
        httpd = FarmHTTPServer((host, port), make_handler(ui))
    httpd.ui = ui
    httpd._uncounted = set()
    managed = bool(os.environ.get("FARM_UI_PIDFILE"))  # its own process, kept by the farm daemon (uikeeper.py)
    if not managed:  # served on its own (`clodfarm ui`): nobody else keeps the added Claudes running
        threading.Thread(target=ui.manager.keep_alive, name="agents", daemon=True).start()
    threading.Thread(target=ui.browsers.keep, args=(ui.stopping,), name="browser", daemon=True).start()
    ui.slack.start()  # talk to the farm from Slack, once it's connected (the SLACK button)
    if block and threading.current_thread() is threading.main_thread():
        import signal
        # a roll (uikeeper.py): finish the requests in flight, then go; the browsers and the Claudes keep running
        signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=httpd.shutdown, daemon=True).start())
    if managed:
        from .uikeeper import mark_ready
        mark_ready()
    print(f"farm UI on http://{'localhost' if host in ('0.0.0.0', '::') else host}:{port}", flush=True)
    if not block:
        threading.Thread(target=httpd.serve_forever, name="ui", daemon=True).start()
        return httpd
    try:
        httpd.serve_forever()
    finally:
        if managed:  # rolled: let what it was answering finish (a browser screen doesn't count), then go
            t0 = time.time()
            while ui.inflight > 0 and time.time() - t0 < 10:
                time.sleep(0.05)
        ui.slack.stop()
        ui.stopping.set()
        if not managed:
            ui.browsers.shutdown()
            ui.manager.shutdown()
    return httpd
