"""The farm as a remote MCP server: connect Claude Code (or any MCP client) on your computer to the farm.

    claude mcp add --transport http farm https://<your farm>/mcp      then /mcp in Claude Code to sign in

Streamable HTTP at FARM_UI_BASE/mcp, and OAuth 2.1 (its person signs in to their Claude on the farm to connect it):
- discovery: protected-resource metadata (RFC 9728) and authorization-server metadata (RFC 8414);
- dynamic client registration (RFC 7591), with redirect URIs limited to loopback http or https;
- authorization code with PKCE S256 only, and a sign-in-and-consent page on the farm itself;
- access tokens last an hour and refresh tokens rotate (a reused refresh token ends the connection);
- tokens are bound to this farm's MCP URL (RFC 8707) and stored as SHA-256 hashes, never in the clear;
- two scopes: farm:read (look) and farm:work (start sub-agents, message Claudes, schedules). Logging Claudes in or
  out, releasing them and pausing the farm stay in the farm UI.

Every connection is a named guest on the farm: its messages come from that name, `clodfarm msg <name>` reaches it,
and its sub-agents show on the farm's own Claude's plot. `clodfarm connections` lists them; `clodfarm disconnect ID`
ends one. Standard library only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import threading
import time
from urllib.parse import urlencode, urlsplit

from . import __version__

SCOPES = {"farm:read": "see the farm: its Claudes, their usage, sub-agents, results, sessions and events",
          "farm:work": "start, cancel and retry sub-agents, message the Claudes and manage schedules"}
PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26")
ACCESS_TTL, REFRESH_TTL, CODE_TTL, FORM_TTL = 3600, 30 * 86400, 300, 900
MAX_CLIENTS = 100
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _loopback_or_https(uri: str) -> bool:
    try:
        u = urlsplit(uri)
    except ValueError:
        return False
    if u.fragment or not u.netloc:
        return False
    return u.scheme == "https" or (u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1", "::1"))


def slug(text: str, default: str = "guest") -> str:
    s = re.sub(r"[^a-z0-9._-]+", "-", (text or "").lower()).strip("-._")[:32]
    return s if s and NAME_RE.match(s) else default


class OAuthStore:
    """Registered clients, connections (grants) and hashed tokens, in one JSON file (mode 600) next to the UI's."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self.codes: dict[str, dict] = {}  # authorization codes live 5 minutes, in memory
        self.secret = None
        self.data = self._load()

    def _load(self) -> dict:
        try:
            d = json.load(open(self.path))
        except (OSError, ValueError):
            d = {}
        d.setdefault("clients", {})
        d.setdefault("grants", {})
        d.setdefault("access", {})
        d.setdefault("secret", secrets.token_hex(32))
        return d

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.data, f)
        os.replace(tmp, self.path)

    def reload(self):
        with self._lock:
            self.data = self._load()

    # ------------------------------------------------------------ clients
    def register(self, meta: dict) -> dict:
        uris = meta.get("redirect_uris")
        if not isinstance(uris, list) or not uris or len(uris) > 5 or \
                not all(isinstance(u, str) and len(u) < 500 and _loopback_or_https(u) for u in uris):
            raise ValueError("redirect_uris must be 1-5 https or loopback http (localhost) URLs")
        if meta.get("token_endpoint_auth_method", "none") != "none":
            raise ValueError("only public clients (token_endpoint_auth_method none) are supported")
        name = str(meta.get("client_name") or "MCP client")[:80]
        with self._lock:
            self._prune()
            used = {g["client_id"] for g in self.data["grants"].values()}
            spare = sorted((c["created"], cid) for cid, c in self.data["clients"].items() if cid not in used)
            while len(self.data["clients"]) >= MAX_CLIENTS and spare:  # registration is open: never let it fill up
                del self.data["clients"][spare.pop(0)[1]]
            if len(self.data["clients"]) >= MAX_CLIENTS:
                raise ValueError("too many connected clients; disconnect some first")
            cid = "cf_" + secrets.token_urlsafe(18)
            c = {"client_id": cid, "client_name": name, "redirect_uris": uris, "created": int(time.time())}
            self.data["clients"][cid] = c
            self._save()
        return {**c, "client_id_issued_at": c["created"], "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
                "scope": " ".join(SCOPES)}

    def client(self, cid: str) -> dict | None:
        return self.data["clients"].get(cid or "")

    def _prune(self):
        t = time.time()
        used = {g["client_id"] for g in self.data["grants"].values()}
        for cid, c in list(self.data["clients"].items()):
            if cid not in used and c["created"] < t - 86400:
                del self.data["clients"][cid]
        for k, a in list(self.data["access"].items()):
            if a["exp"] < t:
                del self.data["access"][k]
        for gid, g in list(self.data["grants"].items()):
            if g["refresh_exp"] < t:
                self._drop(gid)

    # -------------------------------------------------------------- forms
    def form_token(self, params: dict) -> str:
        """Binds the consent form to exactly these request parameters, for FORM_TTL seconds."""
        exp = str(int(time.time()) + FORM_TTL)
        msg = exp + "|" + json.dumps(params, sort_keys=True)
        return exp + "." + hmac.new(bytes.fromhex(self.data["secret"]), msg.encode(), hashlib.sha256).hexdigest()

    def form_ok(self, token: str, params: dict) -> bool:
        exp, _, sig = (token or "").partition(".")
        if not exp.isdigit() or int(exp) < time.time():
            return False
        msg = exp + "|" + json.dumps(params, sort_keys=True)
        good = hmac.new(bytes.fromhex(self.data["secret"]), msg.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, good)

    # --------------------------------------------------------------- codes
    def new_code(self, **fields) -> str:
        code = secrets.token_urlsafe(32)
        with self._lock:
            t = time.time()
            self.codes = {k: v for k, v in self.codes.items() if v["exp"] > t}
            self.codes[_h(code)] = {**fields, "exp": t + CODE_TTL}
        return code

    def take_code(self, code: str) -> dict | None:
        with self._lock:
            c = self.codes.pop(_h(code or ""), None)
        return c if c and c["exp"] > time.time() else None

    # -------------------------------------------------------------- grants
    def _issue(self, gid: str) -> dict:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        g = self.data["grants"][gid]
        t = time.time()
        self.data["access"][_h(access)] = {"grant": gid, "exp": t + ACCESS_TTL}
        g["refresh"], g["refresh_exp"], g["refreshed"] = _h(refresh), t + REFRESH_TTL, int(t)
        self._save()
        return {"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_TTL,
                "refresh_token": refresh, "scope": g["scope"], "name": g["name"]}

    def grant(self, client_id: str, name: str, scope: str, resource: str, owner: str | None = None) -> dict:
        """A new connection. ``owner``: the farm Claude whose person approved it (their computer's sessions show
        under that Claude)."""
        with self._lock:
            self._prune()
            gid = "c-" + secrets.token_hex(3)
            c = self.client(client_id) or {}
            self.data["grants"][gid] = {"id": gid, "client_id": client_id, "client_name": c.get("client_name", "?"),
                                        "name": name, "scope": scope, "resource": resource, "owner": owner,
                                        "created": int(time.time()), "last_used": None,
                                        "refresh": "", "refresh_exp": time.time() + REFRESH_TTL}
            return self._issue(gid)

    def refresh(self, token: str, client_id: str) -> dict | None:
        h = _h(token or "")
        with self._lock:
            for gid, g in self.data["grants"].items():
                if g.get("previous") == h:  # a refresh token used twice: someone copied it. End the connection.
                    self._drop(gid)
                    self._save()
                    return None
                if g["refresh"] and hmac.compare_digest(g["refresh"], h):
                    if g["client_id"] != client_id or g["refresh_exp"] < time.time():
                        return None
                    g["previous"] = h
                    for k, a in list(self.data["access"].items()):
                        if a["grant"] == gid:
                            del self.data["access"][k]
                    return self._issue(gid)
        return None

    def check(self, access: str) -> dict | None:
        a = self.data["access"].get(_h(access or ""))
        if not a or a["exp"] < time.time():
            return None
        g = self.data["grants"].get(a["grant"])
        if g and (not g["last_used"] or g["last_used"] < time.time() - 60):
            with self._lock:
                g["last_used"] = int(time.time())
                self._save()
        return g

    def _drop(self, gid: str):
        self.data["grants"].pop(gid, None)
        for k, a in list(self.data["access"].items()):
            if a["grant"] == gid:
                del self.data["access"][k]

    def revoke_token(self, token: str):
        h = _h(token or "")
        with self._lock:
            a = self.data["access"].pop(h, None)
            gid = a["grant"] if a else next((i for i, g in self.data["grants"].items() if g["refresh"] == h), None)
            if gid:
                self._drop(gid)
            self._save()

    def disconnect(self, gid: str) -> bool:
        with self._lock:
            self.reload()
            ok = gid in self.data["grants"]
            self._drop(gid)
            self._save()
        return ok

    def connections(self) -> list[dict]:
        keep = ("id", "name", "client_name", "scope", "owner", "created", "last_used")
        return [{k: g.get(k) for k in keep} for g in sorted(self.data["grants"].values(), key=lambda g: g["created"])]


