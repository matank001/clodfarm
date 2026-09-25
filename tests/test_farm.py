"""End to end: the real supervisor, store and git flow, with a fake `claude` binary."""
import json
import os
import subprocess
import threading
import time

from conftest import cli
from claude_farm.config import load
from claude_farm.governor import decide
from claude_farm.store import Store
from claude_farm.supervisor import Farm


def start_farm():
    cfg = load()
    farm = Farm(cfg, Store.from_config(cfg))
    t = threading.Thread(target=farm.run, daemon=True)
    t.start()
    wait_for(lambda: [e for e in farm.store.events(time.time() - 60) if e["type"] == "farm.started"] if _table(farm) else None)
    return farm, t


def _table(farm):
    try:
        farm.store.client.describe_table(TableName=farm.cfg.table)
        return True
    except Exception:
        return False


def stop_farm(farm, t):
    farm.stop.set()
    t.join(15)


def wait_for(fn, timeout=40, every=0.3):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(every)
    raise AssertionError("timed out waiting")


def calls(env):
    p = env / "claude.log"
    return [json.loads(l) for l in open(p)] if p.exists() else []


def repo_files(env):
    out = subprocess.run(["git", "-C", str(env / "workspace" / "repo"), "ls-files"], capture_output=True, text=True).stdout
    return set(out.split())


def test_parent_spawns_sub_agents_and_everything_merges(env):
    farm, t = start_farm()
    try:
        tid = json.loads(cli("task", "add", "big job", "--prompt", "SPAWN 2", "--json").stdout)["id"]
        wait_for(lambda: farm.store.get_task(tid)["status"] == "done", timeout=60)
        task = farm.store.get_task(tid)
        assert task["result"].startswith("integrated")
        kids = [farm.store.get_task(c) for c in task["children"]]
        assert [k["status"] for k in kids] == ["done", "done"]
        assert {"child0.txt", "child1.txt"} <= repo_files(env), "children land on main through the parent"
        branches = subprocess.run(["git", "-C", str(env / "workspace" / "repo"), "branch", "--list", "farm/*"],
                                  capture_output=True, text=True).stdout.split()
        assert branches == [], "merged task branches are cleaned up"
        resumed = [c for c in calls(env) if c.get("task") == tid and c.get("resume")]
        assert len(resumed) == 1, "the parent is resumed once, in its own session"
        assert farm.store.get_snapshot() is not None, "every run reports real usage"
    finally:
        stop_farm(farm, t)


def test_remote_control_is_kept_running(env):
    farm, t = start_farm()
    try:
        rc = wait_for(lambda: [c for c in calls(env) if c["cmd"] == "remote-control"])
        argv = rc[0]["argv"]
        assert argv[argv.index("--name") + 1] == "test" and "--spawn" in argv
    finally:
        stop_farm(farm, t)


def test_rate_limit_pauses_the_whole_farm_and_gives_the_attempt_back(env):
    farm, t = start_farm()
    try:
        tid = json.loads(cli("task", "add", "hit the wall", "--prompt", "REJECT", "--json").stdout)["id"]
        wait_for(lambda: (farm.store.get_snapshot() or 0) and farm.store.get_snapshot().status == "rejected")
        wait_for(lambda: farm.store.get_task(tid)["status"] == "queued")
        assert decide(farm.store.get_snapshot(), farm.cfg.policy, time.time()).workers == 0
        assert int(farm.store.get_task(tid)["attempts"]) == 0
        other = json.loads(cli("task", "add", "must wait", "--prompt", "COMMIT waited", "--json").stdout)["id"]
        time.sleep(4)
        assert farm.store.get_task(other)["status"] == "queued", "nothing starts while rate limited"
    finally:
        stop_farm(farm, t)


def test_governor_throttles_when_the_five_hour_window_is_nearly_used(env, monkeypatch):
    monkeypatch.setenv("FAKE_UTIL_5H", "0.95")
    farm, t = start_farm()
    try:
        first = json.loads(cli("task", "add", "first", "--prompt", "COMMIT first", "--json").stdout)["id"]
        wait_for(lambda: farm.store.get_task(first)["status"] == "done")
        second = json.loads(cli("task", "add", "second", "--prompt", "COMMIT second", "--json").stdout)["id"]
        time.sleep(4)
        assert farm.store.get_task(second)["status"] == "queued"
        assert "5-hour window" in json.loads(cli("budget", "--json").stdout)["decision"]["reason"]
    finally:
        stop_farm(farm, t)


def test_planner_keeps_agents_busy_from_the_mission(env, monkeypatch):
    monkeypatch.setenv("FARM_PLANNER", "1")
    monkeypatch.setenv("FAKE_PLAN", "PLAN 2")
    repo = env / "workspace" / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "MISSION.md").write_text("Write two files.\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "mission"], check=True)
    farm, t = start_farm()
    try:
        wait_for(lambda: {"planned0.txt", "planned1.txt"} <= repo_files(env), timeout=60)
        plans = wait_for(lambda: [x for x in farm.store.list_tasks("done") if x["kind"] == "plan"])
        assert int(plans[0].get("spawned", 0)) == 2
    finally:
        stop_farm(farm, t)


def test_pause_stops_new_work(env):
    farm, t = start_farm()
    try:
        cli("pause", "testing")
        tid = json.loads(cli("task", "add", "later", "--prompt", "COMMIT later", "--json").stdout)["id"]
        time.sleep(3)
        assert farm.store.get_task(tid)["status"] == "queued"
        cli("resume")
        wait_for(lambda: farm.store.get_task(tid)["status"] == "done")
    finally:
        stop_farm(farm, t)


def test_agents_get_the_farm_guide(env):
    farm, t = start_farm()
    try:
        tid = json.loads(cli("task", "add", "x", "--prompt", "COMMIT guide", "--json").stdout)["id"]
        wait_for(lambda: farm.store.get_task(tid)["status"] == "done")
        run = [c for c in calls(env) if c.get("task") == tid][0]
        sysprompt = run["argv"][run["argv"].index("--append-system-prompt") + 1]
        assert "claude-farm task add" in sysprompt and tid in sysprompt
        assert "claude-farm:guide:start" in open(env / "claude-home" / "CLAUDE.md").read()
    finally:
        stop_farm(farm, t)
