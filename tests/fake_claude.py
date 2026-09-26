#!/usr/bin/env python3
"""A stand-in for the `claude` binary that speaks the same stream-json protocol.

Behaviour is driven by words in the prompt, so tests can script agents:
  SPAWN <n>     start n sub-agents with `clodfarm spawn` (children of $FARM_TASK_ID)
  COMMIT <name> write <name>.txt in the working directory and git-commit it
  REJECT        report a rejected rate limit and fail
  SLOW <s>      sleep s seconds before answering
  CACHE         leave an uncommitted __pycache__/cache.cpython-311.pyc behind, like a test run does
  FAIL          end with an error result whose text mentions a rate limit (it is not one)
  (resumed to fix a failing check: writes fixed.txt and commits it)
FAKE_CHILD_SLOW=<s> makes SPAWNed sub-agents take s seconds (for watching them in the farm UI).
Utilization reported in each rate_limit_event comes from FAKE_UTIL_5H / FAKE_UTIL_7D.
Every invocation is appended to $FAKE_CLAUDE_LOG (JSON lines) for assertions.
"""
import json
import os
import re
import subprocess
import sys
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
        out({"loggedIn": os.environ.get("FAKE_LOGGED_IN", "1") == "1", "authMethod": "claude.ai", "subscriptionType": "max"})
        return 0
    if argv[:1] == ["--version"]:
        print("9.9.9 (Fake Claude)")
        return 0
    if argv[:1] == ["remote-control"]:
        log({"cmd": "remote-control", "argv": argv, "cwd": os.getcwd()})
        print("Remote Control ready (fake)", flush=True)
        while True:
            time.sleep(3600)
    if "-p" not in argv:
        print("fake claude: unsupported invocation " + " ".join(argv), file=sys.stderr)
        return 2

    prompt = sys.stdin.read()
    session = argv[argv.index("--resume") + 1] if "--resume" in argv else str(uuid.uuid4())
    log({"cmd": "print", "argv": argv, "cwd": os.getcwd(), "prompt": prompt, "task": os.environ.get("FARM_TASK_ID"),
         "resume": "--resume" in argv})
    now = time.time()
    u5, u7 = float(os.environ.get("FAKE_UTIL_5H", "0.10")), float(os.environ.get("FAKE_UTIL_7D", "0.10"))
    out({"type": "system", "subtype": "init", "session_id": session, "model": "claude-opus-5-5"})
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

    if "FAIL" in prompt and "ran the project's check" not in prompt:
        out({"type": "result", "subtype": "error_during_execution", "is_error": True, "session_id": session,
             "result": "3 tests failed: test_rate_limit_backoff expected 429", "num_turns": 1})
        return 1
    if "ran the project's check" in prompt:
        prompt += " COMMIT fixed"
    m = re.search(r"SLOW (\d+)", prompt)
    if m:
        time.sleep(int(m.group(1)))
    text = "did the work"
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
    out({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}],
                                          "usage": {"output_tokens": 42}}})
    out({"type": "result", "subtype": "success", "is_error": False, "session_id": session, "result": text,
         "num_turns": 1, "total_cost_usd": 0.01, "usage": {"output_tokens": 42}, "terminal_reason": "completed"})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
