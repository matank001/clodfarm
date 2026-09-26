"""Agents you add from the farm UI: each one is its own Claude Code login (a seat) with its own `clodfarm run`.

The container's own login is the *primary* agent; the farm daemon runs it. Every agent added in the UI gets a
Claude config dir of its own (``$CLAUDE_CONFIG_DIR/../clodfarm-agents/<id>`` by default, inside the claude-home
volume so the login survives restarts) and a child ``clodfarm run`` process with that dir. The children share the
farm's store and repo, so they join the same queue and are paced on their own account's budget, exactly like a
second box on a multi-seat farm.

Login runs ``claude auth login`` in a pseudo-terminal: the UI shows the URL it prints and types the code you paste
back into it. clodfarm never stores or logs that code, or any credential.
"""

from __future__ import annotations

import json
import os
import pty
import re
import secrets
import select
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

from .auth import auth_status, claude_home

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Z0-9]|\x1b[=>]")
URL = re.compile(r"https://[^\s\"'<>]+")
ID_OK = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}$")
_CRED_ENV = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "FARM_SEAT")
HATS = ["straw", "beanie", "cap", "flower", "headphones", "bow", "crown", "leaf"]


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24].strip("-")
    return s or "claude-" + secrets.token_hex(2)


class LoginSession:
    """One ``claude auth login`` in a pseudo-terminal."""

    def __init__(self, claude_bin: str, config_dir: str, env: dict):
        self.config_dir, self.claude_bin = config_dir, claude_bin
        self.state = "starting"  # starting | waiting_code | checking | done | failed
        self.url: str | None = None
        self.error = ""
        self.tail = ""
        self.started = time.time()
        self._lock = threading.Lock()
        self.pid, self.fd = pty.fork()
        if self.pid == 0:  # child: become `claude auth login`
            try:
                os.execvpe(claude_bin, [claude_bin, "auth", "login", "--claudeai"], env)
            finally:
                os._exit(127)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        buf = ""
        while True:
            try:
                r, _, _ = select.select([self.fd], [], [], 1.0)
                if not r:
                    if time.time() - self.started > 900:
                        self._finish("failed", "the login timed out after 15 minutes; start it again")
                        self.kill()
                        return
                    continue
                data = os.read(self.fd, 4096)
            except OSError:
                data = b""
            if not data:
                break
            text = ANSI.sub("", data.decode(errors="replace")).replace("\r", "\n")
            buf = (buf + text)[-8000:]
            with self._lock:
                self.tail = "\n".join(l for l in buf.splitlines() if l.strip())[-1500:]
                if not self.url:
                    # the URL can wrap over several terminal lines: join the lines that continue it
                    joined = re.sub(r"\n(?=[A-Za-z0-9%&=_.~+/-]{8,}\n?)", "", buf)
                    m = URL.search(joined)
                    if m and ("oauth" in m.group(0) or "authorize" in m.group(0) or "login" in m.group(0)):
                        self.url = m.group(0).rstrip(".,)")
                if self.url and self.state == "starting":
                    self.state = "waiting_code"
        try:
            _, status = os.waitpid(self.pid, 0)
        except ChildProcessError:
            status = 0
        ok = auth_status(self.claude_bin, self.config_dir).get("loggedIn")
        if ok:
            self._finish("done")
        else:
            last = self.tail.strip().splitlines()[-1:] or ["claude auth login exited"]
            self._finish("failed", f"not logged in ({last[0][:200]})")

    def _finish(self, state: str, error: str = ""):
        with self._lock:
            if self.state not in ("done", "failed"):
                self.state, self.error = state, error

    def submit(self, code: str):
        code = code.strip()
        if not code or len(code) > 4096 or any(c in code for c in "\r\n\x03\x04"):
            raise ValueError("that doesn't look like a login code")
        with self._lock:
            if self.state not in ("waiting_code", "starting"):
                raise ValueError(f"the login is {self.state}, not waiting for a code")
            self.state = "checking"
        os.write(self.fd, code.encode() + b"\r")

    def kill(self):
        try:
            os.kill(self.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

    def view(self) -> dict:
        with self._lock:
            return {"state": self.state, "url": self.url, "error": self.error,
                    "age_s": round(time.time() - self.started)}


class AgentManager:
    """The registry of agents on this box, their child processes and their logins."""

    def __init__(self, cfg, primary_config_dir: str | None = None):
        self.cfg = cfg
        self.primary_dir = primary_config_dir or claude_home()
        # inside the claude-home volume, so an agent's login survives a new container
        self.base = os.environ.get("FARM_AGENTS_DIR") or os.path.join(self.primary_dir, "clodfarm-agents")
        self.registry = os.path.join(cfg.workspace, ".farm", "agents.json")
        self.logs = os.path.join(cfg.workspace, ".farm", "agents")
        self.procs: dict[str, subprocess.Popen] = {}
        self.logins: dict[str, LoginSession] = {}
        self._auth_cache: dict[str, tuple[float, dict]] = {}
        self.leaving: set[str] = set()  # being released: never (re)start these
        self._lock = threading.RLock()
        self.stopping = threading.Event()

    # ------------------------------------------------------------- registry
    def _load(self) -> list[dict]:
        try:
            return json.load(open(self.registry)).get("agents", [])
        except (OSError, ValueError):
            return []

    def _save(self, agents: list[dict]):
        os.makedirs(os.path.dirname(self.registry), exist_ok=True)
        tmp = self.registry + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"agents": agents}, f, indent=1)
        os.replace(tmp, self.registry)

    def primary(self) -> dict:
        return {"id": self.cfg.name, "name": self.cfg.name, "primary": True, "config_dir": self.primary_dir,
                "hat": "straw", "created": 0}

    def all(self) -> list[dict]:
        return [self.primary()] + self._load()

    def get(self, aid: str) -> dict | None:
        return next((a for a in self.all() if a["id"] == aid), None)

    def create(self, name: str) -> dict:
        name = (name or "").strip()[:24] or "Claude"
        with self._lock:
            agents = self._load()
            taken = {a["id"] for a in self.all()}
            aid = slug(name)
            while aid in taken:
                aid = f"{slug(name)[:19]}-{secrets.token_hex(2)}"
            if not ID_OK.match(aid):
                raise ValueError("pick a name with letters or digits")
            d = os.path.join(self.base, aid)
            os.makedirs(d, mode=0o700, exist_ok=True)
            agent = {"id": aid, "name": name, "primary": False, "config_dir": d,
                     "hat": HATS[(len(agents) + 1) % len(HATS)], "created": time.time()}
            self._save(agents + [agent])
        self.spawn(agent)
        return agent

    def farm_id(self, aid: str) -> str:
        return f"{aid}@{socket.gethostname()}"  # what its `clodfarm run` calls itself (Config.farm_id)

    def remove(self, aid: str):
        """Release an agent: out of the registry first (so keep_alive can't bring it back), then stop its
        `clodfarm run` (which hands its tasks back), then log it out and delete its login."""
        with self._lock:
            agent = self.get(aid)
            if not agent or agent.get("primary"):
                raise ValueError("the primary agent is the farm itself; log it out with `clodfarm logout`")
            self.leaving.add(aid)
            self._save([a for a in self._load() if a["id"] != aid])
        try:
            self.stop_proc(aid)
            s = self.logins.pop(aid, None)
            if s:
                s.kill()
            try:
                subprocess.run([self.cfg.claude_bin, "auth", "logout"], env=self.env_for(agent), capture_output=True,
                               timeout=60, stdin=subprocess.DEVNULL)
            except (OSError, subprocess.TimeoutExpired):
                pass  # its login is deleted below either way
            if os.path.realpath(agent["config_dir"]).startswith(os.path.realpath(self.base) + os.sep):
                shutil.rmtree(agent["config_dir"], ignore_errors=True)
            self._auth_cache.pop(aid, None)
        finally:
            self.leaving.discard(aid)

    # ------------------------------------------------------------ processes
    def env_for(self, agent: dict) -> dict:
        env = dict(os.environ)
        env["CLAUDE_CONFIG_DIR"] = agent["config_dir"]
        if not agent.get("primary"):  # its own login only, never the container's token or key
            for k in _CRED_ENV:
                env.pop(k, None)
            env.update(FARM_NAME=agent["id"], FARM_UI="0", FARM_CONTAINER_NAME=os.environ.get("FARM_CONTAINER_NAME", "clodfarm"))
        return env

    def spawn(self, agent: dict):
        if agent.get("primary") or self.stopping.is_set():
            return
        with self._lock:
            # the caller's copy may be stale: only start an agent that is still registered and not leaving
            if agent["id"] in self.leaving or all(a["id"] != agent["id"] for a in self._load()):
                return
            p = self.procs.get(agent["id"])
            if p and p.poll() is None:
                return
            os.makedirs(self.logs, exist_ok=True)
            log = open(os.path.join(self.logs, f"{agent['id']}.log"), "ab")
            self.procs[agent["id"]] = subprocess.Popen([sys.executable, "-m", "clodfarm", "run"], env=self.env_for(agent),
                                                       cwd=self.cfg.workspace, stdin=subprocess.DEVNULL, stdout=log,
                                                       stderr=subprocess.STDOUT, start_new_session=True)
            log.close()

    def stop_proc(self, aid: str, wait: float = 20):
        p = self.procs.pop(aid, None)
        if not p or p.poll() is not None:
            return
        try:
            os.killpg(p.pid, signal.SIGTERM)
            p.wait(wait)
        except (ProcessLookupError, PermissionError):
            pass
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    def keep_alive(self):
        """Start every added agent and restart any that exit (runs in a thread next to the UI)."""
        backoff: dict[str, float] = {}
        while not self.stopping.wait(5):
            for a in self._load():
                p = self.procs.get(a["id"])
                if p is None or p.poll() is not None:
                    if time.time() >= backoff.get(a["id"], 0):
                        self.spawn(a)
                        backoff[a["id"]] = time.time() + 30

    def shutdown(self):
        self.stopping.set()
        for aid in list(self.procs):
            self.stop_proc(aid)
        for s in self.logins.values():
            s.kill()

    def alive(self, aid: str) -> bool:
        if aid == self.cfg.name:
            return True
        p = self.procs.get(aid)
        return bool(p and p.poll() is None)

    # ---------------------------------------------------------------- login
    def auth(self, agent: dict, max_age: float = 20) -> dict:
        hit = self._auth_cache.get(agent["id"])
        s = self.logins.get(agent["id"])
        if hit and time.time() - hit[0] < max_age and not (s and s.state == "done" and not hit[1].get("loggedIn")):
            return hit[1]
        env_dir = None if agent.get("primary") else agent["config_dir"]
        st = auth_status(self.cfg.claude_bin, env_dir)
        self._auth_cache[agent["id"]] = (time.time(), st)
        return st

    def start_login(self, aid: str) -> LoginSession:
        agent = self.get(aid)
        if not agent:
            raise KeyError(aid)
        with self._lock:
            old = self.logins.get(aid)
            if old and old.state in ("starting", "waiting_code", "checking"):
                return old
            env = self.env_for(agent)
            env.setdefault("TERM", "xterm-256color")
            env["BROWSER"] = "/bin/true"  # nothing to open inside a container: the UI shows the URL instead
            s = LoginSession(self.cfg.claude_bin, agent["config_dir"], env)
            self.logins[aid] = s
            self._auth_cache.pop(aid, None)
            return s

    def login(self, aid: str) -> LoginSession | None:
        return self.logins.get(aid)
