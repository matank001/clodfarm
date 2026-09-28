#!/usr/bin/env python3
"""A stand-in for the `claude` binary that speaks the same stream-json protocol.

Behaviour is driven by words in the prompt, so tests can script agents:
  SPAWN <n>     start n sub-agents with `clodfarm spawn` (children of $FARM_TASK_ID)
  COMMIT <name> write <name>.txt in the working directory and git-commit it
  REJECT        report a rejected rate limit and fail
  SLOW <s>      sleep s seconds before answering
  CACHE         leave an uncommitted __pycache__/cache.cpython-311.pyc behind, like a test run does
  FAIL          end with an error result whose text mentions a rate limit (it is not one)
  WAITMAIL <s>  work in batches of tool calls for up to s seconds, running the PostToolBatch hooks after each, until
                one hands over mail; then run the Stop hooks (as STOPMAIL)
  STOPMAIL      run the Stop hooks before finishing; one that blocks keeps the turn going with its reason
  (resumed to fix a failing check: writes fixed.txt and commits it)
With `--input-format stream-json` it reads the prompt as a user message and keeps reading stdin: an interrupt
(control_request) ends the turn (SLOW and WAITMAIL notice it), and every further user message is another turn.
The mail each turn got ends up in its result text ("mail: ...").
FAKE_CHILD_SLOW=<s> makes SPAWNed sub-agents take s seconds (for watching them in the farm UI).
Utilization reported in each rate_limit_event comes from FAKE_UTIL_5H / FAKE_UTIL_7D.
Every invocation is appended to $FAKE_CLAUDE_LOG (JSON lines) for assertions.
`--version` prints the version in $FAKE_CLAUDE_VERSION (a file) when set; `install` writes $FAKE_CLAUDE_INSTALLS into it.
"""
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid


def out(obj):
    print(json.dumps(obj), flush=True)


def log(entry):
    p = os.environ.get("FAKE_CLAUDE_LOG")
    if p:
        with open(p, "a") as f:
            f.write(json.dumps(entry) + "\n")


def farm_cli(*args):
    subprocess.run([sys.executable, "-m", "clodfarm", *args], check=True, capture_output=True, text=True)