def oauth_path(workspace: str) -> str:
    return os.path.join(workspace, ".farm", "mcp-auth.json")


def connection_names(workspace: str) -> set[str]:
    """Names of the guests connected over MCP: `clodfarm msg` may message them like a Claude."""
    try:
        return {g["name"] for g in json.load(open(oauth_path(workspace))).get("grants", {}).values()}
    except (OSError, ValueError):
        return set()


# ======================================================================= tools
def _schema(props: dict, required=()) -> dict:
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


S, I, B = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}
TOOLS = [
    ("farm_status", "farm:read", "The Claudes on the farm (each is one person's account), how much of its 5-hour and "
     "7-day usage each has used, how many sub-agents each may start now, and the sub-agents running or waiting.",
     _schema({})),
    ("farm_budget", "farm:read", "Every seat's (Claude account's) usage windows, reset times and what the budget "
     "governor allows it to run right now, with the reason.", _schema({})),
    ("farm_subagents", "farm:read", "The sub-agents running, waiting and queued; with all=true also the recently "
     "finished, failed and cancelled ones.", _schema({"all": B})),
    ("farm_result", "farm:read", "One sub-agent: its status, prompt, result and runs. wait_seconds (up to 45) waits "
     "for it to finish first.", _schema({"id": S, "wait_seconds": I}, ["id"])),
    ("farm_events", "farm:read", "The farm's event log, newest last: sub-agents, merges, checks, messages, pauses "
     "and usage limits.", _schema({"n": I})),
    ("farm_sessions", "farm:read", "Claude sessions on the farm (conversations and sub-agent runs), newest first.",
     _schema({"claude": S, "n": I})),
    ("farm_session", "farm:read", "One session's whole conversation.", _schema({"id": S}, ["id"])),
    ("farm_inbox", "farm:read", "Messages the farm's Claudes sent to you (this connection). They are marked read "
     "unless peek is true.", _schema({"peek": B})),
    ("farm_schedules", "farm:read", "The scheduled sub-agents (cron, every, at).", _schema({})),
    ("farm_spawn", "farm:work", "Start a sub-agent on the farm: a headless Claude Code run in its own git worktree. "
     "The prompt must be self-contained. Without `on`, whichever Claude has budget runs it; `on` picks one by name "
     "(see farm_status). Its work lands on main only when the farm's check passes. Returns its id: follow it with "
     "farm_result.", _schema({"title": S, "prompt": S, "on": S}, ["title", "prompt"])),
    ("farm_msg", "farm:work", "Send a message to a Claude on the farm by name. It lands in that Claude's next "
     "conversation turn; it can answer you with `clodfarm msg <your name>`, which you read with farm_inbox.",
     _schema({"to": S, "text": S}, ["to", "text"])),
    ("farm_cancel", "farm:work", "Stop a sub-agent.", _schema({"id": S}, ["id"])),
    ("farm_retry", "farm:work", "Start a failed or cancelled sub-agent again.", _schema({"id": S}, ["id"])),
    ("farm_schedule_add", "farm:work", "Start a sub-agent on a schedule. Give exactly one of cron (five fields, in "
     "tz), every (30m, 2h, 1d, 1w) or at (2026-10-01T09:00 in tz, or 'in 3h').",
     _schema({"title": S, "prompt": S, "cron": S, "every": S, "at": S, "tz": S, "on": S}, ["title", "prompt"])),
    ("farm_schedule_remove", "farm:work", "Remove a schedule.", _schema({"id": S}, ["id"])),
    ("farm_dashboards", "farm:read", "The farm's dashboards (pages at /dashboards/<name> that show improvements). "
     "With dashboard (its name): its widgets and the history of its stats.", _schema({"dashboard": S})),
    ("farm_dashboard_push", "farm:work", "Create or replace a dashboard at /dashboards/<dashboard>. spec is "
     "{title, description, widgets: [...]}; widget types: stat {key,label,value,unit,good:'up'|'down',target}, "
     "chart {label,unit,from:[stat keys]} (plots the stats' recorded history) or {label,unit,series:[{name,points:"
     "[[iso time, value]]}]}, bars {label,unit,items:[{label,value}]}, table {label,columns,rows}, progress "
     "{label,value,max}, text {label,text}. Every push records each stat's value, so the page shows how it moved. "
     "folder (optional, nest with '/', e.g. 'Growth/Leads') files it on the list page; left out, it stays where it is. "
     "refresh (optional): instructions for the sub-agent the page's Refresh button starts to collect the data again "
     "and push it (where the numbers come from).",
     _schema({"dashboard": S, "spec": {"type": "object"}, "folder": S, "refresh": S}, ["dashboard", "spec"])),
]
READ_ONLY = {t[0] for t in TOOLS if t[1] == "farm:read"}


