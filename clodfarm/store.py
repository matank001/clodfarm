"""The farm's state: task queue, per-seat budget, slots, control switches, heartbeats and the event log.

All logic is written once here, on the primitives in ``backends.py``: SQLite for one box (the default) and DynamoDB
for a farm spread over several boxes and Claude accounts.

Items (PK / SK):

    TASK#<id>          / META              a task (GSI1PK = STATUS#<status>, GSI1SK = priority, then age)
    TASK#<id>          / RUN#<ts>          one agent run of that task (usage, cost)
    BUDGET             / <seat>            newest rate_limit snapshot of that Claude account (seat)
    SLOT               / <seat>#<n>        concurrency slot n of that seat (lease, shared by its boxes)
    SPEND              / <seat>#<day>      API-mode list-price spend per seat and day
    WORKER             / <farm>/<worker>   heartbeat
    SCHEDULE           / <id>              a scheduled task: queued again every time it is due
    CONTROL            / GLOBAL | PLANNER | HEALTH
    EVENT#<yyyy-mm-dd> / <ts>#<rand>       event log (expires after 30 days)
"""

from __future__ import annotations

import copy
import json
import os
import secrets
import time

from .backends import Backend, DynamoBackend, SqliteBackend
from .governor import Snapshot
from .schedule import next_run

EVENT_TTL = 30 * 86400
DEFAULT_SEAT = "default"  # single-account farms and tests
_RETRIES = 50


def now() -> float:
    return time.time()


def iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts if ts is not None else now()))


def new_id() -> str:
    return time.strftime("%y%m%d%H%M%S", time.gmtime()) + secrets.token_hex(3)  # sortable: time prefix + randomness


def _prio_key(priority: int, created: float, tid: str) -> str:
    return f"{9 - max(0, min(9, int(priority)))}#{created:017.6f}#{tid}"  # higher priority first; then oldest first