def main(argv):
    if argv[:2] == ["auth", "status"]:
        st = {"loggedIn": os.environ.get("FAKE_LOGGED_IN", "1") == "1", "authMethod": "claude.ai", "subscriptionType": "max"}
        if os.environ.get("FAKE_EMAIL"):
            st["email"] = os.environ["FAKE_EMAIL"]
        out(st)
        return 0
    if argv[:1] == ["--version"]:
        vf = os.environ.get("FAKE_CLAUDE_VERSION")
        print(f"{open(vf).read().strip() if vf else '9.9.9'} (Fake Claude)")
        return 0
    if argv[:1] == ["install"]:
        log({"cmd": "install", "argv": argv})
        if os.environ.get("FAKE_CLAUDE_VERSION") and os.environ.get("FAKE_CLAUDE_INSTALLS"):
            with open(os.environ["FAKE_CLAUDE_VERSION"], "w") as f:
                f.write(os.environ["FAKE_CLAUDE_INSTALLS"])
        return 0
    if argv[:1] == ["remote-control"]:
        log({"cmd": "remote-control", "argv": argv, "cwd": os.getcwd()})
        print("Remote Control ready (fake)", flush=True)
        while True:
            time.sleep(3600)
    if "-p" not in argv:
        print("fake claude: unsupported invocation " + " ".join(argv), file=sys.stderr)
        return 2

    live = "--input-format" in argv and argv[argv.index("--input-format") + 1] == "stream-json"
    inbox: "queue.Queue" = queue.Queue()
    if live:
        def read():
            for line in sys.stdin:
                try:
                    inbox.put(json.loads(line))
                except ValueError:
                    pass
            inbox.put(None)
        threading.Thread(target=read, daemon=True).start()
        first = next_user(inbox)
        prompt = first if first is not None else ""
    else:
        prompt = sys.stdin.read()
    session = argv[argv.index("--resume") + 1] if "--resume" in argv else str(uuid.uuid4())
    log({"cmd": "print", "argv": argv, "cwd": os.getcwd(), "prompt": prompt, "task": os.environ.get("FARM_TASK_ID"),
         "resume": "--resume" in argv, "live": live,
         "base_url": os.environ.get("ANTHROPIC_BASE_URL"), "token": os.environ.get("ANTHROPIC_AUTH_TOKEN"),
         "model": argv[argv.index("--model") + 1] if "--model" in argv else None})
    now = time.time()
    u5, u7 = float(os.environ.get("FAKE_UTIL_5H", "0.10")), float(os.environ.get("FAKE_UTIL_7D", "0.10"))
    out({"type": "system", "subtype": "init", "session_id": session, "model": "claude-opus-5-5",
         "claude_code_version": "9.9.9", "permissionMode": "bypassPermissions",
         "tools": ["Bash", "Edit", "Read", "mcp__github__create_pr"], "skills": ["dataviz"], "agents": ["Explore"],
         "mcp_servers": [{"name": "github", "status": "connected", "source": "project"}],
         "plugins": [{"name": "farm-kit", "version": "1.0.0", "path": "/secret/path"}]})
    rejected = "REJECT" in prompt
    out({"type": "rate_limit_event", "rate_limit_info": {
        "status": "rejected" if rejected else "allowed", "resetsAt": int(now + 3600), "rateLimitType": "five_hour",
        "isUsingOverage": False,
        "unifiedWindows": {"five_hour": {"utilization": 1.0 if rejected else u5, "resetsAt": int(now + 3600)},
                           "seven_day": {"utilization": u7, "resetsAt": int(now + 5 * 86400)}}}})
    if rejected:
        out({"type": "result", "subtype": "error_during_execution", "is_error": True, "session_id": session,
             "result": "Claude usage limit reached.", "num_turns": 0})
        return 1
    code = turn(prompt, session, inbox if live else None)
    while live:  # like claude: idle between turns until stdin closes
        more = next_user(inbox)
        if more is None:
            break
        code = turn(more, session, inbox, first=False)
    return code


def next_user(inbox):
    """The next user message on a stream-json stdin (None at its end); control requests are answered."""
    while True:
        ev = inbox.get()
        if ev is None:
            return None
        if ev.get("type") == "control_request":
            out({"type": "control_response", "response": {"subtype": "success", "request_id": ev.get("request_id"),
                                                          "response": {"still_queued": []}}})
            continue
        if ev.get("type") == "user":
            c = (ev.get("message") or {}).get("content")
            return c if isinstance(c, str) else json.dumps(c)


def interrupted(inbox) -> bool:
    """A control_request interrupt arrived (answered here); user messages wait for their own turn."""
    if inbox is None:
        return False
    held, hit = [], False
    while True:
        try:
            ev = inbox.get_nowait()
        except queue.Empty:
            break
        if ev and ev.get("type") == "control_request" and (ev.get("request") or {}).get("subtype") == "interrupt":
            out({"type": "control_response", "response": {"subtype": "success", "request_id": ev.get("request_id"),
                                                          "response": {"still_queued": []}}})
            hit = True
        else:
            held.append(ev)
    for ev in held:
        inbox.put(ev)
    return hit


def pause(seconds: float, inbox) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if interrupted(inbox):
            return True
        time.sleep(0.1)
    return False


def run_hooks(event: str, session: str, **fields) -> list:
    """Run this Claude's hooks for ``event`` like Claude Code does (async ones are left out): JSON on stdin, each
    one's stdout parsed as JSON when it is."""
    try:
        cfg = json.load(open(os.path.join(os.environ.get("CLAUDE_CONFIG_DIR", ""), "settings.json")))
    except (OSError, ValueError):
        return []
    outs = []
    for group in (cfg.get("hooks") or {}).get(event, []):
        for h in group.get("hooks", []):
            if h.get("async"):
                continue
            payload = {"hook_event_name": event, "session_id": session, "cwd": os.getcwd(), **fields}
            r = subprocess.run(["sh", "-c", h["command"]], input=json.dumps(payload), capture_output=True, text=True,
                               timeout=60)
            try:
                outs.append(json.loads(r.stdout))
            except ValueError:
                outs.append(r.stdout)
    return outs