class ToolError(Exception):
    pass


def run_tool(ui, grant: dict, name: str, args: dict):
    from .cli import _claudes, _seats, _seats_json
    from .schedule import describe, parse_at, parse_every
    store, cfg, me = ui.store, ui.cfg, grant["name"]
    as_ = grant.get("owner") or me  # a connection its person made acts as their Claude, working from their computer
    names = lambda: {c["name"] for c in _claudes(cfg, store)}  # noqa: E731
    s = lambda k, n=4000: str(args.get(k) or "").strip()[:n]  # noqa: E731
    if name == "farm_status":
        active = store.list_tasks("running") + store.list_tasks("waiting") + store.list_tasks("queued", 20)
        return {"farm": cfg.farm_id, "you": as_, "computer": me, "paused": store.control(), "claudes": _claudes(cfg, store),
                "subagents": [_task(t) for t in active]}
    if name == "farm_budget":
        return {"seats": _seats_json(_seats(cfg, store)), "policy": vars(cfg.policy)}
    if name == "farm_subagents":
        ts = store.list_tasks("running") + store.list_tasks("waiting") + store.list_tasks("queued", 50)
        if args.get("all"):
            ts += store.list_tasks("done", 20) + store.list_tasks("failed", 10) + store.list_tasks("cancelled", 10)
        return [_task(t) for t in ts]
    if name == "farm_result":
        end = time.time() + max(0, min(45, int(args.get("wait_seconds") or 0)))
        t = store.get_task(s("id", 64))
        while t and t["status"] in ("queued", "running", "waiting") and time.time() < end:
            time.sleep(2)
            t = store.get_task(t["id"])
        if not t:
            raise ToolError("no such sub-agent")
        return {**_task(t, full=True), "runs": store.runs(t["id"])}
    if name == "farm_events":
        return [{k: e.get(k) for k in ("at", "type", "msg", "task", "by")}
                for e in store.events(None, max(1, min(200, int(args.get("n") or 30))))]
    if name == "farm_sessions":
        keep = ("id", "claude", "runs_on", "kind", "task", "title", "turns", "started", "last_at", "ended")
        rows = [x for x in store.sessions(s("claude", 64) or None, 500) if x.get("kind") != "usage"]
        return [{k: x.get(k) for k in keep} for x in rows[:max(1, min(100, int(args.get("n") or 20)))]]
    if name == "farm_session":
        x = store.session(s("id", 64))
        if not x:
            raise ToolError("no such session")
        return {"id": x["id"], "claude": x.get("claude"), "kind": x.get("kind"), "title": x.get("title"),
                "conversation": [{k: t.get(k) for k in ("role", "kind", "text", "at")} for t in store.turns(x["id"])]}
    if name == "farm_inbox":
        return [m for a in addresses(grant) for m in store.inbox(a, unread_only=True, mark_read=not args.get("peek"))]
    if name == "farm_schedules":
        return [{**r, "when": describe(r)} for r in store.schedules()]
    # ------------------------------------------------------------- farm:work
    if name == "farm_spawn":
        title, prompt, on = s("title", 200), s("prompt", 100_000), s("on", 64) or None
        if not title or not prompt:
            raise ToolError("title and prompt are required")
        if on and on not in names():
            raise ToolError(f"no Claude named '{on}' is on the farm ({', '.join(sorted(names())) or 'none'})")
        if store.count("queued") >= cfg.max_queue:
            raise ToolError(f"{cfg.max_queue} sub-agents are already waiting; try again later")
        t = store.add_task(title, prompt, created_by=me, to=on, owner=as_, max_depth=cfg.max_depth,
                           max_attempts=cfg.max_attempts)
        store.event("mcp.spawn", f"{me} started sub-agent {t['id']} over MCP: {title[:80]}", task=t["id"], by=me)
        return {"started": t["id"], "title": t["title"], "on": on or "whichever Claude has budget",
                "next": f"farm_result id={t['id']} (wait_seconds up to 45)"}
    if name == "farm_msg":
        to, text = s("to", 64), s("text", 8000)
        if to not in names() | connection_names(cfg.workspace):
            raise ToolError(f"no Claude named '{to}' is on the farm ({', '.join(sorted(names())) or 'none'})")
        if not text:
            raise ToolError("the message is empty")
        store.send_message(as_, to, text)
        return {"sent": to, "note": f"it reads it on its next turn; answers come to '{as_}' (farm_inbox)"}
    if name == "farm_cancel":
        return {"cancelled": store.cancel(s("id", 64))}
    if name == "farm_retry":
        return {"started_again": store.retry(s("id", 64))}
    if name == "farm_schedule_add":
        whens = [k for k in ("cron", "every", "at") if args.get(k)]
        if len(whens) != 1:
            raise ToolError("give exactly one of cron, every or at")
        tz = s("tz", 64) or os.environ.get("FARM_TZ") or "UTC"
        try:
            spec = {"cron": s("cron", 100)} if whens == ["cron"] else {"every": parse_every(s("every", 20))} \
                if whens == ["every"] else {"at": parse_at(s("at", 40), tz)}
            sch = store.add_schedule(s("title", 200), s("prompt", 100_000) or s("title", 200), tz=tz,
                                     to=s("on", 64) or None, created_by=me, owner=as_, **spec)
        except (ValueError, KeyError) as e:
            raise ToolError(f"bad schedule: {e}") from None
        return {**sch, "when": describe(sch)}
    if name == "farm_schedule_remove":
        return {"removed": store.remove_schedule(s("id", 64))}
    if name in ("farm_dashboards", "farm_dashboard_push"):
        from . import dashboards
        pub = os.environ.get("FARM_PUBLIC_URL", "").rstrip("/")
        try:
            if name == "farm_dashboard_push":
                folder = args.get("folder")
                d = dashboards.push(store, s("dashboard", 48), args.get("spec"), by=me, owner=as_,
                                    folder=None if folder is None else str(folder)[:200])
                if s("refresh", 4000) and not (d.get("refresh") or {}).get("cmd"):  # a command (from the CLI) stays
                    d = dashboards.set_refresh(store, d["slug"], agent=s("refresh", 4000), by=me)
                return {"dashboard": d["slug"], "widgets": len(d["widgets"]), "url": f"{pub}/dashboards/{d['slug']}"}
            if s("dashboard", 48):
                d = dashboards.get(store, s("dashboard", 48))
                if not d:
                    raise ToolError("no such dashboard")
                return dashboards.view(store, d)
            return [dashboards.summary(store, d, 7) for d in dashboards.all_(store)]
        except dashboards.SpecError as e:
            raise ToolError(str(e)) from None
    raise ToolError(f"unknown tool {name}")


