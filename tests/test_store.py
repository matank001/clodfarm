import threading

import pytest

from claude_farm.governor import Snapshot, Window
from claude_farm.store import now


def test_priority_then_age_order(store):
    a = store.add_task("low", "x", priority=2)
    b = store.add_task("high", "x", priority=8)
    c = store.add_task("high later", "x", priority=8)
    order = [store.claim_next("w", 60)["id"] for _ in range(3)]
    assert order == [b["id"], c["id"], a["id"]]
    assert store.claim_next("w", 60) is None


def test_claims_are_atomic_under_contention(store):
    for i in range(10):
        store.add_task(f"t{i}", "x")
    got, lock = [], threading.Lock()

    def grab(n):
        while True:
            t = store.claim_next(f"w{n}", 60)
            if not t:
                return
            with lock:
                got.append(t["id"])

    threads = [threading.Thread(target=grab, args=(n,)) for n in range(5)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(got) == 10 and len(set(got)) == 10


def test_sub_task_depth_limit(store):
    t = store.add_task("root", "x")
    c1 = store.add_task("c1", "x", parent=t["id"], max_depth=2)
    c2 = store.add_task("c2", "x", parent=c1["id"], max_depth=2)
    assert c2["depth"] == 2
    with pytest.raises(ValueError):
        store.add_task("c3", "x", parent=c2["id"], max_depth=2)


def test_parent_waits_for_children_then_resumes(store):
    p = store.add_task("parent", "x")
    assert store.claim_next("w", 60)["id"] == p["id"]
    kids = [store.add_task(f"k{i}", "x", parent=p["id"]) for i in range(2)]
    assert store.finish(p["id"], "w", True, "split", 5) == "waiting"
    for k in kids:
        assert store.claim_next("w", 60)["id"] == k["id"]
        store.finish(k["id"], "w", True, f"{k['title']} done", 5)
    parent = store.get_task(p["id"])
    assert parent["status"] == "queued" and parent["resume"] is True
    assert store.claim_next("w", 60)["id"] == p["id"]
    store.mark_resumed(p["id"])
    assert store.finish(p["id"], "w", True, "integrated", 5) == "done"


def test_children_finishing_during_parent_run_trigger_one_resume(store):
    p = store.add_task("parent", "x")
    store.claim_next("w", 60)
    k = store.add_task("kid", "x", parent=p["id"])
    store.claim_next("w2", 60)
    store.finish(k["id"], "w2", True, "kid done", 5)  # finishes while the parent still runs
    assert store.finish(p["id"], "w", True, "split", 5) == "queued"
    assert store.get_task(p["id"])["resume"] is True


def test_failure_retries_then_fails(store):
    t = store.add_task("flaky", "x", max_attempts=2)
    store.claim_next("w", 60)
    assert store.finish(t["id"], "w", False, "boom", 5) == "queued"
    store.claim_next("w", 60)
    assert store.finish(t["id"], "w", False, "boom", 5) == "failed"


def test_expired_lease_is_reaped(store):
    t = store.add_task("orphan", "x")
    store.claim_next("dead-worker", -1)  # lease already expired
    assert store.reap_expired() == 1
    assert store.get_task(t["id"])["status"] == "queued"


def test_only_the_lease_holder_can_finish(store):
    t = store.add_task("mine", "x")
    store.claim_next("w1", 60)
    store.finish(t["id"], "w2", True, "stolen", 5)
    assert store.get_task(t["id"])["status"] == "running"


def test_slots_are_global_and_capped(store):
    assert store.acquire_slot("a", 2, 60) == 0
    assert store.acquire_slot("b", 2, 60) == 1
    assert store.acquire_slot("c", 2, 60) is None
    store.release_slot(0, "a")
    assert store.acquire_slot("c", 2, 60) == 0


def test_expired_slot_can_be_taken(store):
    store.acquire_slot("dead", 1, -1)
    assert store.acquire_slot("alive", 1, 60) == 0


def test_snapshot_keeps_only_the_newest(store):
    t = now()
    new = Snapshot(observed_at=t, five_hour=Window(0.5, t + 100))
    old = Snapshot(observed_at=t - 50, five_hour=Window(0.1, t + 100))
    store.put_snapshot(new)
    store.put_snapshot(old)
    assert store.get_snapshot().five_hour.utilization == 0.5


def test_planner_single_flight(store):
    assert store.planner_try_start(600)
    assert not store.planner_try_start(600)


def test_planner_backs_off_when_idle(store):
    store.planner_try_start(10)
    store.planner_backoff(0, 10, 1000)
    first = store.planner_state()["next_allowed"]
    store.planner_backoff(0, 10, 1000)
    assert store.planner_state()["next_allowed"] > first
    assert store.planner_state()["idle_runs"] == 2


def test_pause_switch(store):
    store.set_paused(True, "maintenance")
    assert store.control()["paused"] is True
    store.set_paused(False)
    assert store.control()["paused"] is False


def test_update_task_removes_none_fields(store):
    t = store.add_task("x", "x")
    store.update_task(t["id"], branch="b", session_id="s")
    store.update_task(t["id"], branch=None, session_id="s2")
    got = store.get_task(t["id"])
    assert "branch" not in got and got["session_id"] == "s2"


def test_spend_accumulates_per_day(store):
    store.add_spend(1.25)
    store.add_spend(0.75)
    assert abs(store.spent_today() - 2.0) < 1e-9
