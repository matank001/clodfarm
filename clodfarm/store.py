"""The farm's state: task queue, per-seat budget, slots, control switches, heartbeats and the event log.

All logic is written once here, on the primitives in ``backends.py``: SQLite for one box (the default) and DynamoDB
for a farm spread over several boxes and Claude accounts.

Items (PK / SK):

    TASK#<id>          / META              a task (GSI1PK = STATUS#<status>, GSI1SK = priority, then age)
    TASK#<id>          / RUN#<ts>          one agent run of that task (usage, cost)
    TOOLS              / <claude>          what that Claude can use (tools, MCP servers, skills), from its last run
    BUDGET             / <seat>            newest rate_limit snapshot of that Claude account (seat)
    SLOT               / <seat>#<n>        concurrency slot n of that seat (lease, shared by its boxes)
    SPEND              / <seat>#<day>      API-mode list-price spend per seat and day
    WORKER             / <farm>/<worker>   heartbeat
    SCHEDULE           / <id>              a scheduled sub-agent: started again every time it is due
    MSG#<address>      / <ts>#<rand>       a message to a Claude (its name) or to one sub-agent (its task id)
    BELL               / <claude>          bumped by every message for that Claude's boxes: their mail loop looks
    WAKEQ              / <ts>#<rand>       a --wake message: if nobody has read it when due, the farm starts someone
    WAKES              / <address>#<hour>  wakes per recipient and hour (the cap that stops message loops)
    SESSION            / <session id>      a Claude Code session: which Claude, what kind, its task, title, turns
    TURN#<session id>  / <n>               one turn of its conversation (see sessions.py)
    CONTROL            / GLOBAL | HEALTH
    CONTROL            / SETTINGS          the farm manager's switches: private farm, hatching (see web.py)
    CONTROL            / PLANNER           the planner: on/off, its goal, its Claude, its cadence (see planner.py)
    CONTROL            / BOX#<host>        `clodfarm drain` on that box
    CLAUDE             / <id>              a Claude's settings: skin, tools, approve every mission, owner cookie version
    HELD               / <message id>      a message waiting for its recipient's person to approve it
    STATS              / TOKENS[#day|@claude]  tokens burned (input, output, cache write, cache read)
    PAIR               / <token hash>      a one-time link (or code) that signs a person in to their Claude
    LOGIN              / <username>        a username and password that sign a person in to their Claude (or bot)
    LIVE               / <ts>#<rand>       the live feed: what an agent thought, said or did (feed.py), for a day
    EVENT#<yyyy-mm-dd> / <ts>#<rand>       event log (expires after 30 days)
"""

from __future__ import annotations

import copy
import hmac
import json
import os
import re
import secrets
import time

from .backends import Backend, DynamoBackend, SqliteBackend
from .governor import Snapshot
from .schedule import next_run

EVENT_TTL = 30 * 86400
APPROVAL_TTL = 86400  # a mission nobody approved in a day is denied
DEFAULT_SEAT = "default"  # single-account farms and tests
_RETRIES = 50


def now() -> float:
    return time.time()


def iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts if ts is not None else now()))


def new_id() -> str:
    return time.strftime("%y%m%d%H%M%S", time.gmtime()) + secrets.token_hex(3)  # sortable: time prefix + randomness


TASK_ID = re.compile(r"\d{12}[0-9a-f]{6}")  # what new_id() makes: a message to one is for that sub-agent
CLAUDE_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")


def hmac_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def _prio_key(priority: int, created: float, tid: str) -> str:
    return f"{9 - max(0, min(9, int(priority)))}#{created:017.6f}#{tid}"  # higher priority first; then oldest first


