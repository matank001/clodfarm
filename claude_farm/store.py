"""DynamoDB single-table store.

One table holds everything for one Claude subscription. Several farms (hosts)
can share the table; that is how they share one budget.

Items (PK / SK):

    TASK#<id>        / META              a task (GSI1PK = STATUS#<status>)
    TASK#<id>        / RUN#<ts>          one agent run of that task (usage, cost)
    BUDGET           / <seat>            newest rate_limit snapshot of that Claude account (seat)
    SLOT             / <seat>#<n>        concurrency slot n of that seat (lease, shared by its boxes)
    SPEND            / <seat>#<day>      API-mode list-price spend per seat and day
    WORKER           / <farm>/<worker>   heartbeat
    CONTROL          / GLOBAL            pause switch
    CONTROL          / PLANNER           planner single-flight + back-off
    EVENT#<yyyy-mm-dd> / <ts>#<rand>     event log (expires after 30 days)

GSI1 (GSI1PK, GSI1SK) indexes tasks by status; GSI1SK sorts by priority, then age.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from .governor import Snapshot

EVENT_TTL = 30 * 86400
DEFAULT_SEAT = "default"  # single-account farms and tests
OPEN = ("queued", "running", "waiting")


def now() -> float:
    return time.time()


def iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts if ts is not None else now()))


def new_id() -> str:
    # sortable: time prefix + randomness
    return time.strftime("%y%m%d%H%M%S", time.gmtime()) + secrets.token_hex(3)


def _clean(o):
    """DynamoDB wants Decimal, not float; and back again on the way out."""
    if isinstance(o, float):
        return Decimal(str(o))
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items() if v is not None}
    if isinstance(o, list):
        return [_clean(v) for v in o]
    return o


def _plain(o):
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    if isinstance(o, dict):
        return {k: _plain(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_plain(v) for v in o]
    return o


def _conditional_failed(e: ClientError) -> bool:
    return e.response["Error"]["Code"] == "ConditionalCheckFailedException"


def _prio_key(priority: int, created: float, tid: str) -> str:
    # higher priority first; then oldest first
    return f"{9 - max(0, min(9, priority))}#{created:017.6f}#{tid}"


class Store:
    def __init__(self, table: str, region: str, endpoint: str | None = None):
        kw = {"region_name": region}
        if endpoint:
            kw["endpoint_url"] = endpoint
            # DynamoDB Local accepts any credentials
            if not os.environ.get("AWS_ACCESS_KEY_ID"):
                kw.update(aws_access_key_id="local", aws_secret_access_key="local")
        self.ddb = boto3.resource("dynamodb", **kw)
        self.client = self.ddb.meta.client
        self.name = table
        self.t = self.ddb.Table(table)
        self.echo = False  # the daemon prints every event as a JSON log line; the CLI stays quiet

    @classmethod
    def from_config(cls, cfg) -> "Store":
        return cls(cfg.table, cfg.region, cfg.endpoint)

    # ---------------------------------------------------------------- schema
    def ensure_table(self) -> bool:
        """Create the table if it doesn't exist. Returns True if created."""
        try:
            self.client.describe_table(TableName=self.name)
            return False
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceNotFoundException":
                raise
        self.client.create_table(
            TableName=self.name,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": n, "AttributeType": "S"} for n in ("PK", "SK", "GSI1PK", "GSI1SK")
            ],
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            GlobalSecondaryIndexes=[{
                "IndexName": "GSI1",
                "KeySchema": [{"AttributeName": "GSI1PK", "KeyType": "HASH"},
                              {"AttributeName": "GSI1SK", "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "ALL"},
            }],
        )
        self.client.get_waiter("table_exists").wait(TableName=self.name)
        try:
            self.client.update_time_to_live(
                TableName=self.name, TimeToLiveSpecification={"Enabled": True, "AttributeName": "expires_at"})
        except ClientError:
            pass  # DynamoDB Local may not support TTL; harmless
        return True

    # ----------------------------------------------------------------- tasks
    def add_task(self, title: str, prompt: str, priority: int = 5, parent: str | None = None,
                 kind: str = "task", created_by: str = "human", max_depth: int = 3,
                 max_attempts: int = 3) -> dict:
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
        item = {
            "PK": f"TASK#{tid}", "SK": "META", "GSI1PK": "STATUS#queued", "GSI1SK": _prio_key(priority, t, tid),
            "id": tid, "title": title[:300], "prompt": prompt, "status": "queued", "priority": priority,
            "kind": kind, "parent": parent, "depth": depth, "created": t, "updated": t,
            "created_by": created_by, "attempts": 0, "max_attempts": max_attempts, "resumes": 0,
            "children_open": 0,
        }
        self.t.put_item(Item=_clean(item))
        spawner = os.environ.get("FARM_TASK_ID")
        if spawner and created_by == spawner:
            # lets the planner's back-off know whether it found work
            self.t.update_item(Key={"PK": f"TASK#{spawner}", "SK": "META"}, UpdateExpression="ADD spawned :one",
                               ExpressionAttributeValues={":one": 1})
        if parent:
            self.t.update_item(Key={"PK": f"TASK#{parent}", "SK": "META"},
                               UpdateExpression="ADD children_open :one SET children = list_append(if_not_exists(children, :e), :c)",
                               ExpressionAttributeValues={":one": 1, ":e": [], ":c": [tid]})
        self.event("task.added", f"{tid} {title[:120]}", task=tid, by=created_by)
        return _plain(item)

    def get_task(self, tid: str) -> dict | None:
        r = self.t.get_item(Key={"PK": f"TASK#{tid}", "SK": "META"})
        return _plain(r["Item"]) if "Item" in r else None

    def list_tasks(self, status: str | None = None, limit: int = 50) -> list[dict]:
        statuses = [status] if status else ["running", "queued", "waiting", "done", "failed", "cancelled"]
        out = []
        for s in statuses:
            kw = {"IndexName": "GSI1", "KeyConditionExpression": Key("GSI1PK").eq(f"STATUS#{s}"), "Limit": limit}
            if s in ("done", "failed", "cancelled"):
                kw["ScanIndexForward"] = False  # newest first
            out += [_plain(i) for i in self.t.query(**kw).get("Items", [])]
        return out[:limit] if status else out

    def count(self, status: str) -> int:
        n, kw = 0, {"IndexName": "GSI1", "KeyConditionExpression": Key("GSI1PK").eq(f"STATUS#{status}"),
                    "Select": "COUNT"}
        while True:
            r = self.t.query(**kw)
            n += r["Count"]
            if "LastEvaluatedKey" not in r:
                return n
            kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]

    def _set_status(self, tid: str, status: str, cond=None, extra: dict | None = None, remove=()) -> bool:
        t = now()
        task = self.get_task(tid)
        if not task:
            return False
        sk = (_prio_key(int(task.get("priority", 5)), task["created"], tid) if status == "queued"
              else f"{t:017.6f}#{tid}")
        vals = {"status": status, "GSI1PK": f"STATUS#{status}", "GSI1SK": sk, "updated": t, **(extra or {})}
        names = {f"#{k}": k for k in vals}
        expr = "SET " + ", ".join(f"#{k} = :{k}" for k in vals)
        if remove:
            expr += " REMOVE " + ", ".join(remove)
        kw = {"Key": {"PK": f"TASK#{tid}", "SK": "META"}, "UpdateExpression": expr,
              "ExpressionAttributeNames": names, "ExpressionAttributeValues": _clean({f":{k}": v for k, v in vals.items()})}
        if cond is not None:
            kw["ConditionExpression"] = cond
        try:
            self.t.update_item(**kw)
            return True
        except ClientError as e:
            if _conditional_failed(e):
                return False
            raise

    def claim_next(self, worker: str, lease: int, farm: str | None = None, affinity: float = 600) -> dict | None:
        """Atomically take the highest-priority queued task.

        A task waiting to be *resumed* keeps its conversation on the box it last ran on (``home``). For ``affinity``
        seconds only that box may take it; after that anyone may, starting fresh with the results so far."""
        r = self.t.query(IndexName="GSI1", KeyConditionExpression=Key("GSI1PK").eq("STATUS#queued"), Limit=25)
        for item in r.get("Items", []):
            tid = item["id"]
            if farm and item.get("resume") and item.get("home") and item["home"] != farm \
                    and now() - float(item.get("updated", 0)) < affinity:
                continue
            ok = self._set_status(tid, "running", cond=Attr("status").eq("queued"),
                                  extra={"worker": worker, "lease_until": now() + lease, "started": now()})
            if ok:
                self.t.update_item(Key={"PK": f"TASK#{tid}", "SK": "META"}, UpdateExpression="ADD attempts :one",
                                   ExpressionAttributeValues={":one": 1})
                self.event("task.claimed", f"{tid} by {worker}", task=tid)
                return self.get_task(tid)
        return None

    def renew_lease(self, tid: str, worker: str, lease: int) -> bool:
        try:
            self.t.update_item(Key={"PK": f"TASK#{tid}", "SK": "META"}, UpdateExpression="SET lease_until = :l",
                               ConditionExpression=Attr("worker").eq(worker) & Attr("status").eq("running"),
                               ExpressionAttributeValues=_clean({":l": now() + lease}))
            return True
        except ClientError as e:
            if _conditional_failed(e):
                return False
            raise

    def update_task(self, tid: str, **fields):
        """Set fields; a field given as None is removed."""
        drop = [k for k, v in fields.items() if v is None]
        fields = {k: v for k, v in fields.items() if v is not None}
        fields["updated"] = now()
        names = {f"#{k}": k for k in [*fields, *drop]}
        expr = "SET " + ", ".join(f"#{k} = :{k}" for k in fields)
        if drop:
            expr += " REMOVE " + ", ".join(f"#{k}" for k in drop)
        self.t.update_item(Key={"PK": f"TASK#{tid}", "SK": "META"}, UpdateExpression=expr,
                           ExpressionAttributeNames=names,
                           ExpressionAttributeValues=_clean({f":{k}": v for k, v in fields.items()}))

    def finish(self, tid: str, worker: str, ok: bool, result: str, max_resumes: int) -> str:
        """Finish a run. Returns the task's new status.

        A parent whose sub-tasks are still open goes to ``waiting``; it is re-queued
        (and resumed with their results) when the last one finishes.
        """
        mine = Attr("worker").eq(worker) & Attr("status").eq("running")
        fields = {"result": result[-8000:], "finished": now()}
        if not ok:
            task = self.get_task(tid) or {}
            status = "failed" if int(task.get("attempts", 1)) >= int(task.get("max_attempts", 3)) else "queued"
            self._set_status(tid, status, cond=mine, extra=fields, remove=("lease_until", "worker"))
            self.event("task.failed" if status == "failed" else "task.retry", f"{tid}: {result[-200:]}", task=tid)
            if status == "failed":
                self._child_finished(tid)
            return status
        if self._set_status(tid, "waiting", cond=mine & Attr("children_open").gt(0), extra=fields,
                            remove=("lease_until", "worker")):
            self.event("task.waiting", f"{tid} waits for its sub-tasks", task=tid)
            return "waiting"
        task = self.get_task(tid) or {}
        if task.get("pending_review") and int(task.get("resumes", 0)) < max_resumes:
            self._set_status(tid, "queued", cond=mine, extra={**fields, "resume": True},
                             remove=("lease_until", "worker"))
            self.event("task.resume", f"{tid}: sub-tasks finished during the run; resuming", task=tid)
            return "queued"
        self._set_status(tid, "done", cond=mine, extra=fields, remove=("lease_until", "worker"))
        self.event("task.done", f"{tid} {task.get('title', '')[:120]}", task=tid)
        self._child_finished(tid)
        return "done"

    def _child_finished(self, tid: str):
        task = self.get_task(tid)
        parent = task and task.get("parent")
        if not parent:
            return
        r = self.t.update_item(Key={"PK": f"TASK#{parent}", "SK": "META"},
                               UpdateExpression="ADD children_open :m SET pending_review = :t",
                               ExpressionAttributeValues={":m": -1, ":t": True}, ReturnValues="ALL_NEW")
        p = _plain(r["Attributes"])
        if int(p.get("children_open", 0)) <= 0 and p.get("status") == "waiting":
            if self._set_status(parent, "queued", cond=Attr("status").eq("waiting"), extra={"resume": True}):
                self.event("task.resume", f"{parent}: all sub-tasks finished; resuming", task=parent)

    def requeue_resume(self, tid: str, worker: str, reason: str, note: str, counter: str) -> bool:
        """Put a running task straight back in the queue to be resumed in its own session (to fix a failing
        verify, or to continue after a timeout). The attempt is given back; ``counter`` counts these."""
        mine = Attr("worker").eq(worker) & Attr("status").eq("running")
        task = self.get_task(tid) or {}
        ok = self._set_status(tid, "queued", cond=mine, extra={
            "resume": True, "resume_reason": reason, "resume_note": note[-6000:],
            "attempts": max(0, int(task.get("attempts", 1)) - 1), counter: int(task.get(counter, 0)) + 1},
            remove=("lease_until", "worker"))
        if ok:
            self.event(f"task.{reason}", f"{tid}: resuming to {'fix the failing check' if reason == 'verify' else 'continue after a timeout'}",
                       task=tid)
        return ok

    def mark_resumed(self, tid: str):
        self.t.update_item(Key={"PK": f"TASK#{tid}", "SK": "META"},
                           UpdateExpression="SET pending_review = :f, #r = :f ADD resumes :one REMOVE resume_reason, resume_note",
                           ExpressionAttributeNames={"#r": "resume"},
                           ExpressionAttributeValues={":f": False, ":one": 1})

    def cancel(self, tid: str) -> bool:
        ok = self._set_status(tid, "cancelled", cond=Attr("status").is_in(["queued", "waiting", "running"]),
                              remove=("lease_until",))
        if ok:
            self.event("task.cancelled", tid, task=tid)
            self._child_finished(tid)
        return ok

    def retry(self, tid: str) -> bool:
        ok = self._set_status(tid, "queued", cond=Attr("status").is_in(["failed", "cancelled", "done"]),
                              extra={"attempts": 0})
        if ok:
            self.event("task.retry", f"{tid} re-queued by hand", task=tid)
        return ok

    def reap_expired(self) -> int:
        """Re-queue running tasks whose worker died (lease ran out)."""
        n = 0
        r = self.t.query(IndexName="GSI1", KeyConditionExpression=Key("GSI1PK").eq("STATUS#running"))
        for item in r.get("Items", []):
            if float(item.get("lease_until", 0)) < now():
                task = _plain(item)
                status = "failed" if int(task.get("attempts", 0)) >= int(task.get("max_attempts", 3)) else "queued"
                cond = Attr("status").eq("running") & Attr("lease_until").lt(Decimal(str(now())))
                if self._set_status(task["id"], status, cond=cond, remove=("lease_until", "worker")):
                    n += 1
                    self.event("task.reaped", f"{task['id']} lease expired (worker {task.get('worker')}); {status}",
                               task=task["id"])
        return n

    def add_run(self, tid: str, run: dict):
        self.t.put_item(Item=_clean({"PK": f"TASK#{tid}", "SK": f"RUN#{iso()}#{secrets.token_hex(2)}", **run}))

    def runs(self, tid: str) -> list[dict]:
        r = self.t.query(KeyConditionExpression=Key("PK").eq(f"TASK#{tid}") & Key("SK").begins_with("RUN#"))
        return [_plain(i) for i in r.get("Items", [])]

    # ---------------------------------------------------------------- budget
    # Everything budget-related is per seat (one Claude account): snapshots, slots, spend. The queue is shared.
    def put_snapshot(self, snap: Snapshot, seat: str = DEFAULT_SEAT):
        """Keep only the newest observation per seat (several workers report at once)."""
        try:
            self.t.put_item(Item=_clean({"PK": "BUDGET", "SK": seat, "seat": seat, **snap.to_dict()}),
                            ConditionExpression=Attr("observed_at").not_exists()
                            | Attr("observed_at").lt(Decimal(str(snap.observed_at))))
        except ClientError as e:
            if not _conditional_failed(e):
                raise

    def get_snapshot(self, seat: str = DEFAULT_SEAT) -> Snapshot | None:
        r = self.t.get_item(Key={"PK": "BUDGET", "SK": seat})
        return Snapshot.from_dict(_plain(r["Item"])) if "Item" in r else None

    def snapshots(self) -> dict[str, Snapshot]:
        """Every seat's newest snapshot."""
        r = self.t.query(KeyConditionExpression=Key("PK").eq("BUDGET"))
        return {i["SK"]: Snapshot.from_dict(_plain(i)) for i in r.get("Items", [])}

    def add_spend(self, usd: float, seat: str = DEFAULT_SEAT, ts: float | None = None):
        """Running total of list-price spend per seat and UTC day (the API-mode daily cap reads it)."""
        if usd <= 0:
            return
        day = time.strftime("%Y-%m-%d", time.gmtime(ts if ts is not None else now()))
        self.t.update_item(Key={"PK": "SPEND", "SK": f"{seat}#{day}"}, UpdateExpression="ADD usd :u SET expires_at = :e",
                           ExpressionAttributeValues=_clean({":u": float(usd), ":e": int(now() + 90 * 86400)}))

    def spent_today(self, seat: str = DEFAULT_SEAT) -> float:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        r = self.t.get_item(Key={"PK": "SPEND", "SK": f"{seat}#{day}"}).get("Item")
        return float(r["usd"]) if r else 0.0

    def acquire_slot(self, holder: str, allowed: int, lease: int, seat: str = DEFAULT_SEAT) -> str | None:
        """Take one of ``allowed`` concurrency slots of this seat, shared by every box logged in to it.
        Returns the slot key (renew and release with it)."""
        t = now()
        for n in range(allowed):
            key = f"{seat}#{n:03d}"
            try:
                self.t.put_item(
                    Item=_clean({"PK": "SLOT", "SK": key, "seat": seat, "holder": holder, "lease_until": t + lease}),
                    ConditionExpression=Attr("holder").not_exists() | Attr("holder").eq(holder)
                    | Attr("lease_until").lt(Decimal(str(t))))
                return key
            except ClientError as e:
                if not _conditional_failed(e):
                    raise
        return None

    def renew_slot(self, key: str, holder: str, lease: int) -> bool:
        try:
            self.t.update_item(Key={"PK": "SLOT", "SK": key}, UpdateExpression="SET lease_until = :l",
                               ConditionExpression=Attr("holder").eq(holder),
                               ExpressionAttributeValues=_clean({":l": now() + lease}))
            return True
        except ClientError as e:
            if _conditional_failed(e):
                return False
            raise

    def release_slot(self, key: str, holder: str):
        try:
            self.t.delete_item(Key={"PK": "SLOT", "SK": key}, ConditionExpression=Attr("holder").eq(holder))
        except ClientError as e:
            if not _conditional_failed(e):
                raise

    def slots(self, seat: str | None = None) -> list[dict]:
        kw = {"KeyConditionExpression": Key("PK").eq("SLOT") & Key("SK").begins_with(f"{seat}#")} if seat else \
            {"KeyConditionExpression": Key("PK").eq("SLOT")}
        r = self.t.query(**kw)
        return [_plain(i) for i in r.get("Items", []) if float(i.get("lease_until", 0)) > now()]

    # --------------------------------------------------------------- control
    def set_paused(self, paused: bool, reason: str = "", by: str = "human"):
        self.t.put_item(Item=_clean({"PK": "CONTROL", "SK": "GLOBAL", "paused": paused, "reason": reason,
                                     "by": by, "at": now()}))
        if not paused:
            self.record_health(True)
        self.event("farm.paused" if paused else "farm.resumed", reason or "", by=by)

    def control(self) -> dict:
        r = self.t.get_item(Key={"PK": "CONTROL", "SK": "GLOBAL"})
        return _plain(r.get("Item", {}))

    def record_health(self, ok: bool) -> int:
        """Consecutive failed runs across all farms (the circuit breaker's input). Returns the new count."""
        if ok:
            self.t.put_item(Item={"PK": "CONTROL", "SK": "HEALTH", "failures": 0})
            return 0
        r = self.t.update_item(Key={"PK": "CONTROL", "SK": "HEALTH"}, UpdateExpression="ADD failures :one",
                               ExpressionAttributeValues={":one": 1}, ReturnValues="UPDATED_NEW")
        return int(r["Attributes"]["failures"])

    def planner_try_start(self, cooldown: int) -> bool:
        """Single-flight: only one planner run across all farms per cooldown."""
        t = now()
        try:
            self.t.update_item(
                Key={"PK": "CONTROL", "SK": "PLANNER"}, UpdateExpression="SET last_start = :t",
                ConditionExpression=Attr("next_allowed").not_exists() | Attr("next_allowed").lt(Decimal(str(t))),
                ExpressionAttributeValues=_clean({":t": t}))
            self.t.update_item(Key={"PK": "CONTROL", "SK": "PLANNER"}, UpdateExpression="SET next_allowed = :n",
                               ExpressionAttributeValues=_clean({":n": t + cooldown}))
            return True
        except ClientError as e:
            if _conditional_failed(e):
                return False
            raise

    def planner_backoff(self, added: int, cooldown: int, max_backoff: int):
        """Planner that found nothing to do backs off exponentially."""
        r = self.t.get_item(Key={"PK": "CONTROL", "SK": "PLANNER"}).get("Item", {})
        idle = 0 if added else int(r.get("idle_runs", 0)) + 1
        wait = min(cooldown * (2 ** idle), max_backoff) if idle else cooldown
        self.t.update_item(Key={"PK": "CONTROL", "SK": "PLANNER"},
                           UpdateExpression="SET idle_runs = :i, next_allowed = :n",
                           ExpressionAttributeValues=_clean({":i": idle, ":n": now() + wait}))

    def planner_state(self) -> dict:
        return _plain(self.t.get_item(Key={"PK": "CONTROL", "SK": "PLANNER"}).get("Item", {}))

    # --------------------------------------------------------- heartbeats/log
    def heartbeat(self, farm: str, worker: str, state: str, task: str | None = None, seat: str | None = None):
        self.t.put_item(Item=_clean({"PK": "WORKER", "SK": f"{farm}/{worker}", "state": state, "task": task,
                                     "seat": seat, "at": now(), "expires_at": int(now() + 86400)}))

    def workers(self, max_age: int = 600) -> list[dict]:
        r = self.t.query(KeyConditionExpression=Key("PK").eq("WORKER"))
        return [_plain(i) for i in r.get("Items", []) if float(i.get("at", 0)) > now() - max_age]

    def event(self, type_: str, msg: str, task: str | None = None, by: str | None = None):
        t = now()
        item = {"PK": f"EVENT#{time.strftime('%Y-%m-%d', time.gmtime(t))}",
                "SK": f"{t:017.6f}#{secrets.token_hex(2)}", "type": type_, "msg": msg[:1000], "task": task,
                "by": by or os.environ.get("FARM_WORKER_ID") or "farm", "at": t,
                "expires_at": int(t + EVENT_TTL)}
        try:
            self.t.put_item(Item=_clean(item))
        except ClientError:
            pass  # the log must never break the work
        if self.echo:
            print(json.dumps({"at": iso(t), "event": type_, "msg": msg[:300], "task": task}), flush=True)

    def events(self, since: float | None = None, limit: int = 50) -> list[dict]:
        t = now()
        since = since if since is not None else t - 86400
        out, day = [], since
        while day <= t + 86400:
            kw = {"KeyConditionExpression": Key("PK").eq(f"EVENT#{time.strftime('%Y-%m-%d', time.gmtime(day))}")
                  & Key("SK").gt(f"{since:017.6f}")}
            while True:
                r = self.t.query(**kw)
                out += [_plain(i) for i in r.get("Items", [])]
                if "LastEvaluatedKey" not in r:
                    break
                kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]
            day += 86400
        return out[-limit:]
