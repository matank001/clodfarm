"""Run one headless Claude Code agent and read its stream-json output."""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field

from .governor import Snapshot

# Claude Code's wording when the subscription limit is hit and no rate_limit_event says so. Only ever matched
# against an *error* result, never against what the agent wrote (a task about rate limiting is not a rate limit).
LIMIT_TEXT = re.compile(r"usage limit|limit reached|limit will reset|out of (extra )?usage", re.I)


@dataclass
class RunResult:
    ok: bool
    text: str
    session_id: str | None = None
    cost_usd: float = 0.0
    usage: dict = field(default_factory=dict)
    terminal_reason: str | None = None
    num_turns: int = 0
    duration_s: float = 0.0
    snapshots: list = field(default_factory=list)
    rate_limited: bool = False
    timed_out: bool = False
    error_text: str = ""  # the result text when Claude Code reported an error
    init: dict = field(default_factory=dict)  # Claude Code's init event: its tools, MCP servers, skills, plugins


def session_name(cfg, what: str = "") -> str:
    """How a farm session is named in the Claude app and claude.ai/code: always marked [clodfarm]."""
    who = cfg.name if cfg.name == cfg.farm else f"{cfg.farm} · {cfg.name}"
    return f"[clodfarm] {who}" + (f" · {what}" if what else "")


def build_cmd(cfg, system_prompt: str, resume_session: str | None = None, name: str = "", live: bool = False) -> list[str]:
    cmd = [cfg.claude_bin, "-p", "--output-format", "stream-json", "--verbose", "--name", name or session_name(cfg),
           "--model", cfg.model, "--permission-mode", cfg.permission_mode,
           "--append-system-prompt", system_prompt]
    if live:
        cmd += ["--input-format", "stream-json"]
    if getattr(cfg, "task_budget_usd", 0) and cfg.policy.api_mode:
        cmd += ["--max-budget-usd", str(cfg.task_budget_usd)]
    if getattr(cfg, "effort", ""):
        cmd += ["--effort", cfg.effort]
    if resume_session:
        cmd += ["--resume", resume_session]
    return cmd


def _user_line(text: str) -> str:
    return json.dumps({"type": "user", "message": {"role": "user", "content": text}, "parent_tool_use_id": None,
                       "session_id": "default"}) + "\n"


class Live:
    """The open stdin of a sub-agent started with ``--input-format stream-json``: the farm can interrupt it and hand
    it a message while it works. stdin is closed at the run's last result, so the process then exits as with a plain
    prompt; a message handed over mid-run means one more turn, and one more result, before that."""

    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.more = 0  # results still to come before stdin closes
        self.closed = False

    def _write(self, data: str) -> bool:
        try:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
            return True
        except (BrokenPipeError, OSError, ValueError):
            self.closed = True
            return False

    def interrupt(self, text: str) -> bool:
        """Stop what the agent is doing (a running tool is cancelled) and give it ``text`` as a new turn."""
        with self.lock:
            if self.closed or not self.proc:
                return False
            ok = self._write(json.dumps({"type": "control_request", "request_id": f"farm-{time.time():.6f}",
                                         "request": {"subtype": "interrupt"}}) + "\n") and self._write(_user_line(text))
            if ok:
                self.more += 1
            return ok

    def result(self):
        """A result arrived: close stdin unless a turn is still to come."""
        with self.lock:
            if self.more > 0:
                self.more -= 1
                return
            self.close()

    def close(self):
        if not self.closed and self.proc:
            self.closed = True
            try:
                self.proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass


def run_agent(cmd: list[str], prompt: str, cwd: str, env: dict, timeout: int,
              on_snapshot=None, on_line=None, on_start=None, live: Live | None = None) -> RunResult:
    """Start claude, feed the prompt on stdin, and parse events as they stream.

    The prompt goes on stdin, not argv: some flags are variadic and would swallow it,
    and long prompts don't fit on a command line. With ``live`` (the command has ``--input-format stream-json``)
    it is one stream-json user message, and stdin stays open until the last result (see ``Live``).
    """
    t0 = time.time()
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, bufsize=1, start_new_session=True)
    if live:
        live.proc = proc
    if on_start:
        on_start(proc)
    killer = threading.Timer(timeout, lambda: kill_tree(proc))
    killer.start()
    stderr_tail: list[str] = []

    def drain_stderr():
        for line in proc.stderr:
            stderr_tail.append(line)
            del stderr_tail[:-50]

    threading.Thread(target=drain_stderr, daemon=True).start()
    try:
        if live:
            with live.lock:
                live._write(_user_line(prompt))
        else:
            proc.stdin.write(prompt)
            proc.stdin.close()
    except BrokenPipeError:
        pass

    res = RunResult(ok=False, text="")
    last_text = ""
    turns = 0
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if on_line:
            on_line(ev)
        typ = ev.get("type")
        if typ == "system" and ev.get("subtype") == "init":
            res.session_id = ev.get("session_id")
            res.init = ev
        elif typ == "rate_limit_event":
            snap = Snapshot.from_event(ev.get("rate_limit_info") or {}, time.time())
            res.snapshots.append(snap)
            if snap.status == "rejected":
                res.rate_limited = True
            if on_snapshot:
                on_snapshot(snap)
        elif typ == "assistant":
            for block in (ev.get("message") or {}).get("content") or []:
                if block.get("type") == "text" and block.get("text"):
                    last_text = block["text"]
        elif typ == "result":
            res.session_id = ev.get("session_id") or res.session_id
            res.text = ev.get("result") or last_text
            # one process, one running total (an interrupted turn's result comes before the next turn's)
            res.cost_usd = max(res.cost_usd, float(ev.get("total_cost_usd") or 0))
            res.usage = ev.get("usage") or {}
            res.terminal_reason = ev.get("terminal_reason")
            turns += int(ev.get("num_turns") or 0)
            res.num_turns = turns
            res.ok = not ev.get("is_error") and ev.get("subtype", "success") == "success"
            res.error_text = ""
            if not res.ok:
                res.error_text = str(ev.get("result") or ev.get("subtype") or "")[:2000]
                if ev.get("api_error_status") == 429:
                    res.rate_limited = True
            if live:
                live.result()
    if live:
        live.close()
    proc.wait()
    killer.cancel()
    res.duration_s = time.time() - t0
    if not res.text:
        res.text = last_text or "".join(stderr_tail)[-2000:] or f"claude exited with code {proc.returncode}"
    if proc.returncode not in (0, None) and res.ok:
        res.ok = False
    if time.time() - t0 >= timeout:
        # checked first: a timeout must never be mistaken for a usage limit
        res.ok, res.timed_out, res.rate_limited = False, True, False
        res.text = f"timed out after {timeout}s. Last output: {res.text[-1500:]}"
    elif not res.ok and not res.rate_limited and LIMIT_TEXT.search(res.error_text):
        res.rate_limited = True
    return res


def kill_tree(proc: subprocess.Popen):
    """Stop a run and everything it started (it leads its own process group): SIGTERM, then SIGKILL after 5 s.
    On SIGTERM Claude Code stops the running command's process tree and runs its SessionEnd hooks."""
    try:
        os.killpg(proc.pid, 15)
        time.sleep(5)
        os.killpg(proc.pid, 9)
    except (ProcessLookupError, PermissionError):
        pass