def _task(t: dict, full: bool = False) -> dict:
    keep = ["id", "title", "status", "parent", "children", "created", "updated", "started", "finished", "attempts",
            "branch", "owner", "to"]
    out = {k: t.get(k) for k in keep if t.get(k) not in (None, "", [])}
    out["on"] = t.get("worker", "").split("@")[0] or t.get("to") or None
    if full:
        out.update(prompt=t.get("prompt", ""), result=t.get("result", ""))
    elif t.get("status") in ("done", "failed"):
        out["summary"] = (t.get("result") or "")[-400:]
    return out


def rpc(ui, grant: dict, msg: dict):
    """One JSON-RPC message. Returns the response object, or None for a notification."""
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    ok = lambda result: {"jsonrpc": "2.0", "id": mid, "result": result}  # noqa: E731
    err = lambda code, text: {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": text}}  # noqa: E731
    if mid is None:  # notifications (initialized, cancelled): nothing to answer
        return None
    scopes = set(grant["scope"].split())
    if method == "initialize":
        want = str(params.get("protocolVersion") or "")
        return ok({"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": "clodfarm", "title": f"clodfarm · {ui.cfg.farm}", "version": __version__},
                   "instructions": (
                       (f"You are the farm Claude '{grant['owner']}', working from its person's own computer "
                        f"('{grant['name']}') on the clodfarm farm '{ui.cfg.farm}': you message, start sub-agents and "
                        f"read your inbox as '{grant['owner']}'. " if grant.get("owner") else
                        f"You are connected to the clodfarm farm '{ui.cfg.farm}' as '{grant['name']}'. ") +
                       "The farm runs "
                       "Claude Code agents around the clock, each Claude on one person's account, paced on its real "
                       "5-hour and weekly usage. Use farm_status first. farm_spawn hands work to the farm (a "
                       "self-contained prompt; it lands on main only when the farm's tests pass) and farm_result "
                       "follows it. farm_msg talks to a Claude by name; answers arrive in farm_inbox.")})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": [{"name": n, "title": n.replace("farm_", "").replace("_", " "), "description": d,
                              "inputSchema": sch, "annotations": {"readOnlyHint": n in READ_ONLY,
                                                                  "destructiveHint": n in ("farm_cancel", "farm_schedule_remove"),
                                                                  "openWorldHint": False}}
                             for n, sc, d, sch in TOOLS if sc in scopes]})
    if method == "tools/call":
        name, args = str(params.get("name") or ""), params.get("arguments") or {}
        tool = next((t for t in TOOLS if t[0] == name), None)
        if not tool:
            return err(-32602, f"unknown tool: {name}")
        if tool[1] not in scopes:
            return ok({"content": [{"type": "text", "text": f"this connection has no {tool[1]} access"}], "isError": True})
        if not isinstance(args, dict):
            return err(-32602, "arguments must be an object")
        try:
            out = run_tool(ui, grant, name, args)
            return ok({"content": [{"type": "text", "text": json.dumps(out, default=str, indent=1)}], "isError": False})
        except ToolError as e:
            return ok({"content": [{"type": "text", "text": str(e)}], "isError": True})
    if method in ("resources/list", "prompts/list"):
        return ok({method.split("/")[0]: []})
    return err(-32601, f"method not found: {method}")


