"""Messages between the Claudes: exactly-once delivery from the store, into running sub-agents (after a batch of tool
calls, before they stop, or by interrupting them), into idle conversations, and wake-ups for mail nobody reads."""
import json
import os
import subprocess
import sys
import threading
import time

from conftest import cli
from clodfarm import auth
from clodfarm.config import load
from clodfarm.store import Store
from test_farm import calls, start_farm, stop_farm, wait_for


def hook(event, stdin=None, check=False, **env):
    """`clodfarm hook` as Claude Code runs it: the event on stdin, the session's environment."""
    return subprocess.run([sys.executable, "-m", "clodfarm", "hook", *(["--listen"] if event == "listen" else [])],
                          input=json.dumps({"hook_event_name": event, "session_id": "s-1", **(stdin or {})}),
                          capture_output=True, text=True, check=check, env={**os.environ, **env})


def running(store, holder="test@box/w0"):
    t = store.add_task("job", "x")
    assert store.claim_next(holder, 300)["id"] == t["id"]
    return t["id"]


# ------------------------------------------------------------------ the store
def test_a_message_is_delivered_exactly_once(store):
    for i in range(20):
        store.send_message("gil", "test", f"m{i}")
    got, lock = [], threading.Lock()

    def reader(n):
        mine = store.claim("test", f"reader{n}")
        with lock:
            got.extend(m["text"] for m in mine)
    threads = [threading.Thread(target=reader, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(got) == sorted(f"m{i}" for i in range(20)), "every message once, none twice"
    assert store.claim("test", "late") == []
    store.unclaim(store.inbox("test", unread_only=False)[:1])
    assert [m["text"] for m in store.claim("test", "again")] == ["m0"]  # given back: delivered again


def test_a_message_rings_whoever_reads_it(store):
    tid = running(store, "gil@box/w1")
    store.send_message("noa", tid, "for the sub-agent")
    assert store.bell("gil") == 1, "the box running that sub-agent looks now"
    store.send_message("noa", "test", "for test's conversations")
    assert store.bell("test") == 1
    queued = store.add_task("later", "x")["id"]
    store.send_message("noa", queued, "gets it in its prompt")
    assert store.bell("gil") == 1 and store.bell("test") == 1  # nobody to ring: it isn't running
    m = store.unread(tid)[0]
    assert m["reply"] == "noa" and m["id"] and m["hops"] == 0


def test_a_wake_message_starts_one_mail_sub_agent_within_its_caps(store):
    store.send_message("gil", "test", "deploy is broken", wake=True, wake_after=0)
    store.send_message("gil", "test", "and the docs too", wake=True, wake_after=0)
    started = store.dispatch_wakes()
    assert len(started) == 1, "one mail sub-agent takes all of test's mail"
    t = store.get_task(started[0])
    assert (t["kind"], t["to"], t["owner"], t["title"]) == ("mail", "test", "test", "Messages for test")
    assert store.dispatch_wakes() == []  # each --wake message is handled once
    store.send_message("gil", "noa", "loop", wake=True, wake_after=0, hops=3)
    assert store.dispatch_wakes(max_hops=3) == [], "three message-triggered runs in a row: no more wakes"
    store.send_message("gil", "noa", "read in time", wake=True, wake_after=0)
    store.claim("noa", "conversation")
    assert store.dispatch_wakes() == []  # someone read it: nothing to do
    for i in range(3):
        store.send_message("gil", "zoe", f"#{i}", wake=True, wake_after=0)
        store.dispatch_wakes(per_hour=2)
        for q in store.list_tasks("queued"):
            store.cancel(q["id"])
    assert sum(t.get("to") == "zoe" for t in store.list_tasks("cancelled")) == 2, "at most 2 wakes an hour"
    assert any(e["type"] == "msg.nowake" for e in store.events())


def test_a_wake_message_resumes_a_finished_sub_agent(store):
    tid = running(store)
    store.update_task(tid, session_id="sess-1")
    store.finish(tid, "test@box/w0", True, "done", 5)
    store.send_message("gil", tid, "you missed a case", wake=True, wake_after=0)
    assert store.dispatch_wakes() == [tid]
    t = store.get_task(tid)
    assert t["status"] == "queued" and t["resume_reason"] == "message" and t["message_resumes"] == 1


def test_what_your_own_conversation_sends_is_your_instruction(env, store):
    from clodfarm.prompts import mail_text, urgent_text
    mine = store.add_task("importer", "x", owner="test")["id"]
    theirs = store.add_task("gil's job", "x", owner="gil")["id"]
    cli("msg", mine, "use the blue palette")  # from test's conversation (not a sub-agent)
    cli("msg", theirs, "can you use blue too?")
    cli("msg", mine, "from a sibling", extra_env={"FARM_TASK_ID": theirs, "FARM_OWNER": "test"})
    got = {m["text"]: m for m in store.unread(mine) + store.unread(theirs)}
    assert got["use the blue palette"].get("person") is True
    assert not got["can you use blue too?"].get("person"), "another Claude's sub-agent: a request, not an order"
    assert not got["from a sibling"].get("person"), "a sub-agent speaks for itself, not for the person"
    text = mail_text(store.unread(mine))
    assert "Your person sent you this" in text and "your person (test)" in text and "use the blue palette" in text
    assert "not your person's approval" in text and "from a sibling" in text  # both kinds, each framed as what it is
    assert "from your person" in urgent_text([got["use the blue palette"]])
    assert "from another Claude" in urgent_text([got["can you use blue too?"]])


def test_a_conversation_is_told_to_wait_for_sub_agents_in_the_background(env, store):
    from clodfarm.prompts import FARM_GUIDE
    assert "never wait for a sub-agent in the foreground" in FARM_GUIDE and "run_in_background" in FARM_GUIDE
    out = cli("spawn", "draw", "--prompt", "x").stdout
    assert "--wait --timeout 86400` with Bash in the background and end your turn" in out
    parent = store.add_task("parent", "x")["id"]
    inside = cli("spawn", "part", "--prompt", "x", "--detach", extra_env={"FARM_TASK_ID": parent}).stdout
    assert "in the background" not in inside, "a sub-agent ends its run instead (it is resumed with the results)"


# ------------------------------------------------------------------ the hooks
def test_the_farm_installs_its_hooks_and_takes_messages(env):
    home = env / "claude-home"
    (home / "settings.json").write_text(json.dumps({"crossSessionInbound": "hold", "hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": "say done"}]}],
        "PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "clodfarm inbox --hook"}]}]}}))
    auth.install_hooks()
    auth.install_messaging()
    cfg = json.load(open(home / "settings.json"))
    hooks = cfg["hooks"]
    assert cfg["crossSessionInbound"] == "hold", "a value set by hand is kept"
    assert {"type": "command", "command": "say done"} in hooks["Stop"][0]["hooks"], "the person's own hooks stay"
    assert hooks["PostToolBatch"][0]["hooks"][0]["command"] == auth.MAIL_HOOK_CMD
    assert hooks["PostToolUse"] == [{"matcher": "SendMessage", "hooks": [
        {"type": "command", "command": "clodfarm hook", "timeout": 15}]}], "the old hook is gone"
    listen = [h for g in hooks["Stop"] for h in g["hooks"] if h["command"] == auth.LISTEN_HOOK_CMD][0]
    assert listen["async"] and listen["asyncRewake"]
    before = (home / "settings.json").stat().st_mtime_ns
    auth.install_hooks()
    assert (home / "settings.json").stat().st_mtime_ns == before, "installing again changes nothing"
    os.remove(home / "settings.json")
    auth.install_messaging()
    assert json.load(open(home / "settings.json"))["crossSessionInbound"] == "accept"


def test_mail_reaches_a_session_after_a_tool_batch_and_before_it_stops(env, store):
    flag = env / "flag"
    tid = running(store)
    sub = {"FARM_TASK_ID": tid, "FARM_MAIL_FLAG": str(flag)}
    store.send_message("gil", tid, "use the v2 API")
    flag.touch()
    got = json.loads(hook("PostToolBatch", **sub).stdout)["hookSpecificOutput"]
    assert got["hookEventName"] == "PostToolBatch" and "use the v2 API" in got["additionalContext"]
    assert "from gil" in got["additionalContext"] and not flag.exists(), "the flag is used up"
    assert hook("PostToolBatch", **sub).stdout == "", "delivered once"
    guarded = subprocess.run(["/bin/sh", "-c", auth.MAIL_HOOK_CMD], input="{}", capture_output=True, text=True,
                             env={**os.environ, **sub, "PATH": "/nonexistent"})
    assert guarded.returncode == 0 and guarded.stdout == "", "no flag: the hook doesn't even start Python"
    for n in range(3):  # a Stop hook keeps the turn going for new mail, at most three times in a row
        store.send_message("gil", tid, f"one more thing #{n}")
        out = json.loads(hook("Stop", {"stop_hook_active": n > 0}, **sub).stdout)
        assert out["decision"] == "block" and f"one more thing #{n}" in out["reason"]
    store.send_message("gil", tid, "and another")
    assert hook("Stop", {"stop_hook_active": True}, **sub).stdout == "", "the cap: it stops, the mail waits"
    assert "and another" in json.loads(hook("Stop", {"stop_hook_active": False}, **sub).stdout)["reason"]


def test_a_conversation_is_busy_from_its_prompt_until_its_turn_ends(env, store):
    hook("SessionStart", check=True, FARM_TASK_ID="")
    assert not store.session("s-1").get("busy")
    hook("UserPromptSubmit", {"prompt": "fix the build"}, check=True, FARM_TASK_ID="")
    assert store.session("s-1")["busy"] is True
    store.send_message("gil", "test", "also the docs")
    assert json.loads(hook("Stop", check=True, FARM_TASK_ID="").stdout)["decision"] == "block"
    assert store.session("s-1")["busy"] is True, "kept going for its mail: still at work"
    hook("Stop", {"stop_hook_active": True}, check=True, FARM_TASK_ID="")
    assert store.session("s-1")["busy"] is False


def test_an_idle_conversation_is_woken_for_mail(env, store):
    flag = env / "mail" / "test"
    flag.parent.mkdir()

    def listener():
        p = subprocess.Popen([sys.executable, "-m", "clodfarm", "hook", "--listen"], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             env={**os.environ, "FARM_MAIL_FLAG": str(flag), "CLAUDE_CODE_ENVIRONMENT_KIND": "bridge"})
        p.stdin.write(json.dumps({"hook_event_name": "Stop", "session_id": "conv-1"}))
        p.stdin.close()
        wait_for(lambda: [f for f in flag.parent.glob(".listen-*") if f.read_text().strip() == str(p.pid)])
        return p
    first = listener()
    second = listener()
    assert first.wait(10) == 0, "one listener per session: the last turn's takes over"
    store.send_message("gil", "test", "are you around?")
    flag.touch()
    assert second.wait(20) == 2, "exit 2 wakes the conversation (asyncRewake)"
    assert "are you around?" in second.stderr.read()
    assert not list(flag.parent.glob(".listen-*"))
    for env_ in ({"FARM_TASK_ID": "t1", "CLAUDE_CODE_ENVIRONMENT_KIND": "bridge"}, {}):  # a sub-agent; a terminal
        t0 = time.time()
        assert hook("listen", FARM_MAIL_FLAG=str(flag), **env_).returncode == 0 and time.time() - t0 < 10


def test_messages_sent_with_claude_codes_own_tool_are_logged(env, store):
    hook("PostToolUse", {"tool_name": "SendMessage", "tool_input": {
        "to": "[clodfarm] test · fix the importer · 260927123456abcdef", "message": "rebased, go ahead"},
        "tool_response": {"success": True}}, FARM_OWNER="gil", FARM_TASK_ID="260927000000aaaaaa")
    hook("PostToolUse", {"tool_name": "SendMessage", "tool_input": {"to": "nobody", "message": "x"},
                         "tool_response": {"success": False}})
    sent = [e["msg"] for e in store.events() if e["type"] == "msg.sent"]
    assert sent == ["gil -> 260927123456abcdef: rebased, go ahead (live)"]


def test_every_claude_on_a_box_lists_its_sessions_in_one_place(tmp_path):
    shared, agent = tmp_path / "primary" / "sessions", tmp_path / "agents" / "gil"
    (agent / "sessions").mkdir(parents=True)
    (agent / "sessions" / "42.json").write_text("{}")
    auth.share_session_registry(str(agent), str(shared))
    assert os.path.islink(agent / "sessions") and os.path.realpath(agent / "sessions") == os.path.realpath(shared)
    assert (shared / "42.json").exists(), "a live session's entry moves along"
    auth.share_session_registry(str(agent), str(shared))  # again: nothing changes
    assert os.path.islink(agent / "sessions")


# ------------------------------------------------------------- the whole farm
def test_a_running_sub_agent_gets_a_message_at_its_next_tool_call(env):
    farm, t = start_farm()
    try:
        tid = json.loads(cli("spawn", "long job", "--prompt", "WAITMAIL 40", "--json").stdout)["id"]
        wait_for(lambda: tid in farm.running)
        out = cli("msg", tid, "the schema changed: use tenant_id").stdout
        assert "at its next tool call" in out
        wait_for(lambda: farm.store.get_task(tid)["status"] == "done", timeout=40)
        result = farm.store.get_task(tid)["result"]
        assert "mail: tool:" in result and "use tenant_id" in result, result
        assert "Your person sent you this" in result, "from its own Claude's conversation: the person's instruction"
        run = [c for c in calls(env) if c.get("task") == tid][0]
        assert run["live"] and "tenant_id" not in run["prompt"], "it came mid-run, not with the prompt"
    finally:
        stop_farm(farm, t)


def test_an_urgent_message_interrupts_a_running_sub_agent(env):
    farm, t = start_farm()
    try:
        tid = json.loads(cli("spawn", "slow job", "--prompt", "SLOW 60", "--json").stdout)["id"]
        wait_for(lambda: farm.running.get(tid))
        t0 = time.time()
        assert "interrupted" in cli("msg", tid, "--urgent", "stop: main is broken").stdout
        wait_for(lambda: farm.store.get_task(tid)["status"] == "done", timeout=30)
        assert time.time() - t0 < 25, "it did not wait out its 60 s"
        assert "got a message" in farm.store.get_task(tid)["result"] and \
            "main is broken" in farm.store.get_task(tid)["result"]
        assert cli("msg", "test", "--urgent", "x", check=False).returncode == 2  # only a sub-agent can be interrupted
    finally:
        stop_farm(farm, t)


def test_mail_for_a_sub_agent_that_isnt_running_comes_with_its_prompt(env):
    store = Store.from_config(load())
    store.ensure_table()
    tid = store.add_task("later", "COMMIT later")["id"]
    assert "when it next runs" in cli("msg", tid, "please also update the README").stdout
    farm, t = start_farm()
    try:
        wait_for(lambda: farm.store.get_task(tid)["status"] == "done")
        prompt = [c for c in calls(env) if c.get("task") == tid][0]["prompt"]
        assert "please also update the README" in prompt and "[farm message" in prompt
        assert farm.store.get_task(tid)["mail"][0]["text"] == "please also update the README"
    finally:
        stop_farm(farm, t)


def test_unread_wake_mail_starts_a_sub_agent_on_that_claude(env, monkeypatch):
    monkeypatch.setenv("FARM_MAIL_WAKE_AFTER", "0")
    monkeypatch.setenv("FARM_TICK_SECONDS", "1")
    farm, t = start_farm()
    try:
        wait_for(lambda: farm.store.workers())
        assert "starts someone" in cli("msg", "test", "--wake", "the nightly build is red").stdout
        mail = wait_for(lambda: [x for x in farm.store.list_tasks("done") if x.get("kind") == "mail"], timeout=40)[0]
        assert mail["to"] == "test" and mail["owner"] == "test"
        prompt = [c for c in calls(env) if c.get("task") == mail["id"]][0]["prompt"]
        assert "the nightly build is red" in prompt and "nobody read them in time" in prompt
    finally:
        stop_farm(farm, t)
