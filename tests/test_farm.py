"""End to end: the real supervisor, store and git flow, with a fake `claude` binary."""
import json
import os
import sys
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
        tid = json.loads(cli("spawn", "big job", "--prompt", "SPAWN 2", "--json").stdout)["id"]
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
        tools = farm.store.tools()[farm.cfg.name]                     # what this Claude can use, from its runs
        assert "mcp__github__create_pr" in tools["tools"] and tools["mcp_servers"][0]["status"] == "connected"
        assert tools["plugins"] == [{"name": "farm-kit", "version": "1.0.0"}], "never the plugin paths"
    finally:
        stop_farm(farm, t)


def test_cancelling_a_running_sub_agent_stops_it_at_once(env):
    farm, t = start_farm()
    try:
        tid = json.loads(cli("spawn", "long", "--prompt", "SLOW 60 COMMIT late", "--json").stdout)["id"]
        proc = wait_for(lambda: farm.procs.get(tid))
        t0 = time.time()
        assert cli("cancel", tid).returncode == 0
        wait_for(lambda: proc.poll() is not None, timeout=20)
        assert time.time() - t0 < 15, "stopped within seconds, not when its 60 s are up"
        wait_for(lambda: [e for e in farm.store.events(time.time() - 60) if e["type"] == "task.stopped"])
        assert farm.store.get_task(tid)["status"] == "cancelled"
        assert "late.txt" not in repo_files(env)
        assert int((farm.store.b.get("CONTROL", "HEALTH") or {}).get("failures", 0)) == 0, "a cancel is not a failure"
    finally:
        stop_farm(farm, t)


def test_a_sub_agent_cancelled_while_its_check_runs_never_lands(env, monkeypatch):
    monkeypatch.setenv("FARM_VERIFY_CMD", "sleep 6")
    farm, t = start_farm()
    try:
        tid = json.loads(cli("spawn", "quick", "--prompt", "COMMIT quick", "--json").stdout)["id"]
        wait_for(lambda: [c for c in calls(env) if c.get("task") == tid])  # its run is over: the check runs now
        wait_for(lambda: tid not in farm.procs and farm.store.get_task(tid)["status"] == "running")
        assert cli("cancel", tid).returncode == 0
        wait_for(lambda: [e for e in farm.store.events(time.time() - 60) if e["type"] == "task.stopped"], timeout=30)
        assert "quick.txt" not in repo_files(env), "cancelled before it landed: main is untouched"
        assert farm.store.get_task(tid)["status"] == "cancelled"
    finally:
        stop_farm(farm, t)


def test_remote_control_is_kept_running(env):
    farm, t = start_farm()
    try:
        rc = wait_for(lambda: [c for c in calls(env) if c["cmd"] == "remote-control"])
        argv = rc[0]["argv"]
        assert argv[argv.index("--name") + 1] == "[clodfarm] test" and "--spawn" in argv
    finally:
        stop_farm(farm, t)


def test_rate_limit_pauses_the_whole_farm_and_gives_the_attempt_back(env):
    farm, t = start_farm()
    try:
        tid = json.loads(cli("spawn", "hit the wall", "--prompt", "REJECT", "--json").stdout)["id"]
        wait_for(lambda: (farm.store.get_snapshot() or 0) and farm.store.get_snapshot().status == "rejected")
        wait_for(lambda: farm.store.get_task(tid)["status"] == "queued")
        assert decide(farm.store.get_snapshot(), farm.cfg.policy, time.time()).workers == 0
        assert int(farm.store.get_task(tid)["attempts"]) == 0
        other = json.loads(cli("spawn", "must wait", "--prompt", "COMMIT waited", "--json").stdout)["id"]
        time.sleep(4)
        assert farm.store.get_task(other)["status"] == "queued", "nothing starts while rate limited"
    finally:
        stop_farm(farm, t)


def test_governor_throttles_when_the_five_hour_window_is_nearly_used(env, monkeypatch):
    monkeypatch.setenv("FAKE_UTIL_5H", "0.95")
    farm, t = start_farm()
    try:
        first = json.loads(cli("spawn", "first", "--prompt", "COMMIT first", "--json").stdout)["id"]
        wait_for(lambda: farm.store.get_task(first)["status"] == "done")
        second = json.loads(cli("spawn", "second", "--prompt", "COMMIT second", "--json").stdout)["id"]
        time.sleep(4)
        assert farm.store.get_task(second)["status"] == "queued"
        assert "5-hour window" in json.loads(cli("budget", "--json").stdout)["seats"][0]["decision"]["reason"]
    finally:
        stop_farm(farm, t)


