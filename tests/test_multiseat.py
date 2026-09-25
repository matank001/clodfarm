"""Two boxes on two different Claude accounts (seats) share one farm: one queue, one git origin, separate budgets."""
import dataclasses
import subprocess
import threading
import time

from conftest import cli
from clodfarm.config import load
from clodfarm.governor import Snapshot
from clodfarm.store import Store
from clodfarm.supervisor import Farm
from test_farm import stop_farm, wait_for


def make_origin(env):
    origin, seed = env / "origin.git", env / "seed"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(seed)], check=True, capture_output=True)
    (seed / "README.md").write_text("shared repo\n")
    subprocess.run(["git", "-C", str(seed), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-qm", "seed"], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "main"], check=True)
    return origin


def start_box(env, origin, name, seat):
    cfg = dataclasses.replace(load(), name=name, seat=seat, workspace=str(env / f"ws-{name}"), repo_url=str(origin),
                              push=True, remote_control=False)
    farm = Farm(cfg, Store.from_config(cfg))
    t = threading.Thread(target=farm.run, daemon=True)
    t.start()
    wait_for(lambda: [w for w in farm.store.workers() if w["SK"].startswith(cfg.farm_id) and w.get("seat") == seat])
    return farm, t


def origin_files(origin):
    out = subprocess.run(["git", "--git-dir", str(origin), "ls-tree", "-r", "--name-only", "main"],
                         capture_output=True, text=True).stdout
    return set(out.split())


def test_a_rate_limited_seat_stops_while_the_other_keeps_working(env, store):
    origin = make_origin(env)
    a = start_box(env, origin, "boxA", "matan-aaaa")
    b = start_box(env, origin, "boxB", "gil-bbbb")
    try:
        t0 = time.time()
        a[0].store.put_snapshot(Snapshot(observed_at=t0, status="rejected", rate_limit_type="five_hour",
                                         resets_at=t0 + 3600), "matan-aaaa")
        ids = [__import__("json").loads(cli("task", "add", f"t{i}", "--prompt", f"COMMIT m{i}", "--json").stdout)["id"]
               for i in range(3)]
        wait_for(lambda: all(a[0].store.get_task(i)["status"] == "done" for i in ids), timeout=90)
        homes = {a[0].store.get_task(i)["home"] for i in ids}
        assert homes == {b[0].cfg.farm_id}, "only the seat with headroom took work"
        assert {"m0.txt", "m1.txt", "m2.txt"} <= origin_files(origin), "both boxes land on the shared origin"
        seats = {r["seat"]: r for r in __import__("json").loads(cli("budget", "--json").stdout)["seats"]}
        assert seats["matan-aaaa"]["decision"]["workers"] == 0 and seats["gil-bbbb"]["decision"]["workers"] > 0
    finally:
        stop_farm(*a)
        stop_farm(*b)


def test_parent_and_children_across_two_seats_land_through_origin(env, store):
    origin = make_origin(env)
    a = start_box(env, origin, "boxA", "matan-aaaa")
    b = start_box(env, origin, "boxB", "gil-bbbb")
    try:
        tid = __import__("json").loads(cli("task", "add", "big", "--prompt", "SPAWN 3", "--json").stdout)["id"]
        wait_for(lambda: a[0].store.get_task(tid)["status"] in ("done", "failed"), timeout=120)
        task = a[0].store.get_task(tid)
        assert task["status"] == "done", task["result"][-600:]
        assert {"child0.txt", "child1.txt", "child2.txt"} <= origin_files(origin)
        remote = subprocess.run(["git", "--git-dir", str(origin), "branch", "--list", "farm/*"],
                                capture_output=True, text=True).stdout.split()
        assert remote == [], "task branches are removed from origin after landing"
    finally:
        stop_farm(*a)
        stop_farm(*b)