# ============================================================ guest sessions
SID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
GUEST_EVENTS = ("SessionStart", "UserPromptSubmit", "PostToolBatch", "Stop", "SessionEnd", "Connect", "Disconnect",
                "Listen", "Wake")
MAX_GUEST_TURNS = 400  # per report; the computer sends a long conversation in several
LISTEN_MAX = 20  # seconds a Listen report waits for mail (a proxy in front, like CloudFront, gives up at 30)


def _guest_turns(raw) -> list[dict]:
    from .sessions import MAX_TEXT, _clip
    if not isinstance(raw, list) or len(raw) > MAX_GUEST_TURNS:
        raise ValueError(f"turns must be a list of at most {MAX_GUEST_TURNS}")
    out = []
    for t in raw:
        if not isinstance(t, dict) or t.get("role") not in ("user", "assistant") or \
                t.get("kind") not in ("text", "tool", "tool_result") or not isinstance(t.get("text"), str):
            raise ValueError("a turn is {role: user|assistant, kind: text|tool|tool_result, text, at}")
        out.append({"role": t["role"], "kind": t["kind"], "text": _clip(t["text"], MAX_TEXT),
                    "at": str(t["at"])[:40] if t.get("at") else None})
    return out


def _guest_usage(raw) -> dict:
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("usage is {input, output, cache_write, cache_read}")
    out = {}
    for k in ("input", "output", "cache_write", "cache_read"):
        v = raw.get(k, 0)
        if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 10 ** 9:
            raise ValueError("usage counts are whole numbers of tokens")
        out[k] = v
    return out