def test_pause_stops_new_work(env):
    farm, t = start_farm()
    try:
        cli("pause", "testing")
        tid = json.loads(cli("spawn", "later", "--prompt", "COMMIT later", "--json").stdout)["id"]
        time.sleep(3)
        assert farm.store.get_task(tid)["status"] == "queued"
        cli("resume")
        wait_for(lambda: farm.store.get_task(tid)["status"] == "done")
    finally:
        stop_farm(farm, t)


def test_agents_get_the_farm_guide(env):
    farm, t = start_farm()
    try:
        tid = json.loads(cli("spawn", "x", "--prompt", "COMMIT guide", "--json").stdout)["id"]
        wait_for(lambda: farm.store.get_task(tid)["status"] == "done")
        run = [c for c in calls(env) if c.get("task") == tid][0]
        sysprompt = run["argv"][run["argv"].index("--append-system-prompt") + 1]
        assert "clodfarm spawn" in sysprompt and "clodfarm agents" in sysprompt and tid in sysprompt
        assert "clodfarm:guide:start" in open(env / "claude-home" / "CLAUDE.md").read()
    finally:
        stop_farm(farm, t)


def test_verify_gate_resumes_the_agent_to_fix_a_failing_check(env, monkeypatch):
    monkeypatch.setenv("FARM_VERIFY_CMD", "test -f fixed.txt")
    farm, t = start_farm()
    try:
        tid = json.loads(cli("spawn", "gated", "--prompt", "COMMIT feature", "--json").stdout)["id"]
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
        tid = json.loads(cli("spawn", "never passes", "--prompt", "COMMIT broken", "--json").stdout)["id"]
        wait_for(lambda: farm.store.get_task(tid)["status"] == "failed", timeout=60)
        assert "broken.txt" not in repo_files(env)
        assert "not landed" in farm.store.get_task(tid)["result"]
    finally:
        stop_farm(farm, t)


def test_timeout_resumes_the_same_session(env, monkeypatch):
    monkeypatch.setenv("FARM_TASK_TIMEOUT", "2")
    farm, t = start_farm()
    try:
        tid = json.loads(cli("spawn", "slow", "--prompt", "SLOW 6 COMMIT slow", "--json").stdout)["id"]
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
            cli("spawn", f"broken {i}", "--prompt", "FAIL")
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
        tid = json.loads(cli("spawn", "py", "--prompt", "SPAWN 1 CACHE", "--json").stdout)["id"]
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


def test_app_sessions_default_to_the_farm_model(env, monkeypatch):
    monkeypatch.setenv("FARM_MODEL", "opus")
    farm, t = start_farm()
    try:
        cfg = json.load(open(env / "claude-home" / "settings.json"))
        assert cfg["model"] == "opus" and cfg["hooks"], "the model is added next to the hooks"
    finally:
        stop_farm(farm, t)


def test_claude_code_is_kept_on_the_newest_release(env, monkeypatch):
    version = env / "claude-version"
    version.write_text("1.0.0")
    monkeypatch.setenv("FAKE_CLAUDE_VERSION", str(version))
    monkeypatch.setenv("FAKE_CLAUDE_INSTALLS", "2.0.0")
    monkeypatch.setenv("FARM_CLAUDE_UPDATE", "3600")
    monkeypatch.setenv("FARM_RC_VERSION_CHECK", "1")
    monkeypatch.setenv("FARM_TICK_SECONDS", "1")
    farm, t = start_farm()
    rcs = lambda: [c for c in calls(env) if c["cmd"] == "remote-control"]  # noqa: E731
    try:
        wait_for(rcs)
        assert [c["argv"] for c in calls(env) if c["cmd"] == "install"] == [["install", "latest"]]
        assert [e["msg"] for e in farm.store.events(time.time() - 60) if e["type"] == "claude.updated"] == \
            ["Claude Code 1.0.0 → 2.0.0"]
        assert farm.rc_version == "2.0.0", "Remote Control starts on the updated version"
        # a newer release lands while someone is talking to this Claude from the app: they are not cut off
        farm.store.record_session("s1", claude="test", kind="conversation")
        version.write_text("3.0.0")
        time.sleep(4)
        assert len(rcs()) == 1 and farm.rc_version == "2.0.0"
        farm.store.record_session("s1", ended=True)
        wait_for(lambda: len(rcs()) == 2)
        wait_for(lambda: farm.rc_version == "3.0.0")
        assert [e for e in farm.store.events(time.time() - 60) if e["type"] == "rc.updating"]
        assert not [e for e in farm.store.events(time.time() - 60) if e["type"] == "rc.exited"]
    finally:
        stop_farm(farm, t)


