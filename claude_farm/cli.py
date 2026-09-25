"""claude-farm command line. Humans and agents use the same commands.

    claude-farm run                      start the farm daemon (the container does this)
    claude-farm login | logout | whoami  Claude subscription login (see docs/auth.md)
    claude-farm status                   workers, queue, budget in one screen
    claude-farm budget [--refresh]       subscription usage and what the governor allows
    claude-farm task add TITLE [--prompt TEXT | --prompt-file F | -] [--parent ID] [--priority 0-9]
    claude-farm task list [--status S] | show ID | cancel ID | retry ID
    claude-farm events [-n 30] [-f]      the farm's event log
    claude-farm pause [REASON] | resume  stop or restart new work on every farm sharing the table
    claude-farm init                     create the DynamoDB table
    claude-farm doctor                   check claude, login, DynamoDB, git and the workspace

Add --json to status, budget, task list/show and events for machine-readable output.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time

from . import __version__
from .auth import auth_status
from .config import load
from .governor import decide
from .store import Store, iso, now


def _store(cfg):
    return Store.from_config(cfg)


def _ago(ts) -> str:
    if not ts:
        return "-"
    s = max(0, now() - float(ts))
    return f"{s:.0f}s" if s < 90 else f"{s / 60:.0f}m" if s < 5400 else f"{s / 3600:.1f}h" if s < 172800 else f"{s / 86400:.1f}d"


def _until(ts) -> str:
    if not ts:
        return "-"
    left = max(0.0, float(ts) - now())
    rel = f"{left / 60:.0f}m" if left < 5400 else f"{left / 3600:.1f}h"
    return dt.datetime.fromtimestamp(float(ts), dt.timezone.utc).strftime("%a %H:%MZ") + f" (in {rel})"


def _pct(x) -> str:
    return "-" if x is None else f"{x:.0%}"


def _out(obj, as_json: bool, text: str):
    print(json.dumps(obj, indent=1, default=str) if as_json else text)


# ---------------------------------------------------------------- commands
def cmd_login(cfg, a):
    st = auth_status(cfg.claude_bin)
    if st.get("loggedIn") and not a.force:
        print(f"Already logged in: {st.get('email') or ''} {st.get('subscriptionType') or ''} via {st.get('via')}.")
        print("Use `claude-farm login --force` to switch accounts.")
        return 0
    print("Starting Claude Code login. Open the URL it prints on any device, approve, and paste the code here.\n")
    rc = subprocess.call([cfg.claude_bin, "auth", "login"])
    st = auth_status(cfg.claude_bin)
    if st.get("loggedIn"):
        print(f"\nLogged in: {st.get('email') or ''} ({st.get('subscriptionType') or 'subscription'}). "
              "The farm picks this up within a few seconds.")
        return 0
    print("\nNot logged in yet. Run `claude-farm login` again, or see docs/auth.md for the token option.")
    return rc or 1


def cmd_logout(cfg, a):
    return subprocess.call([cfg.claude_bin, "auth", "logout"])


def cmd_whoami(cfg, a):
    st = auth_status(cfg.claude_bin)
    keep = {k: st.get(k) for k in ("loggedIn", "via", "authMethod", "subscriptionType", "email", "orgName", "error") if st.get(k) is not None}
    _out(keep, a.json, "\n".join(f"{k}: {v}" for k, v in keep.items()))
    return 0 if st.get("loggedIn") else 1


def _budget(cfg, store):
    snap = store.get_snapshot()
    d = decide(snap, cfg.policy, now(), store.spent_today())
    return snap, d


def _budget_text(cfg, snap, d, slots) -> str:
    p = cfg.policy
    if p.api_mode:
        cap = f"${p.daily_budget_usd:.2f}/day" if p.daily_budget_usd else "no daily cap (set FARM_DAILY_BUDGET_USD)"
        return "\n".join([f"BUDGET (API key, list-price spend): ${d.details.get('spent_today_usd', 0):.2f} today, {cap}",
                          f"GOVERNOR  {d.workers}/{p.max_workers} agents allowed now: {d.reason}",
                          f"  slots in use: {len(slots)}"])
    lines = ["BUDGET (account-wide, from Claude Code's own rate-limit reports)"]
    if not snap:
        lines.append("  no usage report yet: the first agent run will measure it (or run `claude-farm budget --refresh`)")
    else:
        for name, w, cap in (("5-hour", snap.five_hour, p.five_hour_ceiling), ("7-day", snap.seven_day, p.weekly_target)):
            if w:
                lines.append(f"  {name:<7} {_pct(w.utilization):>5} used   limit for agents {cap:.0%}   resets {_until(w.resets_at)}")
        lines.append(f"  status  {snap.status}{'  (paid overage in use)' if snap.using_overage else ''}   measured {_ago(snap.observed_at)} ago")
    lines.append(f"GOVERNOR  {d.workers}/{p.max_workers} agents allowed now: {d.reason}")
    if d.pause_until:
        lines.append(f"  next check {_until(d.pause_until)}")
    lines.append(f"  slots in use: {len(slots)}")
    return "\n".join(lines)


def cmd_budget(cfg, a):
    store = _store(cfg)
    if a.refresh:
        from .runner import build_cmd, run_agent
        res = run_agent(build_cmd(cfg, "Answer in one word."), "Reply with: ok", cfg.workspace
                        if os.path.isdir(cfg.workspace) else os.getcwd(), dict(os.environ), 180,
                        on_snapshot=store.put_snapshot)
        if not res.snapshots:
            print(f"no rate-limit report received ({res.text[:200]})", file=sys.stderr)
    snap, d = _budget(cfg, store)
    slots = store.slots()
    _out({"snapshot": snap.to_dict() if snap else None, "decision": d.to_dict(), "policy": vars(cfg.policy),
          "slots": slots}, a.json, _budget_text(cfg, snap, d, slots))
    return 0


def _task_line(t) -> str:
    extra = f" <- {t['parent']}" if t.get("parent") else ""
    who = f" [{t.get('worker', '').split('/')[-1]}]" if t.get("status") == "running" else ""
    return f"  {t['id']}  {t['status']:<9} p{t.get('priority', 5)}  {_ago(t.get('updated')):>5}  {t['title'][:70]}{extra}{who}"


def cmd_status(cfg, a):
    store = _store(cfg)
    snap, d = _budget(cfg, store)
    counts = {s: store.count(s) for s in ("queued", "running", "waiting", "done", "failed")}
    workers = store.workers()
    ctl = store.control()
    if a.json:
        _out({"farm": cfg.farm_id, "paused": ctl, "counts": counts, "workers": workers, "decision": d.to_dict(),
              "snapshot": snap.to_dict() if snap else None}, True, "")
        return 0
    print(f"claude-farm {__version__}  farm {cfg.farm_id}  table {cfg.table}"
          + (f"  PAUSED: {ctl.get('reason') or 'by hand'}" if ctl.get("paused") else ""))
    print(_budget_text(cfg, snap, d, store.slots()))
    print("QUEUE     " + "  ".join(f"{k} {v}" for k, v in counts.items()))
    print("WORKERS")
    for w in sorted(workers, key=lambda w: w["SK"]):
        print(f"  {w['SK']:<40} {_ago(w.get('at')):>5}  {w.get('state', '')}{'  ' + w['task'] if w.get('task') else ''}")
    if not workers:
        print("  (none: is `claude-farm run` up?)")
    active = store.list_tasks("running") + store.list_tasks("waiting") + store.list_tasks("queued", 10)
    if active:
        print("TASKS")
        for t in active:
            print(_task_line(t))
    return 0


def _read_prompt(a) -> str:
    if a.prompt_file:
        return open(a.prompt_file).read() if a.prompt_file != "-" else sys.stdin.read()
    if a.prompt == "-":
        return sys.stdin.read()
    return a.prompt or a.title


def cmd_task(cfg, a):
    store = _store(cfg)
    if a.sub == "add":
        parent = a.parent or None
        by = os.environ.get("FARM_TASK_ID") or "human"
        try:
            if store.count("queued") >= cfg.max_queue and by != "human":
                print(f"queue is full ({cfg.max_queue} queued): not adding. Finish or cancel work first.", file=sys.stderr)
                return 3
            t = store.add_task(a.title, _read_prompt(a), priority=a.priority, parent=parent, created_by=by,
                               max_depth=cfg.max_depth, max_attempts=cfg.max_attempts)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        _out(t, a.json, f"queued {t['id']}: {t['title']}" + (f" (sub-task of {parent})" if parent else ""))
        return 0
    if a.sub == "list":
        ts = store.list_tasks(a.status, a.limit)
        _out(ts, a.json, "\n".join(_task_line(t) for t in ts) or "(no tasks)")
        return 0
    if a.sub == "show":
        t = store.get_task(a.id)
        if not t:
            print("no such task", file=sys.stderr)
            return 1
        t["runs"] = store.runs(a.id)
        if a.json:
            _out(t, True, "")
            return 0
        print(f"{t['id']}  {t['status']}  priority {t.get('priority')}  depth {t.get('depth', 0)}  attempts {t.get('attempts', 0)}")
        print(f"title:   {t['title']}")
        for k in ("parent", "children", "worker", "branch", "session_id"):
            if t.get(k):
                print(f"{k + ':':<9}{t[k]}")
        print(f"\nprompt:\n{t['prompt']}\n")
        if t.get("result"):
            print(f"result:\n{t['result']}\n")
        for r in t["runs"]:
            print(f"run {r['SK'][4:24]}  ok={r.get('ok')}  {r.get('duration_s')}s  turns={r.get('turns')}  "
                  f"out_tokens={r.get('output_tokens')}  list-price ${r.get('cost_usd_list_price', 0):.2f}")
        return 0
    if a.sub == "cancel":
        ok = store.cancel(a.id)
        print("cancelled" if ok else "not cancelled (already finished?)")
        return 0 if ok else 1
    if a.sub == "retry":
        ok = store.retry(a.id)
        print("re-queued" if ok else "not re-queued (still open?)")
        return 0 if ok else 1
    return 1


def cmd_events(cfg, a):
    store = _store(cfg)
    since = now() - 86400 * 7
    shown = set()

    def emit(evs):
        for e in evs:
            if e["SK"] in shown:
                continue
            shown.add(e["SK"])
            if a.json:
                print(json.dumps(e, default=str))
            else:
                print(f"{iso(e['at'])[5:16].replace('T', ' ')}  {e['type']:<16} {e.get('by', ''):<28} {e['msg'][:160]}")

    emit(store.events(since, a.n))
    while a.follow:
        time.sleep(3)
        emit(store.events(now() - 120, 200))
    return 0


def cmd_pause(cfg, a):
    _store(cfg).set_paused(True, " ".join(a.reason) or "paused by hand")
    print("paused: running agents finish their current run, no new ones start")
    return 0


def cmd_resume(cfg, a):
    _store(cfg).set_paused(False, "resumed")
    print("resumed")
    return 0


def cmd_init(cfg, a):
    created = _store(cfg).ensure_table()
    print(f"table {cfg.table}: {'created' if created else 'already exists'}")
    return 0


def cmd_doctor(cfg, a):
    ok = True

    def check(name, good, detail=""):
        nonlocal ok
        ok &= bool(good)
        print(f"  {'ok  ' if good else 'FAIL'}  {name}{': ' + detail if detail else ''}")

    print("claude-farm doctor")
    exe = shutil.which(cfg.claude_bin)
    ver = subprocess.run([cfg.claude_bin, "--version"], capture_output=True, text=True).stdout.strip() if exe else ""
    check("claude binary", exe, ver or "not found")
    st = auth_status(cfg.claude_bin)
    check("subscription login", st.get("loggedIn"), st.get("via") or st.get("error") or "run `claude-farm login`")
    try:
        store = _store(cfg)
        store.client.describe_table(TableName=cfg.table)
        check("DynamoDB table", True, f"{cfg.table} ({cfg.endpoint or cfg.region})")
    except Exception as e:  # noqa: BLE001
        check("DynamoDB table", False, f"{cfg.table}: {str(e)[:160]} (run `claude-farm init`)")
    check("git", shutil.which("git"))
    check("workspace", os.path.isdir(cfg.workspace), cfg.workspace)
    mission = next((p for p in cfg.mission_paths if os.path.isfile(p)), None)
    check("MISSION.md", mission or not cfg.planner, mission or "missing: the planner has nothing to plan from")
    return 0 if ok else 1


def cmd_run(cfg, a):
    from .supervisor import Farm
    Farm(cfg, _store(cfg)).run()
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="claude-farm", description="Always-on Claude Code agents on your subscription.",
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("--version", action="version", version=f"claude-farm {__version__}")
    sp = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        q = sp.add_parser(name, help=help_)
        q.add_argument("--json", action="store_true", help="machine-readable output")
        q.set_defaults(fn=fn)
        return q

    add("run", cmd_run, "start the farm daemon")
    add("login", cmd_login, "log in to your Claude subscription").add_argument("--force", action="store_true")
    add("logout", cmd_logout, "log out")
    add("whoami", cmd_whoami, "show the login in use")
    add("status", cmd_status, "workers, queue and budget")
    add("budget", cmd_budget, "subscription usage and governor decision").add_argument(
        "--refresh", action="store_true", help="run a tiny agent call to measure usage now")
    t = add("task", cmd_task, "add, list, show, cancel or retry tasks")
    ts = t.add_subparsers(dest="sub", required=True)
    ta = ts.add_parser("add")
    ta.add_argument("title")
    ta.add_argument("--prompt", help="full instructions (default: the title); '-' reads stdin")
    ta.add_argument("--prompt-file")
    ta.add_argument("--parent", help="make it a sub-task of this task (use $FARM_TASK_ID)")
    ta.add_argument("--priority", type=int, default=5, help="0-9, higher runs first")
    tl = ts.add_parser("list")
    tl.add_argument("--status", choices=["queued", "running", "waiting", "done", "failed", "cancelled"])
    tl.add_argument("--limit", type=int, default=50)
    for name in ("show", "cancel", "retry"):
        ts.add_parser(name).add_argument("id")
    for q in (ta, tl, *[ts.choices[n] for n in ("show", "cancel", "retry")]):
        q.add_argument("--json", action="store_true")
    e = add("events", cmd_events, "the event log")
    e.add_argument("-n", type=int, default=30)
    e.add_argument("-f", "--follow", action="store_true")
    add("pause", cmd_pause, "pause new work everywhere").add_argument("reason", nargs="*")
    add("resume", cmd_resume, "resume work")
    add("init", cmd_init, "create the DynamoDB table")
    add("doctor", cmd_doctor, "check the setup")

    a = p.parse_args(argv)
    cfg = load()
    try:
        return a.fn(cfg, a) or 0
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # noqa: BLE001 - one clear line for humans and agents, not a traceback
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if code == "ResourceNotFoundException":
            print(f"claude-farm: DynamoDB table '{cfg.table}' does not exist yet. Start the farm (`claude-farm run`) "
                  "or run `claude-farm init`.", file=sys.stderr)
        elif "Could not connect" in str(e) or "Unable to locate credentials" in str(e):
            print(f"claude-farm: cannot reach DynamoDB ({cfg.endpoint or cfg.region}): {e}", file=sys.stderr)
        else:
            raise
        return 4


if __name__ == "__main__":
    sys.exit(main())