def addresses(grant: dict) -> list[str]:
    """Where a computer's sessions take their messages: its own name, and its person's Claude (it is that Claude,
    working from the computer: one more of its conversations)."""
    owner = grant.get("owner")
    return [grant["name"]] + ([owner] if owner and owner != grant["name"] else [])


def guest_report(store, grant: dict, body: dict) -> tuple[int, dict]:
    """A Claude Code session on a connected computer reports itself (the `farm` plugin's hook, or `clodfarm attach`'s).

    The session and its new turns go in the farm's store as a ``guest`` session of that computer, kept under the farm
    Claude whose person connected it: it is that Claude working from its person's computer. So it shows on the farm
    while a turn runs, its tokens count for that Claude, it takes that Claude's messages as well as the computer's, and
    the answer carries that Claude's tool settings for the computer to apply. A computer only ever writes its own
    sessions: a session id the farm already holds for anyone else is refused.

    ``Listen`` waits up to LISTEN_MAX seconds for a message and says how many wait, taking none; ``Wake`` then takes
    them (an idle session's listener, which wakes the session with them)."""
    from . import policy
    from .prompts import mail_text
    name, owner = grant["name"], grant.get("owner")
    sid, event = str(body.get("session") or ""), str(body.get("event") or "")
    if not SID_RE.match(sid) or event not in GUEST_EVENTS:
        return 400, {"error": "session (its Claude Code session id) and event are required"}
    try:
        turns, usage = _guest_turns(body.get("turns") or []), _guest_usage(body.get("usage"))
    except ValueError as e:
        return 400, {"error": str(e)}
    had = store.session(sid)
    if had and (had.get("kind") != "guest" or had.get("runs_on") != name):
        return 403, {"error": "that session isn't this computer's"}
    addrs = addresses(grant)
    if event == "Listen":
        end = time.time() + max(0, min(LISTEN_MAX, int(body.get("wait") or 0)))
        while True:
            n = sum(len(store.unread(a)) for a in addrs)
            if n or time.time() >= end:
                return 200, {"ok": True, "waiting": n}
            time.sleep(1)
    ended = event in ("SessionEnd", "Disconnect")
    if event != "Wake":
        title = str(body.get("title") or "")[:120] or None
        store.record_session(sid, turns=turns, claude=owner or name, runs_on=name, kind="guest", box=name,
                             cwd=str(body.get("cwd") or "")[:500] or None, title=title,
                             busy=True if event in ("UserPromptSubmit", "PostToolBatch") else
                             False if event in ("Stop", "SessionEnd", "Disconnect") else None,
                             ended=True if ended else False if event in ("SessionStart", "Connect") else None,
                             end_reason=str(body.get("reason") or event)[:80] if ended else None)
        if not had:
            store.event("session.guest", f"{name} connected a Claude Code session to the farm", by=name)
        if usage and owner:
            store.add_tokens(usage, owner)
    mail = [m for a in addrs for m in store.claim(a, f"{event.lower()}:{name}")] \
        if body.get("mail") and not ended else []
    return 200, {"ok": True, "session": sid, "name": name, "as": owner or name, "turns": len(turns),
                 "policy": policy.clean(store.claude(owner).get("tools")) if owner else {"deny": []},
                 "mail": mail_text(sorted(mail, key=lambda m: float(m.get("at", 0)))) if mail else ""}


