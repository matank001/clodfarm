"""End to end: the real supervisor, store and git flow, with a fake `claude` binary."""
import json
import os
import subprocess
import threading
import time

from conftest import cli
from clodfarm.config import load
from clodfarm.governor import decide
from clodfarm.store import Store
from clodfarm.supervisor import Farm


def start_farm():
    cfg = load()
    farm = Farm(cfg, Store.from_config(cfg))
    t = threading.Thread(target=farm.run, daemon=True)
    t.start()
    wait_for(lambda: [e for e in farm.store.events(time.time() - 60) if e["type"] == "farm.started"] if _table(farm) else None)
    return farm, t


def _table(farm):
    try:
        return farm.store.ready()
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
        def branches():
            return subprocess.run(["git", "-C", str(env / "workspace" / "repo"), "branch", "--list", "farm/*"],
                                  capture_output=True, text=True).stdout.split()
        # cleanup runs right after the task is marked done, so give it a moment
        wait_for(lambda: branches() == [], timeout=15)
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
        assert "5-hour window" in json.loads(cli("budget", "--json").stdout)["seats"][0]["decision"]["reason"]
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
        assert "clodfarm task add" in sysprompt and tid in sysprompt
        assert "clodfarm:guide:start" in open(env / "claude-home" / "CLAUDE.md").read()
    finally:
        stop_farm(farm, t)


def test_verify_gate_resumes_the_agent_to_fix_a_failing_check(env, monkeypatch):
    monkeypatch.setenv("FARM_VERIFY_CMD", "test -f fixed.txt")
    farm, t = start_farm()
    try:
        tid = json.loads(cli("task", "add", "gated", "--prompt", "COMMIT feature", "--json").stdout)["id"]
        wait_for(lambda: farm.store.get_task(tid)["status"] == "done", timeout=60)
        assert {"feature.txt", "fixed.txt"} <= repo_files(env), "lands only after the check passes"
        fix_runs = [c for c in calls(env) if c.get("task") == tid and "ran the project's check" in c["prompt"]]
        assert len(fix_runs) == 1 and fix_runs[0]["resume"], "resumed in its own session with the failure"
        assert int(farm.store.get_task(tid)["attempts"]) == 1
    finally:
        stop_farm(farm, t)


def test_verify_gate_gives_up_and_keeps_main_clean(env, monkeypatch):
    monkeypatch.setenv("FARM_VERIFY_CMD", "false")
    monkeypatch.setenv("FARM_VERIFY_FIXES", "1")
    farm, t = start_farm()
    try:
        tid = json.loads(cli("task", "add", "never passes", "--prompt", "COMMIT broken", "--json").stdout)["id"]
        wait_for(lambda: farm.store.get_task(tid)["status"] == "failed", timeout=60)
        assert "broken.txt" not in repo_files(env)
        assert "not landed" in farm.store.get_task(tid)["result"]
    finally:
        stop_farm(farm, t)


def test_timeout_resumes_the_same_session(env, monkeypatch):
    monkeypatch.setenv("FARM_TASK_TIMEOUT", "2")
    farm, t = start_farm()
    try:
        tid = json.loads(cli("task", "add", "slow", "--prompt", "SLOW 6 COMMIT slow", "--json").stdout)["id"]
        wait_for(lambda: farm.store.get_task(tid)["status"] == "done", timeout=60)
        runs = [c for c in calls(env) if c.get("task") == tid]
        assert len(runs) == 2 and runs[1]["resume"] and "time limit" in runs[1]["prompt"]
    finally:
        stop_farm(farm, t)


def test_circuit_breaker_pauses_and_notifies(env, monkeypatch):
    import http.server
    got = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            got.append(self.rfile.read(int(self.headers["Content-Length"])).decode())
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("FARM_NOTIFY_URL", f"http://127.0.0.1:{srv.server_port}/hook")
    monkeypatch.setenv("FARM_STALL_THRESHOLD", "2")
    farm, t = start_farm()
    try:
        for i in range(2):
            cli("task", "add", f"broken {i}", "--prompt", "FAIL")
        wait_for(lambda: farm.store.control().get("paused"), timeout=60)
        assert "circuit breaker" in farm.store.control()["reason"]
        assert farm.store.get_snapshot() is None or farm.store.get_snapshot().status != "rejected", \
            "an error that mentions a rate limit is not a rate limit"
        wait_for(lambda: any("circuit breaker" in g for g in got))
    finally:
        stop_farm(farm, t)
        srv.shutdown()


def test_build_artifacts_are_never_committed(env):
    """Regression (cloud test 2026-09-25): an auto-committed __pycache__ broke the parent's rebase onto main."""
    farm, t = start_farm()
    try:
        tid = json.loads(cli("task", "add", "py", "--prompt", "SPAWN 1 CACHE", "--json").stdout)["id"]
        wait_for(lambda: farm.store.get_task(tid)["status"] in ("done", "failed"), timeout=60)
        assert farm.store.get_task(tid)["status"] == "done", farm.store.get_task(tid)["result"][-500:]
        files = repo_files(env)
        assert "child0.txt" in files and not any("__pycache__" in f for f in files)
        assert not [x for x in farm.store.list_tasks("queued") if x.get("kind") == "conflict"]
    finally:
        stop_farm(farm, t)


def test_remote_control_prompt_is_pre_answered(env):
    farm, t = start_farm()
    try:
        wait_for(lambda: [c for c in calls(env) if c["cmd"] == "remote-control"])
        assert json.load(open(env / "claude-home" / ".claude.json"))["remoteDialogSeen"] is True
    finally:
        stop_farm(farm, t)


def test_mission_from_env_and_from_the_cli(env, monkeypatch):
    monkeypatch.setenv("FARM_MISSION", "Write one file.")
    farm, t = start_farm()
    try:
        mission = env / "workspace" / "repo" / "MISSION.md"
        wait_for(lambda: mission.exists())
        assert mission.read_text().strip() == "Write one file."
        cli("mission", "Write two files.")
        assert mission.read_text().strip() == "Write two files."
        assert "MISSION.md" in repo_files(env), "committed, so every worktree sees it"
        assert "Write two files." in cli("mission").stdout
    finally:
        stop_farm(farm, t)


def test_stopping_a_box_hands_its_running_task_back_at_once(env):
    farm, t = start_farm()
    tid = json.loads(cli("task", "add", "long", "--prompt", "SLOW 30 COMMIT long", "--json").stdout)["id"]
    wait_for(lambda: farm.store.get_task(tid)["status"] == "running")
    stop_farm(farm, t)
    task = farm.store.get_task(tid)
    assert task["status"] == "queued" and task["resume_reason"] == "restart", task
    assert int(task["attempts"]) == 0, "a restart doesn't cost an attempt"
    assert farm.store.slots() == [], "its slots are free immediately"