def turn(prompt: str, session: str, inbox, first: bool = True) -> int:
    def interrupted_result():
        out({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "[Request interrupted by user]"}]}})
        out({"type": "result", "subtype": "error_during_execution", "is_error": True, "session_id": session,
             "result": None, "num_turns": 1})
        return 1

    if not first:  # a message handed over while it ran (live stdin)
        text = "got a message: " + prompt[:500]
        out({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})
        out({"type": "result", "subtype": "success", "is_error": False, "session_id": session, "result": text,
             "num_turns": 1, "total_cost_usd": 0.02, "usage": {"output_tokens": 7}, "terminal_reason": "completed"})
        return 0
    if "FAIL" in prompt and "ran the project's check" not in prompt:
        out({"type": "result", "subtype": "error_during_execution", "is_error": True, "session_id": session,
             "result": "3 tests failed: test_rate_limit_backoff expected 429", "num_turns": 1})
        return 1
    if "ran the project's check" in prompt:
        prompt += " COMMIT fixed"
    m = re.search(r"SLOW (\d+)", prompt)
    if m and pause(int(m.group(1)), inbox):
        return interrupted_result()
    text = "did the work"
    got = []
    m = re.search(r"WAITMAIL (\d+)", prompt)
    if m:
        end = time.time() + int(m.group(1))
        while time.time() < end and not got:
            for o in run_hooks("PostToolBatch", session):
                ctx = (o.get("hookSpecificOutput") or {}).get("additionalContext") if isinstance(o, dict) else None
                if ctx:
                    got.append("tool: " + ctx)
            if not got and pause(0.3, inbox):
                return interrupted_result()
    resumed = "Your sub-agents have finished" in prompt
    m = re.search(r"SPAWN (\d+)", prompt)
    if m and not resumed:
        for i in range(int(m.group(1))):
            farm_cli("spawn", f"child {i}", "--prompt", f"COMMIT child{i}" + (f" SLOW {os.environ['FAKE_CHILD_SLOW']}" if os.environ.get("FAKE_CHILD_SLOW") else ""))
        text = f"split into {m.group(1)} sub-agents"
    for name in re.findall(r"COMMIT (\w+)", prompt):
        with open(f"{name}.txt", "w") as f:
            f.write(name + "\n")
        subprocess.run(["git", "add", f"{name}.txt"], check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", f"add {name}"], check=True)
        text = f"committed {name}"
    if "CACHE" in prompt:
        os.makedirs("__pycache__", exist_ok=True)
        with open("__pycache__/cache.cpython-311.pyc", "wb") as f:
            f.write(os.urandom(16))
    if resumed:
        for br in sorted(set(re.findall(r"branch (farm/\w+)", prompt))):
            subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "merge", "-q", "--no-edit", br], check=True)
        text = "integrated the sub-agent results"
    if "WAITMAIL" in prompt or "STOPMAIL" in prompt:
        active = False
        for _ in range(6):  # Claude Code keeps a turn going while a Stop hook blocks
            blocks = [o for o in run_hooks("Stop", session, stop_hook_active=active)
                      if isinstance(o, dict) and o.get("decision") == "block"]
            if not blocks:
                break
            got.append("stop: " + blocks[0].get("reason", ""))
            active = True
    if got:
        text += " | mail: " + " || ".join(got)
    out({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}],
                                          "usage": {"output_tokens": 42}}})
    out({"type": "result", "subtype": "success", "is_error": False, "session_id": session, "result": text,
         "num_turns": 1, "total_cost_usd": 0.01, "usage": {"output_tokens": 42}, "terminal_reason": "completed"})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