def test_added_claudes_leave_updating_to_the_farms_own(env, monkeypatch):
    monkeypatch.setenv("FARM_CLAUDE_UPDATE", "3600")
    monkeypatch.setenv("FARM_HATCHED", "1")
    farm, t = start_farm()
    try:
        wait_for(lambda: [c for c in calls(env) if c["cmd"] == "remote-control"])
        assert not [c for c in calls(env) if c["cmd"] == "install"]
    finally:
        stop_farm(farm, t)


def test_stopping_a_box_hands_its_running_task_back_at_once(env):
    farm, t = start_farm()
    tid = json.loads(cli("spawn", "long", "--prompt", "SLOW 30 COMMIT long", "--json").stdout)["id"]
    wait_for(lambda: farm.store.get_task(tid)["status"] == "running")
    stop_farm(farm, t)
    task = farm.store.get_task(tid)
    assert task["status"] == "queued" and task["resume_reason"] == "restart", task
    assert int(task["attempts"]) == 0, "a restart doesn't cost an attempt"
    assert farm.store.slots() == [], "its slots are free immediately"


def test_cli_hands_work_to_another_claude_and_schedules_it(env, monkeypatch):
    monkeypatch.setenv("FARM_TICK_SECONDS", "1")
    farm, t = start_farm()
    try:
        wait_for(lambda: farm.store.workers())
        out = cli("agents").stdout
        assert "test" in out and "(you)" in out
        bad = cli("spawn", "review", "--on", "gil", check=False)
        assert bad.returncode == 2 and "no Claude named 'gil'" in bad.stderr
        held = json.loads(cli("spawn", "for gil later", "--on", "gil", "--force", "--json").stdout)
        sch = json.loads(cli("schedule", "add", "tick", "--prompt", "say hi", "--at", "in 1m", "--json").stdout)
        assert sch["next_at"] > time.time() + 50
        farm.store.b.put({**farm.store.b.get("SCHEDULE", sch["id"]), "next_at": time.time()})  # make it due now
        wait_for(lambda: any(x["title"] == "tick" and x["status"] == "done" for x in farm.store.list_tasks("done")))
        assert "(no schedules)" in cli("schedule", "list").stdout  # a one-off is gone once it fired
        every = json.loads(cli("schedule", "add", "digest", "--cron", "0 9 * * *", "--tz", "Asia/Jerusalem", "--json").stdout)
        assert "cron '0 9 * * *' (Asia/Jerusalem)" in cli("schedule", "list").stdout
        assert cli("schedule", "remove", every["id"]).returncode == 0
        assert farm.store.get_task(held["id"])["status"] == "queued"  # nobody here is gil
    finally:
        stop_farm(farm, t)


def test_a_new_claude_measures_its_usage_at_once_and_reads_its_messages(env, monkeypatch):
    monkeypatch.setenv("FARM_USAGE_REFRESH", "300")
    monkeypatch.setenv("FAKE_UTIL_5H", "0.42")
    farm, t = start_farm()
    try:
        snap = wait_for(lambda: farm.store.get_snapshot(farm.seat), timeout=30)
        assert abs(snap.five_hour.utilization - 0.42) < 1e-6  # no sub-agent ran: the usage keeper measured it
        # the snapshot comes mid-run; the event is written once the measuring run has exited
        wait_for(lambda: any(e["type"] == "usage.measured" for e in farm.store.events(time.time() - 60)), timeout=30)
        settings = json.load(open(env / "claude-home" / "settings.json"))
        for event in ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"):
            assert "clodfarm hook" in json.dumps(settings["hooks"][event])
        assert cli("msg", "gil", "hi", check=False).returncode == 2  # gil isn't on this farm
        farm.store.send_message("gil", "test", "please review the importer")
        hook = lambda ev, **env: subprocess.run([sys.executable, "-m", "clodfarm", "hook"], input=json.dumps(  # noqa: E731
            {"hook_event_name": ev, "session_id": "s-1"}), capture_output=True, text=True, env={**os.environ, **env}).stdout
        assert hook("SessionStart") == ""  # only a real prompt gets the messages
        assert hook("UserPromptSubmit", FARM_TASK_ID="x") == ""  # never inside a sub-agent
        out = hook("UserPromptSubmit")
        assert "from gil" in out and "please review the importer" in out
        assert hook("UserPromptSubmit") == ""  # delivered once
        assert "5h 42% used" in cli("agents").stdout
    finally:
        stop_farm(farm, t)