# =================================================================== HTTP glue
def public_url(handler, base: str) -> str:
    """The farm's public URL: FARM_PUBLIC_URL (behind a proxy that rewrites Host, like CloudFront), else from Host."""
    env = os.environ.get("FARM_PUBLIC_URL", "").rstrip("/")
    if env:
        return env
    scheme = "https" if (os.environ.get("FARM_UI_SECURE") == "1" or handler.headers.get("X-Forwarded-Proto") == "https") else "http"
    return f"{scheme}://{handler.headers.get('Host') or 'localhost'}{base}"


def metadata(pub: str) -> tuple[dict, dict]:
    prm = {"resource": pub + "/mcp", "authorization_servers": [pub], "scopes_supported": list(SCOPES),
           "bearer_methods_supported": ["header"], "resource_name": "clodfarm"}
    asm = {"issuer": pub, "authorization_endpoint": pub + "/oauth/authorize", "token_endpoint": pub + "/oauth/token",
           "registration_endpoint": pub + "/oauth/register", "revocation_endpoint": pub + "/oauth/revoke",
           "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
           "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["none"],
           "revocation_endpoint_auth_methods_supported": ["none"], "scopes_supported": list(SCOPES),
           "authorization_response_iss_parameter_supported": True}
    return prm, asm


AUTH_PARAMS = ("response_type", "client_id", "redirect_uri", "code_challenge", "code_challenge_method", "state",
               "scope", "resource")


def check_authorize(oa: OAuthStore, q: dict, pub: str) -> tuple[dict | None, str | None, bool]:
    """(params, error, can_redirect). Errors before the redirect URI is trusted are shown on the page, never sent."""
    p = {k: str(q.get(k) or "")[:2000] for k in AUTH_PARAMS}
    c = oa.client(p["client_id"])
    if not c:
        return None, "unknown client: add the farm again in your MCP client", False
    if p["redirect_uri"] not in c["redirect_uris"]:
        return None, "this redirect address was not registered by the client", False
    if p["response_type"] != "code":
        return p, "unsupported_response_type", True
    if p["code_challenge_method"] != "S256" or not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", p["code_challenge"]):
        return p, "invalid_request", True
    if p["resource"] and p["resource"].rstrip("/") not in (pub + "/mcp", pub):
        return p, "invalid_target", True
    wanted = set(p["scope"].split()) if p["scope"] else set(SCOPES)
    if not wanted <= set(SCOPES):
        return p, "invalid_scope", True
    p["scope"] = " ".join(s for s in SCOPES if s in wanted)
    return p, None, True


def redirect_with(uri: str, **params) -> str:
    return uri + ("&" if "?" in uri else "?") + urlencode({k: v for k, v in params.items() if v})


