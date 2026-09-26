import threading

import pytest

from clodfarm.governor import Snapshot, Window
from clodfarm.store import now


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
    assert store.acquire_slot("a", 2, 60) == "default#000"
    assert store.acquire_slot("b", 2, 60) == "default#001"
    assert store.acquire_slot("c", 2, 60) is None
    store.release_slot("default#000", "a")
    assert store.acquire_slot("c", 2, 60) == "default#000"


def test_expired_slot_can_be_taken(store):
    store.acquire_slot("dead", 1, -1)
    assert store.acquire_slot("alive", 1, 60) == "default#000"


def test_snapshot_keeps_only_the_newest(store):
    t = now()
    new = Snapshot(observed_at=t, five_hour=Window(0.5, t + 100))
    old = Snapshot(observed_at=t - 50, five_hour=Window(0.1, t + 100))
    store.put_snapshot(new)
    store.put_snapshot(old)
    assert store.get_snapshot().five_hour.utilization == 0.5


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


def test_each_seat_has_its_own_budget_and_slots(store):
    t = now()
    store.put_snapshot(Snapshot(observed_at=t, status="rejected", resets_at=t + 3600), "matan-1a2b")
    store.put_snapshot(Snapshot(observed_at=t, five_hour=Window(0.2, t + 3600)), "gil-3c4d")
    snaps = store.snapshots()
    assert snaps["matan-1a2b"].status == "rejected" and snaps["gil-3c4d"].five_hour.utilization == 0.2
    assert store.acquire_slot("m", 1, 60, "matan-1a2b") == "matan-1a2b#000"
    assert store.acquire_slot("g", 1, 60, "gil-3c4d") == "gil-3c4d#000", "one seat's slots never block another's"
    store.add_spend(3.0, "api-aaaaaa")
    assert store.spent_today("api-aaaaaa") == 3.0 and store.spent_today("api-bbbbbb") == 0.0


def test_resumed_task_waits_for_its_home_box(store):
    t = store.add_task("parent", "x")
    store.claim_next("boxA/w0", 60, "boxA")
    store.update_task(t["id"], home="boxA")
    store.add_task("kid", "x", parent=t["id"])
    store.finish(t["id"], "boxA/w0", True, "split", 5)
    kid = store.claim_next("boxB/w0", 60, "boxB")
    store.finish(kid["id"], "boxB/w0", True, "kid done", 5)
    assert store.get_task(t["id"])["status"] == "queued"
    assert store.claim_next("boxB/w0", 60, "boxB") is None, "another box doesn't steal a resume within the affinity"
    assert store.claim_next("boxA/w0", 60, "boxA")["id"] == t["id"]


def test_items_written_before_versioning_are_adopted(store):
    """Regression (cloud test 3): an item without `ver`, written by an older clodfarm, must still update."""
    store.b.put({"PK": "CONTROL", "SK": "HEALTH", "failures": 0})  # the old format: no version
    assert store.record_health(False) == 1
    assert store.record_health(False) == 2
    assert store.record_health(True) == 0


def test_finishing_a_task_that_is_no_longer_yours_is_a_no_op(store):
    t = store.add_task("x", "x")
    store.claim_next("w1", 60)
    store.finish(t["id"], "w1", True, "done", 5)
    before = len(store.events(limit=500))
    assert store.finish(t["id"], "w1", False, "late farm error", 5) == "done"
    assert store.get_task(t["id"])["status"] == "done"
    assert len(store.events(limit=500)) == before, "no misleading task.retry event"


def test_task_for_one_claude_is_only_taken_by_it(store):
    t = store.add_task("review it, gil", "x", to="gil")
    other = store.add_task("anyone", "x")
    assert store.claim_next("matan@h/w0", 60, "matan@h", agent="matan")["id"] == other["id"]
    assert store.claim_next("matan@h/w1", 60, "matan@h", agent="matan") is None
    assert store.claim_next("gil@h/w0", 60, "gil@h", agent="gil")["id"] == t["id"]


def test_schedules_fire_once_per_due_time_on_any_box(store):
    import clodfarm.store as st
    t0 = now()
    every = store.add_schedule("standup notes", "x", every=3600, to="gil")
    once = store.add_schedule("launch check", "x", at=t0 + 120)
    cron = store.add_schedule("weekday report", "x", cron="0 9 * * 1-5", tz="Europe/Berlin")
    assert abs(every["next_at"] - (t0 + 3600)) < 5 and once["next_at"] == t0 + 120 and cron["next_at"] > t0
    with pytest.raises(ValueError):
        store.add_schedule("never", "x", at=t0 - 10)
    with pytest.raises(ValueError):
        store.add_schedule("bad", "x", cron="61 * * * *")
    real = st.now
    try:
        st.now = lambda: t0 + 3700  # an hour later: two boxes check at once
        fired = store.fire_due() + store.fire_due()
        assert sorted(f["title"] for f in fired) == ["launch check", "standup notes"]
        assert next(f for f in fired if f["title"] == "standup notes")["to"] == "gil"
        left = {s["title"]: s for s in store.schedules()}
        assert "launch check" not in left and left["standup notes"]["runs"] == 1
        assert left["standup notes"]["next_at"] > t0 + 3700
    finally:
        st.now = real
    assert store.remove_schedule(cron["id"]) and not store.remove_schedule(cron["id"])


def test_sub_agents_inherit_their_claude(store):
    top = store.add_task("big job", "x", owner="gil")
    child = store.add_task("part", "x", parent=top["id"], owner="someone-else")
    assert child["owner"] == "gil"  # the Claude that started the tree owns all of it


def test_messages_between_claudes(store):
    store.send_message("jestr", "gil", "can you review the importer?")
    store.send_message("jestr", "noa", "not for gil")
    got = store.inbox("gil")
    assert [m["text"] for m in got] == ["can you review the importer?"] and got[0]["from"] == "jestr"
    assert store.inbox("gil") == []  # read now
    assert len(store.inbox("gil", unread_only=False)) == 1