def test_every_farm_session_is_marked_clodfarm(env):
    farm, t = start_farm()
    try:
        tid = json.loads(cli("spawn", "name me", "--prompt", "COMMIT named", "--json").stdout)["id"]
        wait_for(lambda: farm.store.get_task(tid)["status"] == "done")
        rc = wait_for(lambda: [c for c in calls(env) if c["cmd"] == "remote-control"])[0]["argv"]
        assert rc[rc.index("--name") + 1] == "[clodfarm] test"
        assert rc[rc.index("--remote-control-session-name-prefix") + 1] == "[clodfarm] test"
        run = [c for c in calls(env) if c.get("task") == tid][0]["argv"]
        assert run[run.index("--name") + 1] == f"[clodfarm] test · name me · {tid}"  # SendMessage finds it by id
    finally:
        stop_farm(farm, t)


def test_every_session_and_its_whole_conversation_is_recorded(env, tmp_path):
    transcript = tmp_path / "t.jsonl"
    lines = [
        {"type": "bridge-session", "sessionId": "abc", "bridgeSessionId": "session_01RC"},
        {"type": "user", "isMeta": True, "message": {"role": "user", "content": "<local-command-caveat>x"}},
        {"type": "user", "message": {"role": "user", "content": "refactor the importer"}, "timestamp": "t1"},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "secret"}, {"type": "text", "text": "On it."},
            {"type": "tool_use", "name": "Bash", "input": {"command": "clodfarm spawn parse"}}]}},
        {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "started sub-agent 1"}]}},
        {"type": "ai-title", "aiTitle": "Refactor the importer"},
    ]
    Store.from_config(load()).ensure_table()
    transcript.write_text("".join(json.dumps(x) + "\n" for x in lines[:3]))
    hook = lambda ev, **env_: subprocess.run([sys.executable, "-m", "clodfarm", "hook"], input=json.dumps(  # noqa: E731
        {"hook_event_name": ev, "session_id": "abc", "transcript_path": str(transcript), "cwd": "/w"}),
        capture_output=True, text=True, env={**os.environ, **env_}, check=True)
    hook("SessionStart")
    with open(transcript, "a") as f:  # Claude Code keeps writing; a half-written line waits for the next call
        f.write("".join(json.dumps(x) + "\n" for x in lines[3:]) + '{"type": "user", "mess')
    hook("Stop")
    hook("Stop")  # again: nothing new, nothing copied twice
    store = Store.from_config(load())
    s = store.session("abc")
    assert s["kind"] == "conversation" and s["claude"] == "test" and s["title"] == "Refactor the importer"
    assert s["remote_session"] == "session_01RC" and s["turns"] == 4
    turns = store.turns("abc")
    assert [(t["role"], t["kind"]) for t in turns] == [("user", "text"), ("assistant", "text"), ("assistant", "tool"),
                                                     ("user", "tool_result")]
    assert turns[0]["text"] == "refactor the importer" and "clodfarm spawn parse" in turns[2]["text"]
    assert "secret" not in json.dumps(turns)  # thinking is not the conversation
    out = cli("session", "abc").stdout
    assert "YOU: refactor the importer" in out and "TOOL [tool_result]: started sub-agent 1" in out
    assert "abc" in cli("sessions").stdout
    hook("SessionStart", FARM_TASK_ID="usage")  # the usage check is not recorded
    assert store.session("abc")["kind"] == "conversation" and len(store.sessions()) == 1
    hook("SessionEnd", FARM_TASK_ID="t9", FARM_OWNER="gil")  # the same hook inside a sub-agent run
    s = store.session("abc")
    assert s["ended"] and s["kind"] == "sub-agent" and s["claude"] == "gil" and s["task"] == "t9"


def test_the_farms_claude_is_named_after_its_account(env, monkeypatch):
    monkeypatch.setenv("FAKE_EMAIL", "matan@jestr.ai")
    farm, t = start_farm()
    try:
        assert farm.cfg.name == "matan" and farm.cfg.farm == "test"  # the farm keeps its name
        rc = wait_for(lambda: [c for c in calls(env) if c["cmd"] == "remote-control"])[0]["argv"]
        assert rc[rc.index("--name") + 1] == "[clodfarm] test · matan"
        wait_for(lambda: any(w["SK"].startswith("matan@") for w in farm.store.workers()))
        assert "matan" in cli("agents").stdout and "(you)" in cli("agents").stdout  # commands from outside know it too
        tid = json.loads(cli("spawn", "job", "--prompt", "COMMIT job", "--json").stdout)["id"]
        assert farm.store.get_task(tid)["owner"] == "matan"
    finally:
        stop_farm(farm, t)