CONSENT_CSS = """
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#4f6b25;font:22px/1.2 VT323,ui-monospace,monospace;color:#1f2a44}
@font-face{font-family:PressStart;src:url(fonts/PressStart2P.ttf)}@font-face{font-family:VT323;src:url(fonts/VT323.ttf)}
main{width:min(560px,calc(100vw - 32px));background:#f4f1e8;border:4px solid #1f2a44;border-radius:10px;padding:26px 28px;box-shadow:0 4px 0 rgba(18,22,12,.35)}
h1{font:14px/1.6 PressStart,monospace;margin:0 0 14px;color:#b45a3c}p{margin:0 0 12px}b{color:#1f2a44}
ul{margin:0 0 14px;padding-left:22px}li{margin:4px 0}.muted{color:#6b6f7c;font-size:19px}
label{display:block;font:10px/1.6 PressStart,monospace;margin:14px 0 6px}
input[type=text],input[type=password]{width:100%;box-sizing:border-box;font:22px VT323,monospace;padding:8px 10px;border:3px solid #1f2a44;border-radius:6px;background:#fff}
.opt{display:flex;gap:10px;align-items:flex-start;margin:8px 0;font-size:20px}.opt input{margin-top:5px}
.row{display:flex;gap:12px;margin-top:20px}button{flex:1;font:11px PressStart,monospace;padding:14px;border:3px solid #1f2a44;border-radius:6px;cursor:pointer;background:#e6e0cf}
button.go{background:#d97757;color:#fff}.err{background:#fde3dd;border:3px solid #c0392b;border-radius:6px;padding:8px 10px;margin-bottom:12px}
"""


def _css(base: str) -> str:
    return CONSENT_CSS.replace("url(fonts/", f"url({base}/fonts/")


def consent_page(p: dict, client: dict, logged_in: bool, form: str, default_name: str, nonce: str, base: str,
                 error: str = "") -> str:
    e = html.escape
    hidden = "".join(f'<input type="hidden" name="{k}" value="{e(p[k])}">' for k in AUTH_PARAMS)
    host = e(urlsplit(p["redirect_uri"]).netloc)
    ro = p["scope"] == "farm:read"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connect to the farm</title><style nonce="{nonce}">{_css(base)}</style></head><body><main>
<h1>CONNECT TO THE FARM</h1>
{f'<div class="err">{e(error)}</div>' if error else ''}
<p><b>{e(client["client_name"])}</b> wants to use this farm (it will return to <b>{host}</b>).</p>
<form method="post" action="authorize">{hidden}<input type="hidden" name="form" value="{e(form)}">
<label for="name">ITS NAME ON THE FARM</label>
<input id="name" name="name" type="text" value="{e(default_name)}" maxlength="32" pattern="[a-z0-9][a-z0-9._-]*" required>
<p class="muted">The Claudes see messages and sub-agents from this name, and answer it with <code>clodfarm msg</code>.</p>
<label>WHAT IT MAY DO</label>
<div class="opt"><input type="radio" id="rw" name="access" value="work"{'' if ro else ' checked'}{' disabled' if ro else ''}><label for="rw" class="muted">See the farm, start sub-agents, message the Claudes and manage schedules</label></div>
<div class="opt"><input type="radio" id="ro" name="access" value="read"{' checked' if ro else ''}><label for="ro" class="muted">Only see the farm</label></div>
<p class="muted">It can never log Claudes in or out, release them or pause the farm. End it any time with <code>clodfarm disconnect</code>.</p>
{'' if logged_in else f'<div class="err">Sign in to your Claude on this farm first: open <a href="{e(base)}/">the farm</a> in this browser and tap MY CLAUDE (ask your Claude for a code: "farm login"). Then connect again.</div>'}
<div class="row"><button name="decision" value="deny" formnovalidate>DENY</button><button class="go" name="decision" value="allow"{'' if logged_in else ' disabled'}>ALLOW</button></div>
</form></main></body></html>"""


def error_page(text: str, nonce: str, base: str) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Farm connection</title>'
            f'<style nonce="{nonce}">{_css(base)}</style></head><body><main><h1>CAN\'T CONNECT</h1>'
            f'<p>{html.escape(text)}</p></main></body></html>')


def token_response(oa: OAuthStore, form: dict, pub: str) -> tuple[int, dict]:
    gt = form.get("grant_type")
    if gt == "authorization_code":
        c = oa.take_code(form.get("code", ""))
        if not c or c["client_id"] != form.get("client_id") or c["redirect_uri"] != form.get("redirect_uri"):
            return 400, {"error": "invalid_grant"}
        verifier = form.get("code_verifier", "")
        if not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier) or \
                not hmac.compare_digest(_b64(hashlib.sha256(verifier.encode()).digest()), c["challenge"]):
            return 400, {"error": "invalid_grant", "error_description": "PKCE check failed"}
        if form.get("resource") and form["resource"].rstrip("/") not in (pub + "/mcp", pub):
            return 400, {"error": "invalid_target"}
        return 200, oa.grant(c["client_id"], c["name"], c["scope"], pub + "/mcp", owner=c.get("owner"))
    if gt == "refresh_token":
        out = oa.refresh(form.get("refresh_token", ""), form.get("client_id", ""))
        return (200, out) if out else (400, {"error": "invalid_grant"})
    return 400, {"error": "unsupported_grant_type"}