class Store:
    def __init__(self, backend: Backend):
        self.b = backend
        self.echo = False  # the daemon prints every event as a JSON log line; the CLI stays quiet
        self.bot: dict = {}  # a bot's daemon marks its heartbeats with its model (bot, bot_via, bot_takes)

    @classmethod
    def from_config(cls, cfg) -> "Store":
        if cfg.store == "dynamodb":
            return cls(DynamoBackend(cfg.table, cfg.region, cfg.endpoint))
        return cls(SqliteBackend(cfg.db_path))

    # ------------------------------------------------------------ primitives
    def ensure_table(self) -> bool:
        return self.b.ensure()

    def ready(self) -> bool:
        return self.b.ready()

    def describe(self) -> str:
        return self.b.describe()

    def _update(self, pk: str, sk: str, fn, create: bool = False) -> dict | None:
        """Atomically change one item: ``fn(item)`` gets a copy (or {} when absent and ``create``) and returns the new
        item, or None to leave it alone (a failed condition). Retries on concurrent writers."""
        for _ in range(_RETRIES):
            cur = self.b.get(pk, sk)
            if cur is None and not create:
                return None
            new = fn(copy.deepcopy(cur) if cur else {})
            if new is None:
                return None
            ver = int((cur or {}).get("ver", 0))
            new.update(PK=pk, SK=sk, ver=ver + 1)
            if self.b.put(new, expect_ver=ver):
                return new
            time.sleep(0.005)
        raise RuntimeError(f"could not update {pk}/{sk}: too much contention")

    # ----------------------------------------------------------------- tasks
    def _tkey(self, tid):
        return f"TASK#{tid}", "META"

    def add_task(self, title: str, prompt: str, priority: int = 5, parent: str | None = None,
                 kind: str = "task", created_by: str = "human", max_depth: int = 3, max_attempts: int = 3,
                 to: str | None = None, owner: str | None = None) -> dict:
        """A sub-agent run. ``owner`` is the Claude that started it (a sub-agent's own sub-agents inherit it), so the
        farm shows it next to that Claude. ``to`` pins it to one Claude's account (e.g. ``gil``): only its boxes take
        it. Without ``to`` any Claude with budget left runs it."""
        depth = 0
        if parent:
            p = self.get_task(parent)
            if not p:
                raise ValueError(f"parent task {parent} not found")
            depth = int(p.get("depth", 0)) + 1
            owner = p.get("owner") or owner
            if depth > max_depth:
                raise ValueError(f"sub-agent depth {depth} exceeds FARM_MAX_DEPTH={max_depth}; "
                                 "do the work yourself instead of splitting further")
        tid, t = new_id(), now()
        # a Claude whose person approves every mission: work others send it waits for their OK (on their phone)
        gate = self.needs_approval(to, owner, created_by)
        status = "pending" if gate else "queued"
        item = {"PK": f"TASK#{tid}", "SK": "META", "GSI1PK": f"STATUS#{status}",
                "GSI1SK": _prio_key(priority, t, tid) if status == "queued" else f"{t:017.6f}#{tid}",
                "ver": 1, "id": tid, "title": title[:300], "prompt": prompt, "status": status, "priority": priority,
                "kind": kind, "parent": parent, "depth": depth, "created": t, "updated": t, "created_by": created_by,
                "attempts": 0, "max_attempts": max_attempts, "resumes": 0, "children_open": 0, "to": to or None,
                "owner": owner or None}
        if gate:
            item["approval"] = {"asked_at": t, "from": owner or created_by, "expires_at": t + APPROVAL_TTL}
        item = {k: v for k, v in item.items() if v is not None}
        self.b.put(item, expect_ver=0)
        if parent:
            self._update(*self._tkey(parent), lambda x: {**x, "children_open": int(x.get("children_open", 0)) + 1,
                                                          "children": list(x.get("children") or []) + [tid]})
        self.event("task.added", f"{tid} {title[:120]}" + (f" (for {to})" if to else ""), task=tid, by=created_by)
        if gate:
            self.event("approval.asked", f"{tid}: {owner or created_by} asks {to} for \"{title[:100]}\"; waiting for "
                       f"{to}'s person to approve", task=tid, by=created_by)
            self._ask(to, title, owner or created_by, tid)
        return item

    def _ask(self, to: str, what: str, frm: str, pid: str):
        from . import notify
        base = (os.environ.get("FARM_PUBLIC_URL") or os.environ.get("FARM_UI_PUBLIC_URL") or "").rstrip("/")
        notify.ask_approval(self.claude(to).get("notify_topic") or "", to, what[:200], frm,
                            f"{base}/?approve={pid}" if base else "")

    # ------------------------------------------------------------- approvals
    def approving(self, claude: str | None) -> bool:
        return bool(claude) and bool(self.claude(claude).get("approve_missions"))

    def needs_approval(self, to: str | None, owner: str | None, created_by: str | None) -> bool:
        """Work for ``to`` needs its person's OK when they asked for that, unless it comes from that Claude itself (or
        its own sub-agents: they carry it as ``owner``), or from its person (the farm UI or its own conversation)."""
        if not to or not self.approving(to):
            return False
        if owner == to or created_by in (to, f"owner:{to}", f"person:{to}"):
            return False
        return True

    def pending(self, claude: str | None = None) -> list[dict]:
        """Missions and messages waiting for approval (for one Claude's person, or all)."""
        tasks = [t for t in self.b.query_index("STATUS#pending", 500) if not claude or t.get("to") == claude]
        msgs = [m for m in self.b.query("HELD") if not claude or m.get("to") == claude]
        return [{"type": "task", **t} for t in tasks] + [{"type": "message", **m} for m in msgs]

    def approve(self, tid: str, by: str) -> bool:
        """Let a waiting mission run (or a held message through)."""
        if self.b.get("HELD", tid):
            return self._release_message(tid, by)
        ok = self._set_status(tid, "queued", when=lambda x: x.get("status") == "pending", extra={
            "approval": {**((self.get_task(tid) or {}).get("approval") or {}), "decided_by": by, "decided_at": now(),
                         "ok": True}})
        if ok:
            self.event("approval.ok", f"{tid}: approved by {by}", task=tid, by=by)
        return ok

    def deny(self, tid: str, by: str, reason: str = "") -> bool:
        if self.b.get("HELD", tid):
            return self._release_message(tid, by, deliver=False)
        task = self.get_task(tid) or {}
        ok = self._set_status(tid, "denied", when=lambda x: x.get("status") == "pending", extra={
            "approval": {**(task.get("approval") or {}), "decided_by": by, "decided_at": now(), "ok": False},
            "finished": now(), "result": f"not approved by {task.get('to')}'s person" + (f": {reason}" if reason else "")})
        if ok:
            self.event("approval.denied", f"{tid}: denied by {by}" + (f" ({reason})" if reason else ""), task=tid, by=by)
            self._child_finished(tid)
            asker = task.get("parent") or task.get("owner") or task.get("created_by")
            if asker and (TASK_ID.fullmatch(asker) or CLAUDE_NAME.fullmatch(asker)):
                self.send_message(task.get("to") or "farm", asker,
                                  f"Your request \"{task.get('title', '')[:120]}\" ({tid}) to {task.get('to')} was not "
                                  f"approved by its person" + (f" ({reason})" if reason else "") + ". Do it another way.")
        return ok

    def expire_approvals(self) -> int:
        n = 0
        for t in self.b.query_index("STATUS#pending", 500):
            if float((t.get("approval") or {}).get("expires_at", 0)) < now() and self.deny(t["id"], "farm", "expired"):
                n += 1
        for m in self.b.query("HELD"):
            if float(m.get("expires_at_held", 0)) < now():
                self._release_message(m["SK"], "farm", deliver=False)
        return n

    def get_task(self, tid: str) -> dict | None:
        return self.b.get(*self._tkey(tid))

    def list_tasks(self, status: str | None = None, limit: int = 50) -> list[dict]:
        statuses = [status] if status else ["running", "queued", "waiting", "pending", "done", "failed", "cancelled",
                                            "denied"]
        out = []
        for s in statuses:
            out += self.b.query_index(f"STATUS#{s}", limit, desc=s in ("done", "failed", "cancelled", "denied"))
        return out[:limit] if status else out

    def count(self, status: str) -> int:
        return self.b.count_index(f"STATUS#{status}")

    def _set_status(self, tid: str, status: str, when=None, extra: dict | None = None, remove=()) -> bool:
        """Move a task to ``status`` if ``when(task)`` holds (atomically)."""
        def fn(task):
            if when is not None and not when(task):
                return None
            t = now()
            task.update(extra or {})
            for k in remove:
                task.pop(k, None)
            task.update(status=status, GSI1PK=f"STATUS#{status}", updated=t,
                        GSI1SK=(_prio_key(task.get("priority", 5), task["created"], tid) if status == "queued"
                                else f"{t:017.6f}#{tid}"))
            return task
        return self._update(*self._tkey(tid), fn) is not None

    def claim_next(self, worker: str, lease: int, farm: str | None = None, affinity: float = 600,
                   agent: str | None = None, sent_only: bool = False) -> dict | None:
        """Atomically take the highest-priority queued task this box may take.

        A task handed to one Claude (``to``) is only taken by that agent's boxes (``agent`` is the box's farm name).
        With ``sent_only`` (a bot) the box takes only those.

        A task waiting to be *resumed* keeps its conversation on the box it last ran on (``home``). For ``affinity``
        seconds only that box may take it; after that anyone may, starting fresh with the results so far."""
        careful = self.approving(agent)  # its person approves every mission: it takes only its own, or approved work
        for item in self.b.query_index("STATUS#queued", 100):
            tid = item["id"]
            if (item.get("to") or sent_only) and item.get("to") != agent:
                continue
            if careful and not item.get("to") and item.get("owner") != agent:
                continue
            if farm and item.get("resume") and item.get("home") and item["home"] != farm \
                    and now() - float(item.get("updated", 0)) < affinity:
                continue

            def take(x):
                return x.get("status") == "queued"
            ok = self._set_status(tid, "running", when=take,
                                  extra={"worker": worker, "lease_until": now() + lease, "started": now()})
            if ok:
                self._update(*self._tkey(tid), lambda x: {**x, "attempts": int(x.get("attempts", 0)) + 1})
                self.event("task.claimed", f"{tid} by {worker}", task=tid)
                return self.get_task(tid)
        return None

    def _mine(self, worker):
        return lambda x: x.get("worker") == worker and x.get("status") == "running"

    def renew_lease(self, tid: str, worker: str, lease: int) -> bool:
        mine = self._mine(worker)
        return self._update(*self._tkey(tid), lambda x: {**x, "lease_until": now() + lease} if mine(x) else None) is not None

    def update_task(self, tid: str, **fields):
        """Set fields; a field given as None is removed."""
        def fn(x):
            for k, v in fields.items():
                if v is None:
                    x.pop(k, None)
                else:
                    x[k] = v
            x["updated"] = now()
            return x
        self._update(*self._tkey(tid), fn)

    def finish(self, tid: str, worker: str, ok: bool, result: str, max_resumes: int) -> str:
        """Finish a run. Returns the task's new status.

        A parent whose sub-agents are still open goes to ``waiting``; it is re-queued
        (and resumed with their results) when the last one finishes."""
        mine = self._mine(worker)
        fields, drop = {"result": result[-8000:], "finished": now()}, ("lease_until", "worker")
        if not ok:
            task = self.get_task(tid) or {}
            status = "failed" if int(task.get("attempts", 1)) >= int(task.get("max_attempts", 3)) else "queued"
            if not self._set_status(tid, status, when=mine, extra=fields, remove=drop):
                return task.get("status", "unknown")  # not ours any more (finished, reaped or cancelled): no-op
            self.event("task.failed" if status == "failed" else "task.retry", f"{tid}: {result[-200:]}", task=tid)
            if status == "failed":
                self._child_finished(tid)
            return status
        if self._set_status(tid, "waiting", when=lambda x: mine(x) and int(x.get("children_open", 0)) > 0,
                            extra=fields, remove=drop):
            self.event("task.waiting", f"{tid} waits for its sub-agents", task=tid)
            return "waiting"
        task = self.get_task(tid) or {}
        if task.get("pending_review") and int(task.get("resumes", 0)) < max_resumes:
            if not self._set_status(tid, "queued", when=mine, extra={**fields, "resume": True}, remove=drop):
                return task.get("status", "unknown")  # not ours any more (cancelled, reaped): no-op
            self.event("task.resume", f"{tid}: sub-agents finished during the run; resuming", task=tid)
            return "queued"
        if not self._set_status(tid, "done", when=mine, extra=fields, remove=drop):
            return task.get("status", "unknown")
        self.event("task.done", f"{tid} {task.get('title', '')[:120]}", task=tid)
        self._child_finished(tid)
        return "done"

    def _child_finished(self, tid: str):
        task = self.get_task(tid)
        parent = task and task.get("parent")
        if not parent:
            return
        p = self._update(*self._tkey(parent), lambda x: {**x, "children_open": int(x.get("children_open", 0)) - 1,
                                                          "pending_review": True})
        if p and int(p.get("children_open", 0)) <= 0 and p.get("status") == "waiting":
            if self._set_status(parent, "queued", when=lambda x: x.get("status") == "waiting", extra={"resume": True}):
                self.event("task.resume", f"{parent}: all sub-agents finished; resuming", task=parent)

    def requeue_resume(self, tid: str, worker: str, reason: str, note: str, counter: str) -> bool:
        """Put a running task straight back in the queue to be resumed in its own session (to fix a failing
        verify, or to continue after a timeout). The attempt is given back; ``counter`` counts these."""
        mine = self._mine(worker)
        task = self.get_task(tid) or {}
        ok = self._set_status(tid, "queued", when=mine, remove=("lease_until", "worker"), extra={
            "resume": True, "resume_reason": reason, "resume_note": note[-6000:],
            "attempts": max(0, int(task.get("attempts", 1)) - 1), counter: int(task.get(counter, 0)) + 1})
        if ok:
            what = {"verify": "fix the failing check", "timeout": "continue after a timeout",
                    "restart": "continue after its box restarted"}.get(reason, reason)
            self.event(f"task.{reason}", f"{tid}: resuming to {what}", task=tid)
        return ok

    def mark_resumed(self, tid: str):
        def fn(x):
            x.update(pending_review=False, resume=False, resumes=int(x.get("resumes", 0)) + 1)
            x.pop("resume_reason", None)
            x.pop("resume_note", None)
            return x
        self._update(*self._tkey(tid), fn)

    def cancel(self, tid: str) -> bool:
        """Cancel a sub-agent and every sub-agent under it. A running one is stopped by its box within seconds
        (Farm.run_task watches its status) and nothing it did lands."""
        ok = self._set_status(tid, "cancelled", when=lambda x: x.get("status") in ("queued", "waiting", "running",
                                                                                "pending"),
                              remove=("lease_until",))
        if ok:
            self.event("task.cancelled", tid, task=tid)
            self._child_finished(tid)
            for child in (self.get_task(tid) or {}).get("children") or []:
                self.cancel(child)
        return ok

    def retry(self, tid: str) -> bool:
        ok = self._set_status(tid, "queued", when=lambda x: x.get("status") in ("failed", "cancelled", "done"),
                              extra={"attempts": 0})
        if ok:
            self.event("task.retry", f"{tid} re-queued by hand", task=tid)
        return ok

    def reap_expired(self) -> int:
        """Re-queue running tasks whose worker died (lease ran out); drop approvals nobody gave in time."""
        n = 0
        try:
            self.expire_approvals()
        except Exception:  # noqa: BLE001 - never hold up the reaping
            pass
        for task in self.b.query_index("STATUS#running"):
            if float(task.get("lease_until", 0)) >= now():
                continue
            status = "failed" if int(task.get("attempts", 0)) >= int(task.get("max_attempts", 3)) else "queued"

            def expired(x):
                return x.get("status") == "running" and float(x.get("lease_until", 0)) < now()
            if self._set_status(task["id"], status, when=expired, remove=("lease_until", "worker")):
                n += 1
                self.event("task.reaped", f"{task['id']} lease expired (worker {task.get('worker')}); {status}",
                           task=task["id"])
        return n

    def forget_box(self, farm_id: str) -> int:
        """A box (or an agent added in the UI) left for good: hand its running tasks back, free its slots and drop
        its heartbeats, so it stops showing up as working. Returns how many tasks went back to the queue."""
        mine, n = f"{farm_id}/", 0
        for t in self.b.query_index("STATUS#running"):
            if not str(t.get("worker", "")).startswith(mine):
                continue
            # its conversation lived in that login: another Claude restarts it fresh, from the work on its branch
            if self._set_status(t["id"], "queued", when=self._mine(t["worker"]),
                                remove=("lease_until", "worker", "home", "session_id"),
                                extra={"resume": True, "resume_reason": "restart",
                                       "attempts": max(0, int(t.get("attempts", 1)) - 1)}):
                self.event("task.restart", f"{t['id']}: its Claude left the farm; back in the queue", task=t["id"])
                n += 1
        for s in self.b.query("SLOT"):
            if str(s.get("holder", "")).startswith(mine):
                self.release_slot(s["SK"], s["holder"])
        for w in self.b.query("WORKER", sk_prefix=mine):
            self.b.delete("WORKER", w["SK"])
        return n

    # -------------------------------------------------------------- messages
    def send_message(self, frm: str, to: str, text: str, reply: str | None = None, urgent: bool = False,
                     wake: bool = False, hops: int = 0, wake_after: float = 120, person: bool = False) -> dict:
        """Leave a message for ``to``: a Claude (its name; its conversations read it) or one sub-agent (its task id).
        ``reply`` is where an answer goes (the sender's task id inside a sub-agent). ``urgent`` interrupts a running
        sub-agent. ``wake`` makes sure someone handles it: if it is still unread after ``wake_after`` seconds, the
        farm starts a sub-agent for that Claude, or resumes that finished sub-agent (see ``dispatch_wakes``).
        ``hops`` counts how many message-triggered runs led to this one. ``person``: it comes from the person the
        recipient sub-agent works for (their own conversation), so it is their instruction, not another Claude's."""
        t = now()
        item = {"PK": f"MSG#{to}", "SK": f"{t:017.6f}#{secrets.token_hex(2)}", "ver": 1, "id": new_id(), "from": frm,
                "to": to, "reply": reply or frm, "text": text[:4000], "at": t, "read": False, "hops": int(hops),
                "expires_at": int(t + EVENT_TTL)}
        if urgent:
            item["urgent"] = True
        if wake:
            item["wake"] = True
        if person:
            item["person"] = True
        if not person and self._hold(frm, to):
            # for a Claude whose person approves every mission: kept aside until they do (no bell, no wake)
            held = {**item, "PK": "HELD", "SK": item["id"], "wake_after": wake_after,
                    "expires_at_held": t + APPROVAL_TTL}
            self.b.put(held)
            self.event("approval.asked", f"message {item['id']} from {frm} to {to} waits for {to}'s person: "
                       f"{text[:120]}", by=frm)
            self._ask(to, "a message: " + text[:160], frm, item["id"])
            return {**item, "held": True}
        self.b.put(item)
        self.ring(to)
        if wake:
            self.b.put({"PK": "WAKEQ", "SK": f"{t + wake_after:017.6f}#{secrets.token_hex(2)}", "ver": 1,
                        "msg_pk": item["PK"], "msg_sk": item["SK"], "to": to, "due": t + wake_after,
                        "expires_at": int(t + EVENT_TTL)})
        flags = " (urgent)" if urgent else " (wake)" if wake else ""
        self.event("msg.sent", f"{frm} -> {to}: {text[:200]}{flags}", by=frm)
        try:  # the live feed shows who talks to whom
            self.live_add([{"kind": "msg", "text": " ".join(text.split())[:500], "to": to}], claude=frm)
        except Exception:  # noqa: BLE001 - a message is sent whether or not the feed takes it
            pass
        return item

    def _hold(self, frm: str, to: str) -> bool:
        if TASK_ID.fullmatch(to) or not self.approving(to) or frm == to:
            return False
        sender = self.get_task(frm) if TASK_ID.fullmatch(frm or "") else None
        return not (sender and sender.get("owner") == to)  # its own sub-agents may write to it

    def _release_message(self, mid: str, by: str, deliver: bool = True) -> bool:
        it = self.b.get("HELD", mid)
        if not it or not self.b.delete("HELD", mid, expect_ver=int(it.get("ver", 0))):
            return False
        if not deliver:
            self.event("approval.denied", f"message {mid} from {it.get('from')} to {it.get('to')} not delivered "
                       f"({by})", by=by)
            return True
        msg = {k: v for k, v in it.items() if k not in ("wake_after", "expires_at_held")}
        msg.update(PK=f"MSG#{it['to']}", SK=f"{now():017.6f}#{secrets.token_hex(2)}", ver=1)
        self.b.put(msg)
        self.ring(it["to"])
        if it.get("wake"):
            t = now()
            self.b.put({"PK": "WAKEQ", "SK": f"{t + float(it.get('wake_after', 120)):017.6f}#{secrets.token_hex(2)}",
                        "ver": 1, "msg_pk": msg["PK"], "msg_sk": msg["SK"], "to": it["to"],
                        "due": t + float(it.get("wake_after", 120)), "expires_at": int(t + EVENT_TTL)})
        self.event("approval.ok", f"message {mid} from {it.get('from')} to {it.get('to')} approved by {by}", by=by)
        return True

    def ring(self, to: str):
        """Tell the boxes of whoever should read a message for ``to`` to look now: that Claude's, or the box running
        that sub-agent. A sub-agent that isn't running gets it in its prompt when it next starts."""
        name = to
        if TASK_ID.fullmatch(to):
            task = self.get_task(to) or {}
            if task.get("status") != "running" or "@" not in str(task.get("worker", "")):
                return
            name = task["worker"].split("@")[0]
        self._update("BELL", name, lambda x: {**x, "n": int(x.get("n", 0)) + 1, "at": now()}, create=True)

    def bell(self, name: str) -> int:
        it = self.b.get("BELL", name)
        return int(it.get("n", 0)) if it else 0

    def unread(self, addr: str, urgent_only: bool = False) -> list[dict]:
        return [m for m in self.b.query(f"MSG#{addr}") if not m.get("read") and (m.get("urgent") or not urgent_only)]

    def claim(self, addr: str, by: str, urgent_only: bool = False) -> list[dict]:
        """Take the unread messages for ``addr`` exactly once: when two readers race (two hooks, two boxes), each
        message goes to one of them. Returns the ones this call won."""
        out = []
        for m in self.unread(addr, urgent_only):
            won = self._update(m["PK"], m["SK"], lambda x: None if x.get("read") else
                               {**x, "read": True, "read_by": by, "read_at": now()})
            if won:
                out.append(won)
        if out:
            self.event("msg.delivered", f"{len(out)} message(s) for {addr} via {by}", by=by)
        return out

    def unclaim(self, msgs: list[dict]):
        """Give claimed messages back (they could not be handed over)."""
        for m in msgs:
            self._update(m["PK"], m["SK"], lambda x: {**{k: v for k, v in x.items() if k not in ("read_by", "read_at")},
                                                      "read": False})

    def inbox(self, name: str, unread_only: bool = True, mark_read: bool = True) -> list[dict]:
        if unread_only and mark_read:
            return self.claim(name, "inbox")
        return [m for m in self.b.query(f"MSG#{name}") if not (unread_only and m.get("read"))]

    def dispatch_wakes(self, max_hops: int = 3, per_hour: int = 6, max_task_wakes: int = 3) -> list[str]:
        """Handle every --wake message nobody read in time, on any box (each is taken atomically). A message for a
        Claude starts one "mail" sub-agent on its account (one at a time: a queued one picks up all its mail); one for
        a finished sub-agent resumes it in its own session. Never past ``max_hops`` message-triggered runs in a row or
        ``per_hour`` wakes per recipient: two Claudes can't keep each other busy. Returns what was started."""
        out = []
        for w in self.b.query("WAKEQ"):
            if float(w.get("due", 0)) > now() or not self.b.delete("WAKEQ", w["SK"], expect_ver=int(w.get("ver", 0))):
                continue
            m = self.b.get(w["msg_pk"], w["msg_sk"])
            if not m or m.get("read"):
                continue
            to = m["to"]
            if int(m.get("hops", 0)) >= max_hops:
                self.event("msg.nowake", f"{to}: not woken, {m.get('hops')} message-triggered runs in a row")
                continue
            if TASK_ID.fullmatch(to):
                task = self.get_task(to) or {}
                if task.get("status") not in ("done", "failed") or not task.get("session_id"):
                    continue  # queued, running or waiting: it reads the message when it runs
                if not self._count_wake(to, per_hour):
                    continue
                if self._set_status(to, "queued", when=lambda x: x.get("status") in ("done", "failed")
                                    and int(x.get("message_resumes", 0)) < max_task_wakes,
                                    extra={"resume": True, "resume_reason": "message", "attempts": 0,
                                           "message_resumes": int(task.get("message_resumes", 0)) + 1}):
                    self.event("task.message", f"{to}: resuming to read a message from {m['from']}", task=to)
                    out.append(to)
                continue
            if any(t.get("kind") == "mail" and t.get("to") == to for t in self.list_tasks("queued", 100)):
                continue  # one is already waiting to start: it takes this message too
            if not self._count_wake(to, per_hour):
                continue
            t = self.add_task(f"Messages for {to}", "", kind="mail", to=to, owner=to, priority=6,
                              created_by=f"msg:{m['from']}")
            out.append(t["id"])
        return out

    def _count_wake(self, to: str, per_hour: int) -> bool:
        hour = time.strftime("%Y%m%d%H", time.gmtime())
        ok = self._update("WAKES", f"{to}#{hour}", lambda x: None if int(x.get("n", 0)) >= per_hour else
                          {**x, "n": int(x.get("n", 0)) + 1, "expires_at": int(now() + 86400)}, create=True)
        if not ok:
            self.event("msg.nowake", f"{to}: not woken, already {per_hour} wake(s) this hour")
        return ok is not None

    # -------------------------------------------------------------- sessions
    def record_session(self, sid: str, transcript: str | None = None, turns: list[dict] | None = None,
                       **fields) -> dict | None:
        """Register (or update) a session and append its conversation: the ``transcript`` lines written since the last
        call (tracked by byte offset, claimed atomically so two hooks never copy a turn twice) and/or ``turns``."""
        from .sessions import read_transcript, usage_total
        t, got = now(), {}

        def fn(x):
            if not x:
                x = {"id": sid, "started": t, "turns": 0, "offset": 0}
            new = list(turns or [])
            if transcript:
                read, off, meta = read_transcript(transcript, int(x.get("offset", 0)))
                new += read
                x.update(offset=off, transcript=transcript)
                per = meta.pop("usage", None) or {}
                if per:  # tokens it used since the last call (a message split over two reads counts once)
                    got["usage"] = usage_total(per, skip=x.get("usage_last") or "")
                    x["usage_last"] = list(per)[-1]
                x.update({k: v for k, v in meta.items() if v})
            x.update({k: v for k, v in fields.items() if v is not None}, last_at=t)
            if not x.get("title"):
                first = next((n["text"] for n in new if n["role"] == "user" and n["kind"] == "text"), "")
                if first:
                    x["title"] = first.strip().splitlines()[0][:120]
            got["first"], got["turns"] = int(x.get("turns", 0)), new
            x["turns"] = got["first"] + len(new)
            return x
        it = self._update("SESSION", sid, fn, create=True)
        for n, turn in enumerate(got["turns"]):
            self.b.put({"PK": f"TURN#{sid}", "SK": f"{got['first'] + n:06d}", "ver": 1,
                        **{k: v for k, v in turn.items() if v is not None}})
        # a conversation's tokens count here; a sub-agent's are counted once from its run's result (supervisor)
        if got.get("usage") and it and it.get("kind") == "conversation":
            self.add_tokens(got["usage"], it.get("claude"))
        return it

    def sessions(self, claude: str | None = None, limit: int = 100) -> list[dict]:
        out = sorted(self.b.query("SESSION"), key=lambda x: -float(x.get("last_at", 0)))
        return [s for s in out if not claude or s.get("claude") == claude][:limit]

    def session(self, sid: str) -> dict | None:
        return self.b.get("SESSION", sid)

    def turns(self, sid: str) -> list[dict]:
        return self.b.query(f"TURN#{sid}")

    # ------------------------------------------------------------- schedules
    def add_schedule(self, title: str, prompt: str, *, cron: str | None = None, every: int | None = None,
                     at: float | None = None, tz: str = "UTC", to: str | None = None, priority: int = 5,
                     created_by: str = "human", owner: str | None = None) -> dict:
        """Queue ``title`` on a schedule: a cron line (in ``tz``), every N seconds, or once ``at`` a time."""
        if sum(x is not None for x in (cron, every, at)) != 1:
            raise ValueError("give exactly one of cron, every or at")
        spec = {"cron": cron, "every": every, "at": at, "tz": tz}
        first = next_run(spec, now())
        if first is None:
            raise ValueError("that schedule never runs")
        sid = "s" + new_id()
        item = {"PK": "SCHEDULE", "SK": sid, "ver": 1, "id": sid, "title": title[:300], "prompt": prompt,
                "priority": priority, "created_by": created_by, "created": now(), "next_at": first, "runs": 0,
                **{k: v for k, v in {**spec, "to": to or None, "owner": owner or None}.items() if v is not None}}
        self.b.put(item, expect_ver=0)
        self.event("schedule.added", f"{sid} {title[:120]}", by=created_by)
        return item

    def schedules(self) -> list[dict]:
        return sorted(self.b.query("SCHEDULE"), key=lambda x: float(x.get("next_at", 0)))

    def pause_schedule(self, sid: str, paused: bool, by: str | None = None) -> dict | None:
        """Stop a schedule from firing, or let it fire again. A resumed one runs next at its next time from now, not
        at the times it missed (a one-off whose time went by while it was paused runs now)."""
        def fn(x):
            if bool(x.get("paused")) == paused:
                return None
            if paused:
                x["paused"] = True
            else:
                x.pop("paused", None)
                nxt = next_run({**x, "next_at": None} if x.get("every") else x, now())
                x["next_at"] = nxt if nxt is not None else now()
            return x
        it = self._update("SCHEDULE", sid, fn)
        if it:
            self.event("schedule.paused" if paused else "schedule.resumed", f"{sid} {it.get('title', '')[:120]}", by=by)
        return it

    def run_schedule(self, sid: str, max_depth: int = 3, max_attempts: int = 3, by: str | None = None) -> dict | None:
        """Start a schedule's sub-agent now, once, by hand; its next time stays as it was."""
        def fn(x):
            x.update(runs=int(x.get("runs", 0)) + 1, last_at=now())
            return x
        it = self._update("SCHEDULE", sid, fn)
        if not it:
            return None
        self.event("schedule.run", f"{sid} {it.get('title', '')[:120]}: started by hand", by=by)
        return self.add_task(it["title"], it["prompt"], priority=int(it.get("priority", 5)),
                             created_by=f"schedule:{it['id']}", max_depth=max_depth, max_attempts=max_attempts,
                             to=it.get("to"), owner=it.get("owner"))

    def remove_schedule(self, sid: str) -> bool:
        it = self.b.get("SCHEDULE", sid)
        if not it:
            return False
        self.b.delete("SCHEDULE", sid)
        self.event("schedule.removed", f"{sid} {it.get('title', '')[:120]}")
        return True

    def fire_due(self, max_depth: int = 3, max_attempts: int = 3) -> list[dict]:
        """Queue every schedule that is due. Safe on every box at once: each firing is claimed atomically."""
        out, t = [], now()
        for sch in self.b.query("SCHEDULE"):
            if sch.get("paused") or float(sch.get("next_at", 0)) > t:
                continue
            due = float(sch["next_at"])

            def advance(x, due=due):
                if float(x.get("next_at", 0)) != due:
                    return None  # another box fired it
                nxt = None if x.get("at") is not None else next_run(x, max(t, due))  # a one-off fires once
                x.update(next_at=nxt if nxt is not None else -1, runs=int(x.get("runs", 0)) + 1, last_at=t)
                return x
            it = self._update("SCHEDULE", sch["SK"], advance)
            if not it:
                continue
            task = self.add_task(it["title"], it["prompt"], priority=int(it.get("priority", 5)),
                                 created_by=f"schedule:{it['id']}", max_depth=max_depth,
                                 max_attempts=max_attempts, to=it.get("to"), owner=it.get("owner"))
            out.append(task)
            if float(it["next_at"]) < 0:  # a one-off: done
                self.b.delete("SCHEDULE", it["SK"])
        return out

    def add_run(self, tid: str, run: dict):
        self.b.put({"PK": f"TASK#{tid}", "SK": f"RUN#{iso()}#{secrets.token_hex(2)}", "ver": 1,
                    **{k: v for k, v in run.items() if v is not None}})

    def runs(self, tid: str) -> list[dict]:
        return self.b.query(f"TASK#{tid}", sk_prefix="RUN#")

    # ---------------------------------------------------------------- budget
    # Everything budget-related is per seat (one Claude account): snapshots, slots, spend. The queue is shared.
    # ----------------------------------------------------------------- tools
    def put_tools(self, claude: str, init: dict, where: str = "task", keep_newer: float = 0):
        """What a Claude can use, from Claude Code's init event: built-in and MCP tools, MCP servers and their
        status, skills, plugins and sub-agent types. ``keep_newer``: leave a record younger than that many seconds
        (the usage check runs outside the repo, so it doesn't see the repo's own MCP servers and skills)."""
        if not init or not isinstance(init.get("tools"), list):
            return
        t = now()
        rec = {"claude": claude, "at": t, "where": where, "model": init.get("model"),
               "version": init.get("claude_code_version"), "permission_mode": init.get("permissionMode"),
               "tools": [str(x)[:120] for x in init["tools"][:400]],
               "mcp_servers": [{k: str(m.get(k) or "")[:120] for k in ("name", "status", "source")}
                               for m in (init.get("mcp_servers") or [])[:60] if isinstance(m, dict)],
               "skills": [str(x)[:120] for x in (init.get("skills") or [])[:200]],
               "agents": [str(x)[:120] for x in (init.get("agents") or [])[:60]],
               "plugins": [{k: str(p.get(k) or "")[:120] for k in ("name", "version")}  # never the paths
                           for p in (init.get("plugins") or [])[:60] if isinstance(p, dict)]}
        self._update("TOOLS", claude, lambda x: None if keep_newer and float(x.get("at", 0)) > t - keep_newer
                     else dict(rec), create=True)

    def tools(self) -> dict[str, dict]:
        return {i["SK"]: i for i in self.b.query("TOOLS")}

    def put_snapshot(self, snap: Snapshot, seat: str = DEFAULT_SEAT):
        """Keep only the newest observation per seat (several workers report at once)."""
        new = {"seat": seat, **snap.to_dict()}
        self._update("BUDGET", seat, lambda x: dict(new) if float(x.get("observed_at", -1)) < snap.observed_at else None,
                     create=True)

    def get_snapshot(self, seat: str = DEFAULT_SEAT) -> Snapshot | None:
        it = self.b.get("BUDGET", seat)
        return Snapshot.from_dict(it) if it else None

    def snapshots(self) -> dict[str, Snapshot]:
        return {i["SK"]: Snapshot.from_dict(i) for i in self.b.query("BUDGET")}

    def add_spend(self, usd: float, seat: str = DEFAULT_SEAT, ts: float | None = None):
        """Running total of list-price spend per seat and UTC day (the API-mode daily cap reads it)."""
        if usd <= 0:
            return
        day = time.strftime("%Y-%m-%d", time.gmtime(ts if ts is not None else now()))
        self._update("SPEND", f"{seat}#{day}", lambda x: {**x, "usd": float(x.get("usd", 0)) + float(usd),
                                                          "expires_at": int(now() + 90 * 86400)}, create=True)

    def spent_today(self, seat: str = DEFAULT_SEAT) -> float:
        it = self.b.get("SPEND", f"{seat}#{time.strftime('%Y-%m-%d', time.gmtime())}")
        return float(it["usd"]) if it else 0.0

    def acquire_slot(self, holder: str, allowed: int, lease: int, seat: str = DEFAULT_SEAT) -> str | None:
        """Take one of ``allowed`` concurrency slots of this seat, shared by every box logged in to it.
        Returns the slot key (renew and release with it)."""
        for n in range(allowed):
            key = f"{seat}#{n:03d}"

            def take(x):
                busy = x.get("holder") not in (None, holder) and float(x.get("lease_until", 0)) >= now()
                return None if busy else {"seat": seat, "holder": holder, "lease_until": now() + lease}
            if self._update("SLOT", key, take, create=True):
                return key
        return None

    def renew_slot(self, key: str, holder: str, lease: int) -> bool:
        return self._update("SLOT", key, lambda x: {**x, "lease_until": now() + lease}
                            if x.get("holder") == holder else None) is not None

    def release_slot(self, key: str, holder: str):
        it = self.b.get("SLOT", key)
        if it and it.get("holder") == holder:
            self.b.delete("SLOT", key, expect_ver=int(it.get("ver", 0)))

    def slots(self, seat: str | None = None) -> list[dict]:
        return [i for i in self.b.query("SLOT", sk_prefix=f"{seat}#" if seat else None)
                if float(i.get("lease_until", 0)) > now()]

    # ---------------------------------------------------------------- claudes
    def claude(self, cid: str) -> dict:
        """A Claude's settings, shared by every box: its look (skin), what it may use, whether its person approves
        every mission sent to it, and its owner cookie's version (bumped to sign its person out everywhere)."""
        return (self.b.get("CLAUDE", cid) or {}) if cid else {}

    def claudes(self) -> list[dict]:
        return self.b.query("CLAUDE")

    def put_claude(self, cid: str, **fields) -> dict:
        def fn(x):
            if not x:
                x = {"id": cid, "created": now(), "owner_ver": 1}
            x.update({k: v for k, v in fields.items() if v is not None})
            x["updated"] = now()
            return x
        return self._update("CLAUDE", cid, fn, create=True)

    def forget_claude(self, cid: str):
        self.drop_logins(cid)
        it = self.b.get("CLAUDE", cid)
        if it:
            self.b.delete("CLAUDE", cid, expect_ver=int(it.get("ver", 0)))

    # --------------------------------------------------------------- the live feed
    LIVE_TTL = 86400

    def live_add(self, items: list[dict], claude: str | None = None, task: str | None = None,
                 owner: str | None = None):
        """What an agent thought, said or did (feed.py), for the live feed. Kept for a day."""
        t = now()
        for i, it in enumerate(items):
            self.b.put({"PK": "LIVE", "SK": f"{t:017.6f}#{i:03d}{secrets.token_hex(2)}", "ver": 1,
                        **{k: v for k, v in it.items() if v not in (None, "")},
                        "claude": claude, "task": task, "owner": owner, "at": t, "expires_at": int(t + self.LIVE_TTL)})

    def live(self, since: str | None = None, limit: int = 200) -> list[dict]:
        """The feed after cursor ``since`` (an item's SK), oldest first; the newest ``limit`` when there's none."""
        if since:
            return self.b.query("LIVE", sk_gt=since, limit=limit)
        start = f"{now() - self.LIVE_TTL:017.6f}"
        return self.b.query("LIVE", sk_gt=start)[-limit:]

    def spend_rows(self, days: int = 14) -> list[dict]:
        """Every seat's list-price spend per day, for the last ``days`` days."""
        first = time.strftime("%Y-%m-%d", time.gmtime(now() - days * 86400))
        out = []
        for it in self.b.query("SPEND"):
            seat, _, day = it["SK"].rpartition("#")
            if day >= first:
                out.append({"seat": seat, "day": day, "usd": round(float(it.get("usd") or 0), 6)})
        return out

    # --------------------------------------------------------------- logins
    def login(self, username: str) -> dict | None:
        """The username and password that sign a person in to their Claude: its salt and hash, and which Claude."""
        return self.b.get("LOGIN", username) if username else None

    def login_of(self, cid: str) -> dict | None:
        return next((it for it in self.b.query("LOGIN") if it.get("claude") == cid), None)

    def put_login(self, username: str, cid: str, salt: str, hashed: str) -> bool:
        """A Claude's username and password (one per Claude: an old one goes). False when the username is someone
        else's."""
        old = self.b.get("LOGIN", username)
        if old and old.get("claude") != cid:
            return False
        for it in self.b.query("LOGIN"):
            if it.get("claude") == cid and it["SK"] != username:
                self.b.delete("LOGIN", it["SK"])
        item = {"PK": "LOGIN", "SK": username, "ver": int((old or {}).get("ver", 0)) + 1, "claude": cid,
                "salt": salt, "hash": hashed, "at": now()}
        return self.b.put(item, expect_ver=int((old or {}).get("ver", 0)))

    def drop_logins(self, cid: str):
        for it in self.b.query("LOGIN"):
            if it.get("claude") == cid:
                self.b.delete("LOGIN", it["SK"])

    # --------------------------------------------------------------- settings
    SETTINGS = {"private": False, "hatch_open": True, "max_claudes": 100, "hatch_per_ip_hour": 3, "mcp": True,
                "broadcast": False}

    def settings(self) -> dict:
        """The farm manager's switches: a private farm (its Claudes' people only), hatching open or not, its limits,
        and whether computers may connect over MCP."""
        it = self.b.get("CONTROL", "SETTINGS") or {}
        return {**self.SETTINGS, **{k: v for k, v in it.items() if k not in ("PK", "SK", "ver")}}

    def set_settings(self, **fields) -> dict:
        return self._update("CONTROL", "SETTINGS", lambda x: {**x, **fields, "at": now()}, create=True)

    # ---------------------------------------------------------------- planner
    def planner(self) -> dict:
        it = self.b.get("CONTROL", "PLANNER") or {}
        return {"on": False, "goal": "", "host": "", "every_s": 900, "cycles": 0,
                **{k: v for k, v in it.items() if k not in ("PK", "SK", "ver")}}

    def set_planner(self, **fields) -> dict:
        return self._update("CONTROL", "PLANNER", lambda x: {**x, **{k: v for k, v in fields.items()}, "at": now()},
                            create=True)

    def planner_claim(self, holder: str, lease: float) -> bool:
        """One farm daemon at a time drives the planner (across boxes)."""
        return self._update("CONTROL", "PLANNER", lambda x: None if x.get("holder") not in (None, holder)
                            and float(x.get("lease_until", 0)) > now() else
                            {**x, "holder": holder, "lease_until": now() + lease}, create=True) is not None

    # ----------------------------------------------------------------- tokens
    TOKEN_KINDS = ("input", "output", "cache_write", "cache_read")

    @staticmethod
    def token_counts(usage: dict | None) -> dict:
        u = usage or {}
        return {"input": int(u.get("input_tokens") or 0), "output": int(u.get("output_tokens") or 0),
                "cache_write": int(u.get("cache_creation_input_tokens") or 0),
                "cache_read": int(u.get("cache_read_input_tokens") or 0)}

    def add_tokens(self, counts: dict, claude: str | None = None):
        """Tokens burned, farm-wide, per day and per Claude (for the counter on the farm)."""
        counts = {k: int(counts.get(k) or 0) for k in self.TOKEN_KINDS}
        n = sum(counts.values())
        if n <= 0:
            return
        day = time.strftime("%Y-%m-%d", time.gmtime())

        def add(x):
            for k, v in counts.items():
                x[k] = int(x.get(k, 0)) + v
            x["total"] = int(x.get("total", 0)) + n
            x["at"] = now()
            return x
        for sk in ["TOKENS", f"TOKENS#{day}"] + ([f"TOKENS@{claude}", f"TOKENS#{day}@{claude}"] if claude else []):
            try:
                self._update("STATS", sk, add, create=True)
            except RuntimeError:  # never let the counter hold up the work
                pass

    def tokens_today_by(self) -> dict:
        """Per Claude, today (from the per-day, per-Claude counter)."""
        day = time.strftime("%Y-%m-%d", time.gmtime())
        return {i["SK"].split("@", 1)[1]: {k: int(i.get(k, 0)) for k in (*self.TOKEN_KINDS, "total")}
                for i in self.b.query("STATS", sk_prefix=f"TOKENS#{day}@")}

    def tokens(self) -> dict:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        strip = lambda it: {k: int(it.get(k, 0)) for k in (*self.TOKEN_KINDS, "total")}  # noqa: E731
        by = {i["SK"][7:]: strip(i) for i in self.b.query("STATS", sk_prefix="TOKENS@")}
        return {"total": strip(self.b.get("STATS", "TOKENS") or {}), "today": strip(self.b.get("STATS", f"TOKENS#{day}") or {}),
                "by_claude": by, "at": now()}

    # ---------------------------------------------------------------- pairing
    def add_pairing(self, claude: str, token_hash: str, code: str, ttl: float = 600):
        """The link and the code are each good once, apart: tapping the link in the Claude app (its own browser) or a
        link preview must not use up the code the person then types on the farm's page."""
        t = now()
        for sk, extra in ((token_hash, {}), (f"{token_hash}#code", {"code": code})):
            self.b.put({"PK": "PAIR", "SK": sk, "ver": 1, "claude": claude, "at": t, "until": t + ttl,
                        "expires_at": int(t + ttl + 3600), **extra})

    # ---------------------------------------------------------------- invites
    def add_invite(self, token_hash: str, by: str, ttl: float = 7 * 86400):
        """A link the manager sends someone: it lets them hatch a Claude of their own here, once."""
        t = now()
        self.b.put({"PK": "INVITE", "SK": token_hash, "ver": 1, "by": by, "at": t, "until": t + ttl,
                    "expires_at": int(t + ttl + 3600)})

    def invite(self, token_hash: str) -> dict | None:
        it = self.b.get("INVITE", token_hash)
        return it if it and float(it.get("until", 0)) >= now() else None

    def take_invite(self, token_hash: str) -> bool:
        """Use an invite: True the one time it works."""
        it = self.invite(token_hash)
        return bool(it) and self.b.delete("INVITE", token_hash, expect_ver=int(it.get("ver", 0)))

    def use_sso(self, nonce: str, until: float) -> bool:
        """Spend a sign-in link's nonce (see sso.py): True the first time, False ever after (until it has expired)."""
        return self.b.put({"PK": "SSO", "SK": nonce, "ver": 1, "at": now(), "expires_at": int(until) + 3600},
                          expect_ver=0)

    def take_pairing(self, token_hash: str | None = None, code: str | None = None) -> str | None:
        """Use a pairing link (its token's hash) or code once: returns the Claude it signs in to."""
        items = [self.b.get("PAIR", token_hash)] if token_hash else \
            [i for i in self.b.query("PAIR") if code and hmac_eq(str(i.get("code", "")), code.strip().upper())]
        for it in items:
            if not it or float(it.get("until", 0)) < now():
                continue
            if self.b.delete("PAIR", it["SK"], expect_ver=int(it.get("ver", 0))):
                return it.get("claude")
        return None

    # --------------------------------------------------------------- control
    def box_control(self, host: str) -> dict:
        """`clodfarm drain` on one box (host): {draining, exit, by, at}."""
        return self.b.get("CONTROL", f"BOX#{host}") or {}

    def set_draining(self, host: str, draining: bool, exit_: bool = False, by: str = "human"):
        if not draining:
            it = self.b.get("CONTROL", f"BOX#{host}")
            if it:
                self.b.delete("CONTROL", f"BOX#{host}", expect_ver=int(it.get("ver", 0)))
            return
        self._update("CONTROL", f"BOX#{host}", lambda x: {"draining": True, "exit": exit_, "by": by, "at": now()},
                     create=True)

    def set_paused(self, paused: bool, reason: str = "", by: str = "human"):
        self._update("CONTROL", "GLOBAL", lambda x: {"paused": paused, "reason": reason, "by": by, "at": now()},
                     create=True)
        self.event("farm.paused" if paused else "farm.resumed", reason or "", by=by)
        if not paused:
            self.record_health(True)

    def control(self) -> dict:
        return self.b.get("CONTROL", "GLOBAL") or {}

    def record_health(self, ok: bool) -> int:
        """Consecutive failed runs across all boxes (the circuit breaker's input). Returns the new count."""
        it = self._update("CONTROL", "HEALTH", lambda x: {"failures": 0 if ok else int(x.get("failures", 0)) + 1},
                          create=True)
        return int(it["failures"])

    # --------------------------------------------------------- heartbeats/log
    def heartbeat(self, farm: str, worker: str, state: str, task: str | None = None, seat: str | None = None):
        self.b.put({k: v for k, v in {"PK": "WORKER", "SK": f"{farm}/{worker}", "ver": 1, "state": state, "task": task,
                                      "seat": seat, "at": now(), "expires_at": int(now() + 86400), **self.bot}.items()
                    if v is not None})

    def workers(self, max_age: int = 600) -> list[dict]:
        return [i for i in self.b.query("WORKER") if float(i.get("at", 0)) > now() - max_age]

    def event(self, type_: str, msg: str, task: str | None = None, by: str | None = None):
        t = now()
        item = {"PK": f"EVENT#{time.strftime('%Y-%m-%d', time.gmtime(t))}", "SK": f"{t:017.6f}#{secrets.token_hex(2)}",
                "ver": 1, "type": type_, "msg": msg[:1000], "task": task,
                "by": by or os.environ.get("FARM_WORKER_ID") or "farm", "at": t, "expires_at": int(t + EVENT_TTL)}
        try:
            self.b.put({k: v for k, v in item.items() if v is not None})
        except Exception:  # noqa: BLE001 - the log must never break the work
            pass
        if self.echo:
            print(json.dumps({"at": iso(t), "event": type_, "msg": msg[:300], "task": task}), flush=True)

    def events(self, since: float | None = None, limit: int = 50) -> list[dict]:
        t = now()
        since = since if since is not None else t - 86400
        out, day = [], since
        while day <= t + 86400:
            out += self.b.query(f"EVENT#{time.strftime('%Y-%m-%d', time.gmtime(day))}", sk_gt=f"{since:017.6f}")
            day += 86400
        return out[-limit:]