class Store:
    def __init__(self, backend: Backend):
        self.b = backend
        self.echo = False  # the daemon prints every event as a JSON log line; the CLI stays quiet

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
                 to: str | None = None) -> dict:
        """``to`` hands the task to one Claude on the farm (an agent's name, e.g. ``gil``): only its boxes take it."""
        depth = 0
        if parent:
            p = self.get_task(parent)
            if not p:
                raise ValueError(f"parent task {parent} not found")
            depth = int(p.get("depth", 0)) + 1
            if depth > max_depth:
                raise ValueError(f"sub-task depth {depth} exceeds FARM_MAX_DEPTH={max_depth}; "
                                 "do the work yourself instead of splitting further")
        tid, t = new_id(), now()
        item = {"PK": f"TASK#{tid}", "SK": "META", "GSI1PK": "STATUS#queued", "GSI1SK": _prio_key(priority, t, tid),
                "ver": 1, "id": tid, "title": title[:300], "prompt": prompt, "status": "queued", "priority": priority,
                "kind": kind, "parent": parent, "depth": depth, "created": t, "updated": t, "created_by": created_by,
                "attempts": 0, "max_attempts": max_attempts, "resumes": 0, "children_open": 0, "to": to or None}
        item = {k: v for k, v in item.items() if v is not None}
        self.b.put(item, expect_ver=0)
        spawner = os.environ.get("FARM_TASK_ID")
        if spawner and created_by == spawner:  # lets the planner's back-off know whether it found work
            self._update(*self._tkey(spawner), lambda x: {**x, "spawned": int(x.get("spawned", 0)) + 1})
        if parent:
            self._update(*self._tkey(parent), lambda x: {**x, "children_open": int(x.get("children_open", 0)) + 1,
                                                          "children": list(x.get("children") or []) + [tid]})
        self.event("task.added", f"{tid} {title[:120]}" + (f" (for {to})" if to else ""), task=tid, by=created_by)
        return item

    def get_task(self, tid: str) -> dict | None:
        return self.b.get(*self._tkey(tid))

    def list_tasks(self, status: str | None = None, limit: int = 50) -> list[dict]:
        statuses = [status] if status else ["running", "queued", "waiting", "done", "failed", "cancelled"]
        out = []
        for s in statuses:
            out += self.b.query_index(f"STATUS#{s}", limit, desc=s in ("done", "failed", "cancelled"))
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
                   agent: str | None = None) -> dict | None:
        """Atomically take the highest-priority queued task this box may take.

        A task handed to one Claude (``to``) is only taken by that agent's boxes (``agent`` is the box's farm name).

        A task waiting to be *resumed* keeps its conversation on the box it last ran on (``home``). For ``affinity``
        seconds only that box may take it; after that anyone may, starting fresh with the results so far."""
        for item in self.b.query_index("STATUS#queued", 100):
            tid = item["id"]
            if item.get("to") and item["to"] != agent:
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

        A parent whose sub-tasks are still open goes to ``waiting``; it is re-queued
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
            self.event("task.waiting", f"{tid} waits for its sub-tasks", task=tid)
            return "waiting"
        task = self.get_task(tid) or {}
        if task.get("pending_review") and int(task.get("resumes", 0)) < max_resumes:
            self._set_status(tid, "queued", when=mine, extra={**fields, "resume": True}, remove=drop)
            self.event("task.resume", f"{tid}: sub-tasks finished during the run; resuming", task=tid)
            return "queued"
        self._set_status(tid, "done", when=mine, extra=fields, remove=drop)
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
                self.event("task.resume", f"{parent}: all sub-tasks finished; resuming", task=parent)

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
        ok = self._set_status(tid, "cancelled", when=lambda x: x.get("status") in ("queued", "waiting", "running"),
                              remove=("lease_until",))
        if ok:
            self.event("task.cancelled", tid, task=tid)
            self._child_finished(tid)
        return ok

    def retry(self, tid: str) -> bool:
        ok = self._set_status(tid, "queued", when=lambda x: x.get("status") in ("failed", "cancelled", "done"),
                              extra={"attempts": 0})
        if ok:
            self.event("task.retry", f"{tid} re-queued by hand", task=tid)
        return ok

    def reap_expired(self) -> int:
        """Re-queue running tasks whose worker died (lease ran out)."""
        n = 0
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

    # ------------------------------------------------------------- schedules
    def add_schedule(self, title: str, prompt: str, *, cron: str | None = None, every: int | None = None,
                     at: float | None = None, tz: str = "UTC", to: str | None = None, priority: int = 5,
                     created_by: str = "human") -> dict:
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
                **{k: v for k, v in {**spec, "to": to or None}.items() if v is not None}}
        self.b.put(item, expect_ver=0)
        self.event("schedule.added", f"{sid} {title[:120]}", by=created_by)
        return item

    def schedules(self) -> list[dict]:
        return sorted(self.b.query("SCHEDULE"), key=lambda x: float(x.get("next_at", 0)))

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
            if float(sch.get("next_at", 0)) > t:
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
                                 max_attempts=max_attempts, to=it.get("to"))
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

    # --------------------------------------------------------------- control
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

    def planner_try_start(self, cooldown: int) -> bool:
        """Single-flight: only one planner run across all boxes per cooldown."""
        t = now()
        return self._update("CONTROL", "PLANNER", lambda x: None if float(x.get("next_allowed", 0)) >= t
                            else {**x, "last_start": t, "next_allowed": t + cooldown}, create=True) is not None

    def planner_backoff(self, added: int, cooldown: int, max_backoff: int):
        """A planner that found nothing to do backs off exponentially."""
        def fn(x):
            idle = 0 if added else int(x.get("idle_runs", 0)) + 1
            wait = min(cooldown * (2 ** idle), max_backoff) if idle else cooldown
            return {**x, "idle_runs": idle, "next_allowed": now() + wait}
        self._update("CONTROL", "PLANNER", fn, create=True)

    def planner_state(self) -> dict:
        return self.b.get("CONTROL", "PLANNER") or {}

    # --------------------------------------------------------- heartbeats/log
    def heartbeat(self, farm: str, worker: str, state: str, task: str | None = None, seat: str | None = None):
        self.b.put({k: v for k, v in {"PK": "WORKER", "SK": f"{farm}/{worker}", "ver": 1, "state": state, "task": task,
                                      "seat": seat, "at": now(), "expires_at": int(now() + 86400)}.items()
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
