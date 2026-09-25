"""The farm daemon: keeps Remote Control up and the workers busy.

    clodfarm run

Threads:
  * remote-control keeper  ``claude remote-control`` so you can drive the box
                           from the Claude app or claude.ai/code; restarted if it exits
  * worker 0..N-1          take a budget slot, claim a task, run a headless agent
  * main loop              reaps dead leases, heartbeats, prints a status line
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time

from . import gitops, notify, prompts
from .auth import accept_remote_control, auth_status, banner, install_guide, seat_id, trust_directory
from .config import Config, load
from .governor import Snapshot, decide
from .runner import build_cmd, run_agent
from .store import Store, iso, now


ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\]8;;[^\x07\x1b]*(?:\x07|\x1b\\)?")


class Farm:
    def __init__(self, cfg: Config, store: Store):
        self.cfg, self.store = cfg, store
        self.seat = cfg.seat or "default"  # the Claude account this box runs on; set from the login in wait_for_auth
        store.echo = True
        self.stop = threading.Event()
        self.procs: dict[str, subprocess.Popen] = {}
        self.no_mission_logged = False

    # ------------------------------------------------------------ lifecycle
    def run(self):
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, lambda *_: self.stop.set())
            signal.signal(signal.SIGINT, lambda *_: self.stop.set())
        self.wait_for_auth()
        self.ensure_table()
        gitops.ensure_repo(self.cfg.repo_dir, self.cfg.repo_url)
        if os.environ.get("FARM_MISSION") and not any(os.path.isfile(p) for p in self.cfg.mission_paths):
            gitops.write_mission(self.cfg.repo_dir, os.environ["FARM_MISSION"])
            self.store.event("mission.set", "MISSION.md written from FARM_MISSION")
        if self.cfg.manage_claude_config:
            if self.cfg.remote_control:
                accept_remote_control()
            install_guide()
            trust_directory(self.cfg.repo_dir)
            trust_directory(self.cfg.workspace)
        self.store.event("farm.started", f"{self.cfg.farm_id}: {self.cfg.policy.max_workers} workers, "
                         f"model {self.cfg.model}, remote control {'on' if self.cfg.remote_control else 'off'}, "
                         f"billing {'API key' if self.cfg.policy.api_mode else 'subscription'}")
        threads = []
        if self.cfg.remote_control:
            threads.append(threading.Thread(target=self.remote_control_loop, name="remote-control", daemon=True))
        for i in range(self.cfg.policy.max_workers):
            threads.append(threading.Thread(target=self.worker_loop, args=(i,), name=f"w{i}", daemon=True))
        for t in threads:
            t.start()
        last_status = 0.0
        while not self.stop.is_set():
            try:
                self.store.reap_expired()
                if now() - last_status > 600:
                    self.print_status()
                    last_status = now()
            except Exception as e:  # keep the farm alive through transient AWS errors
                print(f"housekeeping error: {e}", flush=True)
            self.stop.wait(60)
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
        raise SystemExit(0)

    def shutdown(self):
        """Stop cleanly: end the agents, hand this box's running tasks back to the queue (their attempt is given back)
        and free its slots right away, so another box, or this one after a restart, continues without waiting for
        leases to expire."""
        print("stopping: handing running tasks back to the queue", flush=True)
        for p in list(self.procs.values()):
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        time.sleep(3)
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

    def wait_for_auth(self):
        shown = 0.0
        while not self.stop.is_set():
            st = auth_status(self.cfg.claude_bin)
            if st.get("loggedIn"):
                self.seat = self.cfg.seat or seat_id(st)
                print(f"authenticated: {st.get('authMethod')} ({st.get('subscriptionType') or 'token'}), seat {self.seat}",
                      flush=True)
                return
            if now() - shown > 300:
                print(banner(self.cfg), flush=True)
                shown = now()
            self.stop.wait(5)
        raise SystemExit(0)

    def print_status(self):
        d = decide(self.store.get_snapshot(self.seat), self.cfg.policy, now(), self.store.spent_today(self.seat))
        counts = {s: self.store.count(s) for s in ("queued", "running", "waiting")}
        print(json.dumps({"at": iso(), "status": counts, "budget": d.to_dict()}), flush=True)

    # ------------------------------------------------------- remote control
    def remote_control_loop(self):
        backoff = 10
        while not self.stop.is_set():
            cmd =[self.cfg.claude_bin, "remote-control", "--name", self.cfg.name, "--spawn", self.cfg.rc_spawn,
                   "--capacity", str(self.cfg.rc_capacity), "--permission-mode", self.cfg.permission_mode]
            t0 = now()
            try:
                p = subprocess.Popen(cmd, cwd=self.cfg.repo_dir, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, bufsize=1, start_new_session=True)
            except FileNotFoundError:
                print("remote-control: claude binary not found", flush=True)
                return
            self.procs["remote-control"] = p
            self.store.event("rc.started", f"Remote Control session '{self.cfg.name}' starting (pid {p.pid})")
            seen, connected = set(), False
            for raw in p.stdout:
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
            p.wait()
            self.procs.pop("remote-control", None)
            if self.stop.is_set():
                return
            ran = now() - t0
            backoff = 10 if ran > 300 else min(backoff * 2, 1800)
            self.store.event("rc.exited", f"code {p.returncode} after {ran:.0f}s; restarting in {backoff}s")
            self.stop.wait(backoff)

    # --------------------------------------------------------------- workers
    def worker_loop(self, i: int):
        wid = f"w{i}"
        holder = f"{self.cfg.farm_id}/{wid}"
        os.environ.setdefault("FARM_FARM_ID", self.cfg.farm_id)
        while not self.stop.is_set():
            try:
                self.worker_step(wid, holder)
            except Exception as e:
                print(f"{wid}: error: {e!r}", flush=True)
                self.store.heartbeat(self.cfg.farm_id, wid, f"error: {e}"[:200], seat=self.seat)
                self.stop.wait(30)

    def worker_step(self, wid: str, holder: str):
        cfg, store = self.cfg, self.store
        ctl = store.control()
        if ctl.get("paused"):
            store.heartbeat(cfg.farm_id, wid, f"paused: {ctl.get('reason') or 'by hand'}", seat=self.seat)
            self.stop.wait(cfg.idle_sleep)
            return
        # this seat's own budget decides; other seats in the farm are paced separately
        d = decide(store.get_snapshot(self.seat), cfg.policy, now(), store.spent_today(self.seat))
        slot = store.acquire_slot(holder, d.workers, cfg.lease_seconds, self.seat) if d.workers else None
        if slot is None:
            wait = cfg.idle_sleep if not d.pause_until else max(10, min(300, d.pause_until - now()))
            store.heartbeat(cfg.farm_id, wid, f"throttled: {d.reason}", seat=self.seat)
            self.stop.wait(wait)
            return
        try:
            task = store.claim_next(holder, cfg.lease_seconds, cfg.farm_id, cfg.resume_affinity)
            if not task:
                self.maybe_plan()
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
            if slot is not None:
                store.release_slot(slot, holder)

    def maybe_plan(self):
        cfg, store = self.cfg, self.store
        if not cfg.planner or store.count("queued") > 0:
            return
        mission = next((open(p).read() for p in cfg.mission_paths if os.path.isfile(p)), None)
        if not mission:
            if not self.no_mission_logged:
                store.event("planner.no_mission", prompts.NO_MISSION)
                self.no_mission_logged = True
            return
        if any(t.get("kind") == "plan" for s in ("running", "waiting") for t in store.list_tasks(s)):
            return
        if store.planner_try_start(cfg.planner_cooldown):
            store.add_task("Plan the next tasks", "(planner: prompt is built at run time)", priority=9,
                           kind="plan", created_by="planner", max_depth=cfg.max_depth)

    def run_task(self, task: dict, wid: str, holder: str, slot: int):
        cfg, store = self.cfg, self.store
        tid = task["id"]
        store.heartbeat(cfg.farm_id, wid, "running", tid, seat=self.seat)
        use_git = gitops.is_repo(cfg.repo_dir)
        branch = None
        parent_branch = f"farm/{task['parent']}" if task.get("parent") else None
        if use_git and task.get("kind") != "plan":
            # a sub-task starts from its parent's branch, so it sees the parent's committed work
            cwd, branch = gitops.worktree_for(cfg.repo_dir, tid, parent_branch)
        else:
            cwd = cfg.repo_dir if use_git else cfg.workspace
        if cfg.manage_claude_config:
            trust_directory(cwd)

        resume = bool(task.get("resume"))
        session = task.get("session_id") if resume else None
        if task.get("kind") == "plan":
            mission = next((open(p).read() for p in cfg.mission_paths if os.path.isfile(p)), "")
            recent = [t for t in store.list_tasks("done", 15) + store.list_tasks("failed", 5) if t["id"] != tid]
            queue = store.list_tasks("queued") + store.list_tasks("running") + store.list_tasks("waiting")
            prompt = prompts.planner_prompt(cfg, mission, recent, [t for t in queue if t["id"] != tid])
        elif resume:
            reason = task.get("resume_reason")
            if reason == "verify":
                prompt = prompts.verify_prompt(cfg.verify_cmd, task.get("resume_note") or "")
            elif reason == "timeout":
                prompt = prompts.timeout_prompt(cfg.task_timeout)
            elif reason == "restart":
                prompt = prompts.restart_prompt()
            else:
                children = [store.get_task(c) for c in task.get("children", [])]
                for c in children:  # a child may have run on another box: get its branch from origin
                    if c and use_git:
                        gitops.fetch_branch(cfg.repo_dir, f"farm/{c['id']}")
                prompt = prompts.resume_prompt([c for c in children if c])
            if not session:
                prompt = task["prompt"] + "\n\n---\n" + prompt
        else:
            prompt = task["prompt"]
        if resume:
            store.mark_resumed(tid)

        env = {**os.environ, "FARM_TASK_ID": tid, "FARM_WORKER_ID": f"{cfg.farm_id}/{wid}"}
        sysprompt = prompts.task_system_prompt(cfg, task, cwd, branch)

        keep = threading.Event()

        def renew():
            while not keep.wait(max(20, cfg.lease_seconds // 3)):
                store.renew_lease(tid, holder, cfg.lease_seconds)
                store.renew_slot(slot, holder, cfg.lease_seconds)
                store.heartbeat(cfg.farm_id, wid, "running", tid, seat=self.seat)

        threading.Thread(target=renew, daemon=True).start()
        before = store.get_snapshot(self.seat)
        on_snap = lambda sn: store.put_snapshot(sn, self.seat)  # noqa: E731
        started = now()
        try:
            res = run_agent(build_cmd(cfg, sysprompt, session), prompt, cwd, env, cfg.task_timeout,
                            on_snapshot=on_snap, on_start=lambda p: self.procs.__setitem__(tid, p))
            if session and not res.ok and res.num_turns == 0 and "conversation" in res.text.lower():
                # session file gone (e.g. new container): start fresh with full context
                res = run_agent(build_cmd(cfg, sysprompt), task["prompt"] + "\n\n---\n" + prompt, cwd, env,
                                cfg.task_timeout, on_snapshot=on_snap, on_start=lambda p: self.procs.__setitem__(tid, p))
        finally:
            keep.set()
            self.procs.pop(tid, None)
        if self.stop.is_set():
            return  # stopping: shutdown() hands this task back to the queue

        after = res.snapshots[-1] if res.snapshots else None
        store.add_spend(res.cost_usd, self.seat)
        store.add_run(tid, {
            "worker": holder, "started": started, "duration_s": round(res.duration_s, 1), "ok": res.ok,
            "cost_usd_list_price": res.cost_usd, "turns": res.num_turns, "terminal_reason": res.terminal_reason,
            "output_tokens": (res.usage or {}).get("output_tokens"),
            "util_before": before.to_dict() if before else None, "util_after": after.to_dict() if after else None,
        })
        if res.session_id:
            store.update_task(tid, session_id=res.session_id, cwd=cwd, branch=branch, home=cfg.farm_id, seat=self.seat)

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
            store.event("budget.rejected", "subscription rate limit hit; workers pause until reset", task=tid)
            snap = store.get_snapshot(self.seat)
            self.notify(f"seat {self.seat} paused: usage limit", f"Claude reported a usage limit; this seat's agents wait until "
                        f"{iso(snap.resets_at) if snap and snap.resets_at else 'the reset'}.")
            return

        if res.timed_out and res.session_id and int(task.get("timeout_resumes_used", 0)) < cfg.timeout_resumes:
            # a timeout is often a big task mid-way: keep the work and the session, continue
            if store.requeue_resume(tid, holder, "timeout", "", "timeout_resumes_used"):
                return

        text = res.text
        if res.ok and branch:
            line, outcome = self.land(task, cwd, branch, parent_branch)
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
            self.notify("paused by circuit breaker", reason + ". Look at `clodfarm events` and `clodfarm task show "
                        f"{tid}`, fix the cause, then run `clodfarm resume`.")
        if final == "done" and branch:
            # a sub-task's branch stays until its parent has merged it
            gitops.remove_worktree(cfg.repo_dir, cwd, None if task.get("parent") else branch)
        if task.get("kind") == "plan":
            spawned = int((store.get_task(tid) or {}).get("spawned", 0))
            store.planner_backoff(spawned, cfg.planner_cooldown, cfg.planner_max_backoff)
            if not spawned and int(store.planner_state().get("idle_runs", 0)) == 1:
                self.notify("nothing left to do", "The planner found nothing useful to queue from MISSION.md: "
                            + res.text[-500:])

    def notify(self, title: str, text: str):
        notify.send(self.cfg.notify_url, title, text, self.cfg.name)


    def descendants(self, task: dict) -> list[str]:
        out, todo = [], list(task.get("children") or (self.store.get_task(task["id"]) or {}).get("children") or [])
        while todo:
            c = todo.pop()
            out.append(c)
            todo += (self.store.get_task(c) or {}).get("children") or []
        return out

    def land(self, task: dict, cwd: str, branch: str, parent_branch: str | None) -> tuple[str, str]:
        """Put a successful run's commits where they belong. Returns (a line for the task result, outcome):
        outcome is kept (stays on its branch), landed (on main), conflict or verify_failed."""
        cfg, store, tid = self.cfg, self.store, task["id"]
        cur = store.get_task(tid) or {}
        more_to_do = int(cur.get("children_open", 0)) > 0 or (
            cur.get("pending_review") and int(cur.get("resumes", 0)) < cfg.max_resumes)
        try:
            if more_to_do:
                gitops.commit_leftovers(cwd, branch)
                if cfg.push:
                    gitops.push_branch(cfg.repo_dir, branch)  # sub-tasks on other boxes branch from it
                return f"work kept on {branch}; sub-tasks branch from it and you merge them when resumed", "kept"
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
