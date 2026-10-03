"""The farm daemon: keeps this Claude reachable from the Claude app and runs the farm's sub-agents.

    clodfarm run

Threads:
  * remote-control keeper  ``claude remote-control``, so you talk to this Claude from the Claude app or
                           claude.ai/code; restarted if it exits
  * runner 0..N-1          take a budget slot, take the next sub-agent this account may run, run it headless
  * usage keeper           measures the account's real usage right after login and whenever no run has for a while
  * updater                keeps Claude Code on its newest release (FARM_CLAUDE_UPDATE; the farm's own Claude only)
  * mail                   watches this Claude's doorbell: flags new messages for its conversations and sub-agents
                           (their hooks deliver them) and hands --urgent ones to a running sub-agent at once
  * main loop              starts scheduled sub-agents, wakes someone for unread --wake messages, reaps dead leases,
                           prints a status line
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

from . import awsapps, boot, dashboards, feed, gitops, notify, planner, procs, prompts
from .auth import (accept_remote_control, auth_status, banner, claude_name, install_browser_mcp, install_commands,
                   install_guide,
                   install_hooks, install_messaging, install_model, seat_id, trust_directory)
from .config import primary_name_file
from .config import Config, load
from .governor import Snapshot, decide
from .runner import Live, RunHandle, build_cmd, kill_tree, run_agent, session_name, start_run
from .store import Store, iso, now


ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\]8;;[^\x07\x1b]*(?:\x07|\x1b\\)?")
RC_SPEC = 2  # how Remote Control is started (its stdin /dev/null): one started another way is replaced when idle
RC_IDLE = 900  # Remote Control is restarted on a new Claude Code only when no conversation was active for this long
DRAINED_MARK = "/tmp/clodfarm-drained" if os.path.exists("/.dockerenv") else "/nonexistent/clodfarm-drained"
CANCEL_POLL = 3  # seconds between two looks at a running sub-agent's status: a cancel stops it within about this long


class Farm:
    def __init__(self, cfg: Config, store: Store):
        self.cfg, self.store = cfg, store
        self.seat = cfg.seat or "default"  # the Claude account this box runs on; set from the login in wait_for_auth
        store.echo = True
        if cfg.bot:  # every Claude on the farm sees it's a bot, and on which model
            store.bot = {"bot": cfg.bot, "bot_via": cfg.bot_via or None, "bot_takes": cfg.bot_takes}
        self.stop = threading.Event()
        self.procs: dict[str, subprocess.Popen] = {}
        self.running: dict[str, Live | None] = {}  # the sub-agents running on this box (their stdin, when live)
        self.ui = None
        self.rc_version = ""  # the Claude Code version Remote Control is running
        self.rc_updating = False  # Remote Control was stopped to restart on a new version
        self.abandoned: set[str] = set()  # tasks whose run goes on past this process (handing over)
        self.handoff = False  # SIGHUP: exec the current release, leaving every run going (it adopts them)
        self.threads: list[threading.Thread] = []
        self.agents = None  # the farm's own daemon keeps the Claudes added in the UI running
        self.ui_keeper = None
        self._drain: tuple[float, dict] = (0.0, {})
        self.started = now()

    @property
    def runs(self) -> str:
        """Where this Claude's runs keep their files (see runshim.py)."""
        return procs.runs_dir(self.cfg.workspace, self.cfg.farm_id)

    def pidfile(self) -> str:
        return os.path.join(procs.pids_dir(self.cfg.workspace),
                            f"farmd-{os.environ.get('FARM_NAME') if os.environ.get('FARM_HATCHED') else 'primary'}.json")

    def write_pidfile(self, ready: bool):
        try:
            procs.write_json(self.pidfile(), {"pid": os.getpid(), "release": boot.running(), "ready": ready,
                                              "name": self.cfg.name, "at": now()})
        except OSError as e:
            print(f"pid file not written: {e}", flush=True)

    def _on_hup(self, *_):
        self.handoff = True
        self.stop.set()

    # ------------------------------------------------------------ lifecycle
    def run(self):
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, lambda *_: self.stop.set())
            signal.signal(signal.SIGINT, lambda *_: self.stop.set())
            signal.signal(signal.SIGHUP, self._on_hup)
        self.write_pidfile(ready=False)
        if not os.environ.get("FARM_HATCHED"):
            from .agents import AgentManager
            self.agents = AgentManager(self.cfg)
            threading.Thread(target=self.agents.keep_alive, name="agents", daemon=True).start()
            if self.cfg.ui:
                # its own process, up before the login, so the first agent can be hatched (logged in) from the browser
                from .uikeeper import UIKeeper
                self.ui_keeper = UIKeeper(self.cfg)
                threading.Thread(target=self.ui_keeper.keep, args=(self.stop,), name="ui", daemon=True).start()
        self.wait_for_auth()
        self.ensure_table()
        self.clear_stale_drain()
        gitops.ensure_repo(self.cfg.repo_dir, self.cfg.repo_url)
        if self.cfg.manage_claude_config:
            if self.cfg.remote_control:
                accept_remote_control()
            install_guide()
            try:  # the optional apps role (deploy/aws/apps-role.yaml): `aws --profile apps` for the Claudes
                if awsapps.install_profile():
                    apps = awsapps.settings()
                    print(f"aws: profile '{awsapps.PROFILE}' " + (f"-> {apps['role']}" if apps else "removed"), flush=True)
            except (OSError, ValueError) as e:
                print(f"aws: apps profile not written: {e}", flush=True)
            if awsapps.settings():  # the CLI the Claudes deploy with; in the background so startup isn't held up
                threading.Thread(target=self._ensure_aws_cli, name="aws-cli", daemon=True).start()
            install_hooks()
            install_commands()  # /farm-login: its person signs in to it on the farm UI from the Claude app
            install_messaging()
            install_model(self.cfg.model)
            # the farm's browser as the `browser` MCP tools (its own profiles), when the image has one
            install_browser_mcp(claude=self.cfg.name, unowned=not os.environ.get("FARM_HATCHED"))
            trust_directory(self.cfg.repo_dir)
            trust_directory(self.cfg.workspace)
        # this Claude's conversations (Remote Control and every session opened from it) inherit their mail flag
        os.makedirs(self.cfg.mail_dir, exist_ok=True)
        os.environ["FARM_MAIL_FLAG"] = os.path.join(self.cfg.mail_dir, self.cfg.name)
        self.store.event("farm.started", f"{self.cfg.farm_id}: {self.cfg.policy.max_workers} workers, "
                         f"model {self.cfg.model}, remote control {'on' if self.cfg.remote_control else 'off'}, "
                         f"billing {self.billing()}")
        threads = []
        if self.updates_claude():
            self.update_claude()  # before Remote Control starts, so it starts on the newest version
            threads.append(threading.Thread(target=self.update_loop, name="updater", daemon=True))
        if self.cfg.remote_control:
            threads.append(threading.Thread(target=self.remote_control_loop, name="remote-control", daemon=True))
        if self.cfg.usage_refresh > 0:
            threads.append(threading.Thread(target=self.usage_loop, name="usage", daemon=True))
        threads.append(threading.Thread(target=self.mail_loop, name="mail", daemon=True))
        for i in range(self.cfg.policy.max_workers):
            threads.append(threading.Thread(target=self.worker_loop, args=(i,), name=f"w{i}", daemon=True))
        for t in threads:
            t.start()
        self.threads = threads
        self.write_pidfile(ready=True)
        last_status = last_rc_check = 0.0
        while not self.stop.is_set():
            try:
                self.store.reap_expired()
                for t in self.store.fire_due(self.cfg.max_depth, self.cfg.max_attempts):
                    print(f"schedule: queued {t['id']} {t['title'][:80]}", flush=True)
                for d in dashboards.claim_due(self.store):  # live dashboards: run their code, off this loop
                    cwd = self.cfg.repo_dir if gitops.is_repo(self.cfg.repo_dir) else self.cfg.workspace
                    threading.Thread(target=dashboards.refresh, args=(self.store, d, cwd), name=f"dash-{d['slug']}",
                                     daemon=True).start()
                for tid in self.store.dispatch_wakes(self.cfg.mail_max_hops, self.cfg.mail_wakes_per_hour):
                    print(f"mail: {tid} started for an unread --wake message", flush=True)
                if not os.environ.get("FARM_HATCHED"):  # the farm's own daemon drives the planner (one per farm)
                    planner.tick(self.store, self.cfg, self.cfg.farm_id, self.cfg.name)
                self.ensure_relay()  # a bot on the relay: brought back if it died
                if now() - last_status > 600:
                    self.print_status()
                    last_status = now()
                if now() - last_rc_check > int(os.environ.get("FARM_RC_VERSION_CHECK", "300")):
                    self.restart_stale_rc()
                    last_rc_check = now()
                if self.drained():
                    self.store.event("farm.drained", f"{self.cfg.farm_id}: drained, exiting")
                    if os.path.exists("/.dockerenv"):  # gone with the container, kept across its restarts
                        open(DRAINED_MARK, "a").close()
                    break
            except Exception as e:  # keep the farm alive through transient AWS errors
                print(f"housekeeping error: {e}", flush=True)
            self.stop.wait(int(os.environ.get("FARM_TICK_SECONDS", "15")))  # schedules fire within a tick
        if self.handoff:
            self.hand_over()
        self.shutdown()

    def ensure_table(self):
        """Create the table on first start; wait while DynamoDB (e.g. the Local container) is still coming up."""
        delay = 2
        while not self.stop.is_set():
            try:
                if self.store.ensure_table():
                    print(f"created DynamoDB table {self.cfg.table}", flush=True)
                return
            except Exception as e:  # noqa: BLE001
                print(f"DynamoDB not reachable yet ({type(e).__name__}: {str(e)[:120]}); retrying in {delay}s", flush=True)
                self.stop.wait(delay)
                delay = min(delay * 2, 60)
        if self.handoff:  # SIGHUP while it waited (for its login, for the store): exec the new release, as always
            self.hand_over()
        raise SystemExit(0)

    def hand_over(self):
        """SIGHUP (`clodfarm upgrade`): exec the current release in this same process, leaving every run, Remote
        Control and added Claude running. The new code adopts them from their files (see runshim.py). A worker that
        is landing a finished run's work (merge, check) finishes that first; the others only read files."""
        wait = int(os.environ.get("FARM_HANDOFF_WAIT", "900"))
        print(f"handing over to release {os.path.basename(boot.target()) or 'image'}: runs keep going", flush=True)
        deadline = now() + wait
        for t in self.threads:
            if t.name.startswith("w"):
                t.join(max(0.0, deadline - now()))
        self.store.event("farm.handoff", f"{self.cfg.farm_id}: {boot.running()} -> "
                         f"{os.path.basename(boot.target()) or 'image'}; {len(self.running)} run(s) keep going")
        sys.stdout.flush()
        sys.stderr.flush()
        os.environ["CLODFARM_HANDOFF"] = "1"
        os.execv(sys.executable, boot.command(sys.argv[1:] or ["run"]))

    def denied_tools(self) -> list[str]:
        """The built-in tools this Claude's person turned off, for --disallowedTools (the hook covers the rest)."""
        from . import policy
        return policy.disallowed_flags(policy.load(os.environ.get("CLAUDE_CONFIG_DIR") or ""))

    def _run_names(self) -> list[str]:
        try:
            return sorted(os.listdir(self.runs))
        except OSError:
            return []

    # ----------------------------------------------------------------- drain
    def drain_state(self) -> dict:
        """`clodfarm drain`: this box stops taking sub-agents, finishes the ones it runs, and (with --exit) stops."""
        t, d = self._drain
        if now() - t > 10:
            try:
                d = self.store.box_control(socket.gethostname())
            except Exception:  # noqa: BLE001 - keep the last answer while the store is unreachable
                pass
            self._drain = (now(), d)
        return d

    def clear_stale_drain(self):
        """A drain left by the container this one replaced (same host name, `deploy.sh roll`): start fresh. A
        hand-over (SIGHUP) keeps it: the drain is this box's own."""
        if os.environ.get("CLODFARM_HANDOFF") or os.path.exists(DRAINED_MARK):
            return  # this same container, restarted by Docker after it drained: it stays drained until recreated
        try:
            d = self.store.box_control(socket.gethostname())
            if d.get("draining") and float(d.get("at") or 0) < self.started:
                self.store.set_draining(socket.gethostname(), False)
                self.store.event("farm.undrained", f"{self.cfg.farm_id}: a drain from before this start was cleared")
        except Exception as e:  # noqa: BLE001
            print(f"drain: {e!r}", flush=True)

    def drained(self) -> bool:
        d = self.drain_state()
        if not (d.get("draining") and d.get("exit")) or self.running:
            return False
        if self.agents and any(self.agents.alive(a["id"]) for a in self.agents._load()):
            return False  # the added Claudes stop first (each drains its own runs)
        busy = [s for s in self.store.sessions(self.cfg.name, 50) if s.get("kind") == "conversation"
                and s.get("busy") and not s.get("ended") and now() - float(s.get("last_at", 0)) < RC_IDLE]
        return not busy  # no conversation cut off mid-turn

    def shutdown(self):
        """Stop cleanly: end the agents, hand this box's running tasks back to the queue (their attempt is given back)
        and free its slots right away, so another box, or this one after a restart, continues without waiting for
        leases to expire."""
        print("stopping: handing running tasks back to the queue", flush=True)
        if self.agents:
            self.agents.shutdown()  # the agents added in the UI hand their tasks back too
        if self.ui_keeper:
            self.ui_keeper.shutdown()
        if not os.environ.get("FARM_HATCHED"):
            try:
                from .browser import Browsers
                Browsers(self.cfg.workspace).shutdown()  # Chromium saves its cookies on the way out
            except Exception as e:  # noqa: BLE001
                print(f"browser: not stopped cleanly: {e!r}", flush=True)
        handles = [RunHandle(os.path.join(self.runs, n)) for n in self._run_names()]
        for h in handles:
            if h.poll() is None:
                h.stop()
        for p in list(self.procs.values()):
            if not isinstance(p, RunHandle):
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
        t0 = now()
        while now() - t0 < 8 and any(h.poll() is None for h in handles):
            time.sleep(0.2)
        mine = f"{self.cfg.farm_id}/"
        try:
            for t in self.store.list_tasks("running", 200):
                if str(t.get("worker", "")).startswith(mine):
                    self.store.requeue_resume(t["id"], t["worker"], "restart", "", "restarts")
            for s in self.store.slots():
                if str(s.get("holder", "")).startswith(mine):
                    self.store.release_slot(s["SK"], s["holder"])
        except Exception as e:  # noqa: BLE001 - leases expire on their own if the store is unreachable
            print(f"could not hand tasks back ({e!r}); their leases will expire", flush=True)
        self.store.event("farm.stopped", self.cfg.farm_id)
        if not os.environ.get("FARM_HATCHED"):
            boot.mark_clean(self.cfg.workspace)  # a stop on purpose: not a crash for the release's crash guard

    def billing(self) -> str:
        if self.cfg.bot:
            return f"bot on {self.cfg.bot}" + (f" via {self.cfg.bot_via}" if self.cfg.bot_via else "")
        return "API key" if self.cfg.policy.api_mode else "subscription"

    def wait_for_auth(self):
        if self.cfg.bot:  # no Claude login: its provider answered when it was added (bots.check)
            self.seat = self.cfg.seat or f"bot-{self.cfg.name}"
            print(f"{self.billing()}, seat {self.seat}, claude {self.cfg.name} on farm {self.cfg.farm}", flush=True)
            while self.cfg.bot_dialect and not self.stop.is_set():
                try:
                    return self.ensure_relay()
                except (OSError, RuntimeError, ValueError) as e:
                    print(f"relay: {e}; trying again in 10s", flush=True)
                    self.stop.wait(10)
            if not self.stop.is_set():
                return
            if self.handoff:
                self.hand_over()
            raise SystemExit(0)
        shown = 0.0
        while not self.stop.is_set():
            st = auth_status(self.cfg.claude_bin)
            if st.get("loggedIn"):
                self.seat = self.cfg.seat or seat_id(st)
                self.name_claude(st)
                print(f"authenticated: {st.get('authMethod')} ({st.get('subscriptionType') or 'token'}), seat {self.seat}, "
                      f"claude {self.cfg.name} on farm {self.cfg.farm}", flush=True)
                return
            if now() - shown > 300:
                print(banner(self.cfg), flush=True)
                shown = now()
            self.stop.wait(5)
        if self.handoff:  # SIGHUP while it waited (for its login, for the store): exec the new release, as always
            self.hand_over()
        raise SystemExit(0)

    def ensure_relay(self):
        """A bot on the relay (relay.py): its relay is up, and every Claude Code this run starts talks to it."""
        if not self.cfg.bot_dialect:
            return
        from . import relay
        url, token = relay.ensure(self.cfg.workspace, self.cfg.name, dict(os.environ))
        if os.environ.get("ANTHROPIC_BASE_URL") != url:
            print(f"relay: {self.cfg.bot} via {self.cfg.bot_via} at {url}", flush=True)
        os.environ["ANTHROPIC_BASE_URL"], os.environ["ANTHROPIC_AUTH_TOKEN"] = url, token

    def run_cost(self, res) -> float:
        """What a run cost at list price: Claude Code's own figure, or for a bot on the relay its model's price."""
        if not self.cfg.bot_dialect:
            return res.cost_usd
        from . import prices
        return round(prices.cost(self.cfg.bot_provider, self.cfg.bot, res.usage, self.cfg.bot_price), 6)

    def name_claude(self, st: dict):
        """The farm's own Claude is named after the account logged in to it (matan), not after the farm (jestr).
        Everything it starts (Remote Control, sub-agents, hooks) inherits the name; a file keeps it for commands run
        from outside (`docker exec clodfarm clodfarm ...`)."""
        if os.environ.get("FARM_HATCHED") or os.environ.get("FARM_CLAUDE_NAME"):
            return
        name = claude_name(st) or self.cfg.farm
        try:
            taken = {a["id"] for a in json.load(open(os.path.join(self.cfg.workspace, ".farm", "agents.json"))).get("agents", [])}
        except (OSError, ValueError):
            taken = set()
        if name in taken:  # an added Claude already has that name
            name = f"{name}-{self.cfg.farm}"[:24]
        self.cfg.name = name
        os.environ["FARM_CLAUDE_NAME"] = name
        path = primary_name_file(self.cfg.workspace)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w") as f:
            json.dump({"farm": self.cfg.farm, "claude": name}, f)
        os.replace(path + ".tmp", path)

    def print_status(self):
        d = decide(self.store.get_snapshot(self.seat), self.cfg.policy, now(), self.store.spent_today(self.seat))
        counts = {s: self.store.count(s) for s in ("queued", "running", "waiting")}
        print(json.dumps({"at": iso(), "status": counts, "budget": d.to_dict()}), flush=True)

    def _ensure_aws_cli(self):
        try:
            v = awsapps.ensure_cli()
            if v:
                print(f"aws: installed {v} for the apps role", flush=True)
        except Exception as e:  # never take the farm down for this; the Claudes can install it themselves
            print(f"aws: CLI not installed ({e}); the apps role still works from boto3", flush=True)

    # ------------------------------------------------------- remote control
    def remote_control_loop(self):
        """Keep ``claude remote-control`` running. It runs under the run shim, so the phone conversations it holds
        outlive this process: a new release (or this one after a crash) finds it running and only reads its output."""
        backoff = 10
        rundir = os.path.join(self.runs, "remote-control")
        while not self.stop.is_set():
            h = RunHandle(rundir)
            meta = procs.read_json(os.path.join(rundir, "meta.json"))
            adopted = h.started() and h.poll() is None and meta.get("kind") == "rc"
            if adopted:
                t0, self.rc_version = float(meta.get("started") or now()), meta.get("version") or self.claude_version()
                print(f"remote-control: still running (pid {h.pid}), adopted", flush=True)
            else:
                # the session and every one you open from the app are marked [clodfarm], so they stand out in the app
                cmd = [self.cfg.claude_bin, "remote-control", "--name", session_name(self.cfg),
                       "--remote-control-session-name-prefix", session_name(self.cfg), "--spawn", self.cfg.rc_spawn,
                       "--capacity", str(self.cfg.rc_capacity), "--permission-mode", self.cfg.permission_mode]
                t0 = now()
                self.rc_version = self.claude_version()
                if not shutil.which(self.cfg.claude_bin):
                    print("remote-control: claude binary not found", flush=True)
                    return
                h = start_run(self.cfg.workspace, rundir, cmd, self.cfg.repo_dir, dict(os.environ),
                              meta={"kind": "rc", "started": t0, "version": self.rc_version, "spec": RC_SPEC},
                              merge_stderr=True, stdin_null=True)
                self.store.event("rc.started", f"Remote Control session '{self.cfg.name}' starting (pid {h.pid})")
            self.procs["remote-control"] = h
            seen, connected = set(), adopted
            out = os.path.join(rundir, "out.jsonl")
            pos = os.path.getsize(out) if adopted and os.path.exists(out) else 0
            buf = ""
            while True:
                try:
                    with open(out, "rb") as f:
                        f.seek(pos)
                        chunk = f.read()
                except FileNotFoundError:
                    chunk = b""
                pos += len(chunk)
                *lines, buf = (buf + chunk.decode(errors="replace")).split("\n")
                for raw in lines:
                    # its screen redraws: drop terminal escapes and print each distinct line once
                    line = ANSI.sub("", raw).replace("\x07", "").strip()
                    if not line or line in seen:
                        continue
                    seen.add(line)
                    print(f"[remote-control] {line}", flush=True)
                    url = re.search(r"https://claude\.ai/code/session_\w+", raw)
                    if url and not connected:
                        connected = True
                        self.store.event("rc.connected", f"Remote Control '{self.cfg.name}' is live: {url.group(0)}")
                if chunk:
                    continue
                if h.poll() is not None or self.stop.is_set():
                    break
                time.sleep(0.5)
            if self.stop.is_set():
                return  # it keeps running: the next release adopts it (or shutdown stops it)
            self.procs.pop("remote-control", None)
            code = h.poll()
            if self.rc_updating:  # stopped by restart_stale_rc: start again on the new version right away
                self.rc_updating = False
                continue
            ran = now() - t0
            backoff = 10 if ran > 300 else min(backoff * 2, 1800)
            self.store.event("rc.exited", f"code {code} after {ran:.0f}s; restarting in {backoff}s")
            self.stop.wait(backoff)

    def restart_stale_rc(self):
        """Remote Control keeps running the Claude Code it was started with. After an update, restart it on the new
        version, but only when no one has talked to this Claude for RC_IDLE seconds, so no conversation is cut off."""
        p = self.procs.get("remote-control")
        if not p or p.poll() is not None or not self.rc_version:
            return
        cur = self.claude_version()
        # started the 1.0.0-1.0.3 way (its stdin a closed pipe: the Claude app's NEW SESSION never reached it)
        old_spec = procs.read_json(os.path.join(self.runs, "remote-control", "meta.json")).get("spec") != RC_SPEC
        if not old_spec and (not cur or cur == self.rc_version):
            return
        if any(s.get("kind") == "conversation" and not s.get("ended") and now() - float(s.get("last_at", 0)) < RC_IDLE
               for s in self.store.sessions(self.cfg.name, 50)):
            return
        self.store.event("rc.updating", f"Remote Control '{self.cfg.name}': restarting " + (
            "so NEW SESSION from the Claude app works again" if old_spec else
            f"on Claude Code {cur} (was {self.rc_version})") + "; no conversation was active")
        self.rc_updating = True
        p.stop()

    # ------------------------------------------------------------ Claude Code
    def claude_version(self) -> str:
        try:
            out = subprocess.run([self.cfg.claude_bin, "--version"], capture_output=True, text=True, timeout=30,
                                 stdin=subprocess.DEVNULL).stdout.split()
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return out[0] if out else ""

    def updates_claude(self) -> bool:
        """Only the farm's own Claude updates Claude Code: the Claudes added in the UI run the same binary."""
        return self.cfg.claude_update > 0 and not os.environ.get("FARM_HATCHED")

    def update_claude(self):
        """Install the newest Claude Code release (the `latest` channel). The next sub-agent runs on it at once;
        Remote Control moves to it once no one is talking to it (restart_stale_rc)."""
        before = self.claude_version()
        try:
            p = subprocess.run([self.cfg.claude_bin, "install", "latest"], capture_output=True, text=True, timeout=600,
                               stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as e:
            print(f"claude update: {e!r}", flush=True)
            return
        if p.returncode:
            print(f"claude update failed (code {p.returncode}): {ANSI.sub('', p.stdout + p.stderr).strip()[-300:]}",
                  flush=True)
            return
        after = self.claude_version()
        if after and after != before:
            self.store.event("claude.updated", f"Claude Code {before or '?'} → {after}")

    def update_loop(self):
        while not self.stop.wait(self.cfg.claude_update):
            self.update_claude()

    # ----------------------------------------------------------------- usage
    def usage_loop(self):
        """Keep this account's 5-hour and 7-day usage fresh, so a new Claude shows its real budget within seconds and
        an idle one stays current. Every sub-agent run reports usage live; when none has for ``usage_refresh`` seconds,
        a one-word call asks Claude Code for a fresh report (it counts as a tiny bit of usage)."""
        while not self.stop.is_set():
            try:
                snap = self.store.get_snapshot(self.seat)
                stale = not snap or now() - snap.observed_at > self.cfg.usage_refresh
                if stale and set(self.procs) <= {"remote-control"} and not self.cfg.policy.api_mode:
                    self.measure_usage()
            except Exception as e:  # noqa: BLE001 - measuring must never take the farm down
                print(f"usage: {e!r}", flush=True)
            self.stop.wait(30)

    def measure_usage(self) -> bool:
        env = {**os.environ, "FARM_TASK_ID": "usage", "FARM_WORKER_ID": f"{self.cfg.farm_id}/usage"}
        try:
            res = run_agent(build_cmd(self.cfg, "Answer in one word.", name=session_name(self.cfg, "usage check")),
                            "Reply with: ok", self.cfg.workspace, env, 180,
                            on_snapshot=lambda sn: self.store.put_snapshot(sn, self.seat),
                            on_start=lambda p: self.procs.__setitem__("usage", p), workspace=self.cfg.workspace,
                            rundir=os.path.join(procs.runs_dir(self.cfg.workspace, "misc"), f"usage-{self.cfg.farm_id}"))
        finally:
            self.procs.pop("usage", None)
        self.store.add_spend(res.cost_usd, self.seat)
        self.store.add_tokens(self.store.token_counts(res.usage), self.cfg.name)
        self.store.put_tools(self.cfg.name, res.init, where="usage check", keep_newer=86400)
        if not res.snapshots:
            return False
        sn = res.snapshots[-1]
        pct = lambda w: "?" if not w else f"{w.utilization:.0%}"  # noqa: E731
        self.store.event("usage.measured", f"{self.cfg.name}: 5h {pct(sn.five_hour)} used, 7d {pct(sn.seven_day)} used")
        return True

    # ------------------------------------------------------------------ mail
    def mail_loop(self):
        """Every message for this Claude, or for a sub-agent running on this box, rings this Claude's doorbell in the
        store. Look at it every ``mail_poll`` seconds (and at every address every 30 s, in case a ring was missed):
        an --urgent message interrupts its running sub-agent now; for any other, touch the address's flag file, and
        the hooks of its sessions deliver it (after the next batch of tool calls, before the turn ends, or by waking an
        idle conversation)."""
        seen, swept = -1, 0.0
        while not self.stop.wait(self.cfg.mail_poll):
            try:
                n = self.store.bell(self.cfg.name)
                if n == seen and now() - swept < 30:
                    continue
                seen, swept = n, now()
                self.deliver_mail()
            except Exception as e:  # noqa: BLE001 - the mail must never take the farm down
                print(f"mail: {e!r}", flush=True)

    def deliver_mail(self):
        store = self.store
        for tid, live in list(self.running.items()):
            if live and store.unread(tid, urgent_only=True):
                msgs = store.claim(tid, f"urgent:{self.cfg.name}", urgent_only=True)
                if msgs and not live.interrupt(prompts.urgent_text(msgs)):
                    store.unclaim(msgs)  # it just finished: its next run (or a hook) gets them
        for addr in [self.cfg.name, *self.running]:
            if store.unread(addr):
                with open(os.path.join(self.cfg.mail_dir, addr), "a"):
                    pass

    # --------------------------------------------------------------- workers
    def worker_loop(self, i: int):
        wid = f"w{i}"
        holder = f"{self.cfg.farm_id}/{wid}"
        os.environ.setdefault("FARM_FARM_ID", self.cfg.farm_id)
        try:
            self.adopt(wid, holder)
        except Exception as e:  # noqa: BLE001 - a run it couldn't adopt is re-queued when its lease runs out
            print(f"{wid}: could not adopt its run: {e!r}", flush=True)
        while not self.stop.is_set():
            try:
                self.worker_step(wid, holder)
            except Exception as e:
                print(f"{wid}: error: {e!r}", flush=True)
                self.store.heartbeat(self.cfg.farm_id, wid, f"error: {e}"[:200], seat=self.seat)
                self.stop.wait(30)

    def adopt(self, wid: str, holder: str):
        """Take over the run this worker had going before the farm handed over to a new release (or crashed): it
        kept running under its shim. Its task still has to be this worker's; otherwise the run is stopped."""
        cfg, store = self.cfg, self.store
        for name in self._run_names():
            rundir = os.path.join(self.runs, name)
            meta = procs.read_json(os.path.join(rundir, "meta.json"))
            if meta.get("kind") != "task" or meta.get("wid") != wid:
                continue
            h, tid = RunHandle(rundir), meta.get("tid")
            cur = store.get_task(tid) or {}
            if cur.get("status") != "running" or cur.get("worker") != holder:
                if h.poll() is None:
                    print(f"{wid}: {tid} isn't this worker's any more ({cur.get('status', 'gone')}): stopping its run",
                          flush=True)
                    kill_tree(h)
                shutil.rmtree(rundir, ignore_errors=True)
                continue
            slot = meta.get("slot")
            store.renew_lease(tid, holder, cfg.lease_seconds)
            if slot and not store.renew_slot(slot, holder, cfg.lease_seconds):
                slot = store.acquire_slot(holder, cfg.policy.max_workers, cfg.lease_seconds, self.seat) or slot
            state = "running" if h.poll() is None else f"finished (code {h.poll()})"
            print(f"{wid}: adopted {tid} ({state}, pid {h.pid or '-'})", flush=True)
            store.event("task.adopted", f"{tid}: adopted by {holder} after a hand-over ({state})", task=tid)
            try:
                self.run_task(cur, wid, holder, slot, adopt=rundir)
            except Exception as e:  # a farm bug must not strand the task until its lease runs out
                store.finish(tid, holder, False, f"farm error: {e!r}", cfg.max_resumes)
                raise
            finally:
                if not self.stop.is_set():
                    self.clean_runs(tid)
                    if slot:
                        store.release_slot(slot, holder)

    def clean_runs(self, tid: str):
        for name in self._run_names():
            if name.startswith(f"{tid}-"):
                shutil.rmtree(os.path.join(self.runs, name), ignore_errors=True)

    def worker_step(self, wid: str, holder: str):
        cfg, store = self.cfg, self.store
        ctl = store.control()
        if ctl.get("paused"):
            store.heartbeat(cfg.farm_id, wid, f"paused: {ctl.get('reason') or 'by hand'}", seat=self.seat)
            self.stop.wait(cfg.idle_sleep)
            return
        if self.drain_state().get("draining"):
            store.heartbeat(cfg.farm_id, wid, "draining", seat=self.seat)
            self.stop.wait(cfg.idle_sleep)
            return
        # this seat's own budget decides; other seats in the farm are paced separately
        d = decide(store.get_snapshot(self.seat), cfg.policy, now(), store.spent_today(self.seat))
        slot = store.acquire_slot(holder, d.workers, cfg.lease_seconds, self.seat) if d.workers else None
        task = None
        if slot is None:
            wait = cfg.idle_sleep if not d.pause_until else max(10, min(300, d.pause_until - now()))
            store.heartbeat(cfg.farm_id, wid, f"throttled: {d.reason}", seat=self.seat)
            self.stop.wait(wait)
            return
        try:
            task = store.claim_next(holder, cfg.lease_seconds, cfg.farm_id, cfg.resume_affinity, agent=cfg.name,
                                    sent_only=bool(cfg.bot) and cfg.bot_takes != "any")
            if not task:
                store.heartbeat(cfg.farm_id, wid, "idle", seat=self.seat)
                store.release_slot(slot, holder)
                slot = None
                self.stop.wait(cfg.idle_sleep)
                return
            try:
                self.run_task(task, wid, holder, slot)
            except Exception as e:  # a farm bug must not strand the task until its lease runs out
                store.finish(task["id"], holder, False, f"farm error: {e!r}", cfg.max_resumes)
                raise
            finally:
                if not self.stop.is_set():
                    self.clean_runs(task["id"])
        finally:
            # handing over: the run goes on, and so does its slot (the next release adopts both)
            if slot is not None and not (task and task["id"] in self.abandoned):
                store.release_slot(slot, holder)

    def run_task(self, task: dict, wid: str, holder: str, slot, adopt: str | None = None):
        cfg, store = self.cfg, self.store
        tid = task["id"]
        store.heartbeat(cfg.farm_id, wid, "running", tid, seat=self.seat)
        if adopt:
            m = procs.read_json(os.path.join(adopt, "meta.json"))
            ctx = {k: m.get(k) for k in ("cwd", "branch", "parent_branch", "name", "sysprompt", "fresh_text",
                                         "session", "started", "before", "live", "run_started")}
            ctx["env"] = procs.read_json(os.path.join(adopt, "cmd.json")).get("env") or dict(os.environ)
            ctx["text"] = ""
        else:
            ctx = self.prepare(task, wid, holder)
            if ctx is None:
                return
        self.execute(task, wid, holder, slot, ctx, adopt)

    def prepare(self, task: dict, wid: str, holder: str) -> dict | None:
        """Everything a sub-agent's run starts from: its worktree, its prompt (and the one it would get fresh), its
        environment. Kept with the run, so a new release that adopts it knows all of it."""
        cfg, store = self.cfg, self.store
        tid = task["id"]
        resume = bool(task.get("resume"))
        session = task.get("session_id") if resume else None
        # messages left for it (a "mail" sub-agent: for its Claude) come with its prompt; they are kept on the task, so
        # a run that starts over (a retry, a lost session) sees them all again
        kind, addr = task.get("kind"), task.get("to") if task.get("kind") == "mail" else tid
        new = store.claim(addr, f"prompt:{tid}")
        mail = (task.get("mail") or []) + [{k: m.get(k) for k in ("id", "from", "reply", "text", "at", "hops", "person")}
                                           for m in new]
        if new:
            store.update_task(tid, mail=mail)
        if kind == "mail" and not mail:
            store.finish(tid, holder, True, "no messages were left to handle", cfg.max_resumes)
            return None
        use_git = gitops.is_repo(cfg.repo_dir)
        branch = None
        parent_branch = f"farm/{task['parent']}" if task.get("parent") else None
        if use_git:
            # a sub-agent starts from its parent's branch, so it sees the parent's committed work
            cwd, branch = gitops.worktree_for(cfg.repo_dir, tid, parent_branch)
        else:
            cwd = cfg.repo_dir if use_git else cfg.workspace
        if cfg.manage_claude_config:
            trust_directory(cwd)

        note = None  # what a resumed run is told
        if resume:
            reason = task.get("resume_reason")
            if reason == "verify":
                note = prompts.verify_prompt(cfg.verify_cmd, task.get("resume_note") or "")
            elif reason == "timeout":
                note = prompts.timeout_prompt(cfg.task_timeout)
            elif reason == "restart":
                note = prompts.restart_prompt()
            elif reason == "message":
                note = prompts.message_prompt()
            else:
                children = [store.get_task(c) for c in task.get("children", [])]
                for c in children:  # a child may have run on another box: get its branch from origin
                    if c and use_git:
                        gitops.fetch_branch(cfg.repo_dir, f"farm/{c['id']}")
                note = prompts.resume_prompt([c for c in children if c])
            store.mark_resumed(tid)

        def compose(fresh: bool) -> str:
            """A fresh session gets the whole task and every message; the same session only what is new."""
            parts = ([prompts.mail_task_prompt(addr) if kind == "mail" else task["prompt"]] if fresh else []) + \
                ([note] if note else [])
            shown = mail if fresh else new
            return "\n\n---\n".join(parts + ([prompts.mail_text(shown)] if shown else []))

        env = {**os.environ, "FARM_TASK_ID": tid, "FARM_WORKER_ID": f"{cfg.farm_id}/{wid}",
               "FARM_OWNER": task.get("owner") or cfg.name,  # its own sub-agents and messages speak for its Claude
               "FARM_MAIL_FLAG": os.path.join(cfg.mail_dir, tid),
               # a message it sends counts one more message-triggered run (the wake loop guard)
               "FARM_MAIL_HOPS": str(max([int(m.get("hops") or 0) + 1 for m in mail], default=0))}
        # its session name carries its id, so other Claudes find it in ListAgents and message it with SendMessage
        name = f"[clodfarm] {task.get('owner') or cfg.name} · {task['title'][:50]} · {tid}"
        sysprompt = prompts.task_system_prompt(cfg, task, cwd, branch, name)
        before = store.get_snapshot(self.seat)
        return {"cwd": cwd, "branch": branch, "parent_branch": parent_branch, "name": name, "sysprompt": sysprompt,
                "session": session, "text": compose(fresh=not session),
                "fresh_text": compose(fresh=True) if session else None, "started": now(),
                "before": before.to_dict() if before else None, "live": bool(cfg.live_stdin), "env": env,
                "new_mail": new}

    def execute(self, task: dict, wid: str, holder: str, slot, ctx: dict, adopt: str | None = None):
        cfg, store = self.cfg, self.store
        tid = task["id"]
        cwd, branch, parent_branch, name = ctx["cwd"], ctx["branch"], ctx["parent_branch"], ctx["name"]
        env, sysprompt, session = ctx["env"], ctx["sysprompt"], ctx["session"]
        started = float(ctx.get("started") or now())

        keep = threading.Event()

        def renew():
            while not keep.wait(max(20, cfg.lease_seconds // 3)):
                store.renew_lease(tid, holder, cfg.lease_seconds)
                store.renew_slot(slot, holder, cfg.lease_seconds)
                store.heartbeat(cfg.farm_id, wid, "running", tid, seat=self.seat)

        def watch():
            """Stop the run as soon as the task isn't this worker's any more: cancelled (from any box, the UI, MCP or
            a parent's cancel), or handed to another worker."""
            while not keep.wait(CANCEL_POLL):
                try:
                    cur = store.get_task(tid) or {}
                except Exception:  # noqa: BLE001 - the store is briefly unreachable: look again
                    continue
                if cur.get("status") != "running" or cur.get("worker") != holder:
                    p = self.procs.get(tid)
                    if p and p.poll() is None:
                        print(f"{wid}: {tid} is {cur.get('status', 'gone')}: stopping its run", flush=True)
                        kill_tree(p)
                    return

        threading.Thread(target=renew, daemon=True).start()
        threading.Thread(target=watch, daemon=True).start()
        before = ctx.get("before")
        on_snap = lambda sn: store.put_snapshot(sn, self.seat)  # noqa: E731

        def run(resume_session, text, rundir=None):
            live = Live() if ctx.get("live") else None
            self.running[tid] = live
            run_started = float(ctx.get("run_started") or now()) if rundir else now()
            ctx["run_started"] = run_started
            meta = {"kind": "task", "tid": tid, "wid": wid, "holder": holder, "slot": slot, "seat": self.seat,
                    **{k: ctx.get(k) for k in ("cwd", "branch", "parent_branch", "name", "sysprompt", "fresh_text",
                                               "started", "before", "live")},
                    "session": resume_session, "run_started": run_started}
            adopt_it, rundir = bool(rundir), rundir or os.path.join(self.runs, f"{tid}-{time.time_ns()}")
            # the live feed: what it thinks, says and does (an adopted run carries on where its feed got to)
            on_line = feed.recorder(store, rundir, cfg.name, tid, task.get("owner") or cfg.name)
            return run_agent(build_cmd(cfg, sysprompt, resume_session, name, live=bool(live),
                                       disallowed=self.denied_tools()), text, cwd, env,
                             cfg.task_timeout, on_snapshot=on_snap, on_start=lambda p: self.procs.__setitem__(tid, p),
                             on_line=on_line, live=live, workspace=cfg.workspace, meta=meta, stop=self.stop,
                             adopt=adopt_it, started=run_started, rundir=rundir)
        abandoned = False
        try:
            res = run(session, ctx["text"], adopt)
            abandoned = res.abandoned
            if not abandoned and session and not res.ok and res.num_turns == 0 and \
                    "conversation" in res.text.lower() and ctx.get("fresh_text"):
                # session file gone (e.g. new container): start fresh with full context
                self.clean_runs(tid)
                ctx["run_started"] = None
                res = run(None, ctx["fresh_text"])
                abandoned = res.abandoned
        finally:
            keep.set()
            self.procs.pop(tid, None)
            self.running.pop(tid, None)
            if abandoned:
                self.abandoned.add(tid)
            else:
                try:
                    os.remove(os.path.join(cfg.mail_dir, tid))
                except OSError:
                    pass
        if abandoned or self.stop.is_set():
            return  # handing over: the run goes on and the next release adopts it; stopping: shutdown() hands it back

        after = res.snapshots[-1] if res.snapshots else None
        cost = self.run_cost(res)
        store.add_spend(cost, self.seat)
        store.add_tokens(store.token_counts(res.usage), cfg.name)
        store.put_tools(cfg.name, res.init)  # what this Claude can use, as its last sub-agent saw it
        store.add_run(tid, {
            "worker": holder, "started": started, "duration_s": round(res.duration_s, 1), "ok": res.ok,
            "cost_usd_list_price": cost, "turns": res.num_turns, "terminal_reason": res.terminal_reason,
            "output_tokens": (res.usage or {}).get("output_tokens"), "tokens": store.token_counts(res.usage),
            "util_before": before, "util_after": after.to_dict() if after else None,
        })
        if res.session_id:
            store.update_task(tid, session_id=res.session_id, cwd=cwd, branch=branch, home=cfg.farm_id, seat=self.seat)
            # the hook records its conversation; this makes sure the session is registered even without the hook
            store.record_session(res.session_id, claude=task.get("owner") or cfg.name, runs_on=cfg.name,
                                 kind="sub-agent", task=tid, box=cfg.farm_id, cwd=cwd, title=task["title"][:120])
        if not self.holds(tid, holder):
            # cancelled while it ran (or handed to another worker): it was stopped, and nothing it did lands; it
            # doesn't count as a failure either (the circuit breaker is for runs that break)
            store.event("task.stopped", f"{tid}: its run was stopped ({(store.get_task(tid) or {}).get('status', 'gone')});"
                        " nothing lands", task=tid)
            return

        if res.rate_limited and not res.ok:
            # not the task's fault: give the attempt back and wait for the window
            if not any(sn.status == "rejected" for sn in res.snapshots):
                # no rate_limit_event said so: record a short pause, the next run measures again
                store.put_snapshot(Snapshot(observed_at=now(), status="rejected", rate_limit_type="unknown",
                                            resets_at=now() + 900,
                                            five_hour=after.five_hour if after else None,
                                            seven_day=after.seven_day if after else None), self.seat)
            store.update_task(tid, attempts=max(0, int(task.get("attempts", 1)) - 1))
            store.finish(tid, holder, False, "rate limited; re-queued", cfg.max_resumes)
            who = f"{cfg.bot_via or 'its provider'} rate-limited bot {cfg.name}" if cfg.bot else "Claude reported a usage limit"
            store.event("budget.rejected", f"{who}; workers pause until reset" if cfg.bot else
                        "subscription rate limit hit; workers pause until reset", task=tid)
            snap = store.get_snapshot(self.seat)
            self.notify(f"seat {self.seat} paused: usage limit", f"{who}; this seat's agents wait until "
                        f"{iso(snap.resets_at) if snap and snap.resets_at else 'the reset'}.")
            return

        if res.timed_out and res.session_id and int(task.get("timeout_resumes_used", 0)) < cfg.timeout_resumes:
            # a timeout is often a big task mid-way: keep the work and the session, continue
            if store.requeue_resume(tid, holder, "timeout", "", "timeout_resumes_used"):
                return

        text = res.text
        if res.ok and branch:
            line, outcome = self.land(task, cwd, branch, parent_branch, holder)
            if outcome == "stopped":
                store.event("task.stopped", f"{tid}: cancelled before its work landed; nothing lands", task=tid)
                return
            if outcome == "verify_failed":
                fresh = store.get_task(tid) or task
                if res.session_id and int(fresh.get("verify_fixes_used", 0)) < cfg.verify_fixes and \
                        store.requeue_resume(tid, holder, "verify", line, "verify_fixes_used"):
                    return
                res.ok = False
                store.update_task(tid, attempts=int(fresh.get("max_attempts", 3)))  # fail now; don't retry from scratch
                line = f"`{cfg.verify_cmd}` still fails after {cfg.verify_fixes} fix attempt(s); not landed. " \
                       f"The branch {branch} is kept for a human.\n{line[-2000:]}"
            text += "\n\n[git] " + line
        final = store.finish(tid, holder, res.ok, text, cfg.max_resumes)
        try:
            self.after_run(task, final, res, text, cwd, branch)
        except Exception as e:  # noqa: BLE001 - bookkeeping after a finished run must never re-open the task
            print(f"after-run bookkeeping for {tid} failed: {e!r}", flush=True)

    def after_run(self, task: dict, final: str, res, text: str, cwd: str, branch: str | None):
        cfg, store, tid = self.cfg, self.store, task["id"]
        failures = store.record_health(final != "failed" and res.ok)
        if final == "failed":
            self.notify(f"task failed: {task['title'][:80]}", f"{tid}: {text[-600:]}")
        if cfg.stall_threshold and failures >= cfg.stall_threshold and not store.control().get("paused"):
            reason = f"circuit breaker: {failures} failed runs in a row (last: {tid} {task['title'][:60]})"
            store.set_paused(True, reason, by="farm")
            self.notify("paused by circuit breaker", reason + ". Look at `clodfarm events` and `clodfarm result "
                        f"{tid}`, fix the cause, then run `clodfarm resume`.")
        if final == "done" and branch:
            # a sub-agent's branch stays until its parent has merged it
            gitops.remove_worktree(cfg.repo_dir, cwd, None if task.get("parent") else branch)

    def notify(self, title: str, text: str):
        notify.send(self.cfg.notify_url, title, text, self.cfg.name)


    def descendants(self, task: dict) -> list[str]:
        out, todo = [], list(task.get("children") or (self.store.get_task(task["id"]) or {}).get("children") or [])
        while todo:
            c = todo.pop()
            out.append(c)
            todo += (self.store.get_task(c) or {}).get("children") or []
        return out

    def holds(self, tid: str, holder: str) -> bool:
        cur = self.store.get_task(tid) or {}
        return cur.get("status") == "running" and cur.get("worker") == holder

    def land(self, task: dict, cwd: str, branch: str, parent_branch: str | None, holder: str = "") -> tuple[str, str]:
        """Put a successful run's commits where they belong. Returns (a line for the task result, outcome):
        outcome is kept (stays on its branch), landed (on main), conflict, verify_failed or stopped (cancelled
        meanwhile: not merged)."""
        cfg, store, tid = self.cfg, self.store, task["id"]
        cur = store.get_task(tid) or {}
        more_to_do = int(cur.get("children_open", 0)) > 0 or (
            cur.get("pending_review") and int(cur.get("resumes", 0)) < cfg.max_resumes)
        try:
            if more_to_do:
                gitops.commit_leftovers(cwd, branch)
                if cfg.push:
                    gitops.push_branch(cfg.repo_dir, branch)  # sub-agents on other boxes branch from it
                return f"work kept on {branch}; sub-agents branch from it and you merge them when resumed", "kept"
            if parent_branch:
                gitops.commit_leftovers(cwd, branch)
                n = gitops.ahead_of(cfg.repo_dir, branch, parent_branch)
                shared = cfg.push and gitops.push_branch(cfg.repo_dir, branch)
                return f"{n} commit(s) on {branch}, left for the parent task to merge" + ("; pushed" if shared else ""), "kept"
            if cfg.verify_cmd:
                # check exactly what would land: the branch rebased onto current main
                gitops.rebase_onto_main(cfg.repo_dir, cwd, branch)
                passed, output = gitops.run_check(cwd, cfg.verify_cmd, cfg.verify_timeout)
                store.event("verify.passed" if passed else "verify.failed", f"{tid}: `{cfg.verify_cmd}`", task=tid)
                if not passed:
                    return output, "verify_failed"
            if holder and not self.holds(tid, holder):  # cancelled while the check ran
                return f"cancelled before landing: {branch} not merged", "stopped"
            out = gitops.merge(cfg.repo_dir, cwd, branch, push=cfg.push)
            for d in self.descendants(task):  # their work is in main now, via this task
                gitops.remove_worktree(cfg.repo_dir, os.path.join(os.path.dirname(cfg.repo_dir), ".worktrees", d),
                                       f"farm/{d}")
                if cfg.push:
                    gitops.delete_remote_branch(cfg.repo_dir, f"farm/{d}")
            if cfg.push:
                gitops.delete_remote_branch(cfg.repo_dir, branch)
            gitops.prune_merged(cfg.repo_dir)
            return out + ("; check passed" if cfg.verify_cmd else ""), "landed"
        except gitops.GitError as e:
            if task.get("kind") == "conflict":
                # never chain conflict tasks: a second failure needs a human
                self.notify("merge conflict needs you", f"{tid} ({task['title'][:80]}) could not land either: {str(e)[:500]}")
                return str(e), "conflict"
            store.add_task(f"Resolve merge conflict from task {tid}",
                           f"Task {tid} ({task['title']}) finished, but its branch {branch} conflicts with "
                           f"main. In your worktree run `git merge {branch}`, resolve the conflicts so both "
                           f"sides' intent is kept, run the tests, and commit.", priority=8,
                           kind="conflict", created_by=tid, max_depth=99)
            self.notify("merge conflict", f"{tid} ({task['title'][:80]}) conflicts with main; a resolve task was queued.")
            return str(e), "conflict"


def main():
    cfg = load()
    Farm(cfg, Store.from_config(cfg)).run()


if __name__ == "__main__":
    main()
