"""clodfarm command line. You, and every Claude on the farm, use the same commands.

    clodfarm run                      start the farm daemon (the container does this)
    clodfarm login | logout | whoami  Claude subscription login (see docs/auth.md)
    clodfarm status                   the Claudes, their budget and the sub-agents at work
    clodfarm agents                   the Claudes on this farm and the budget each has left
    clodfarm budget [--refresh]       every account's 5-hour and 7-day usage and what the governor allows
    clodfarm spawn TITLE [--prompt TEXT | --prompt-file F | -] [--on NAME]   start a sub-agent
    clodfarm subagents [--all] [--mine] | result ID [--wait] | cancel ID | retry ID
    clodfarm msg NAME TEXT | inbox    talk to the other Claudes on the farm
    clodfarm schedule add TITLE (--cron "0 9 * * 1-5" [--tz Europe/Berlin] | --every 2h | --at "in 3h") [--prompt TEXT] [--on NAME]
    clodfarm schedule list | remove ID
    clodfarm events [-n 30] [-f]      the farm's event log
    clodfarm pause [REASON] | resume  stop or restart new sub-agents on every box
    clodfarm ui | ui-passwd           serve the farm UI on its own | set its password
    clodfarm init                     create the DynamoDB table
    clodfarm doctor                   check claude, login, the store, git and the workspace

Add --json for machine-readable output.
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


def _bar(frac, width: int = 10) -> str:
    n = max(0, min(width, round((frac or 0) * width)))
    return "█" * n + "░" * (width - n)


def _out(obj, as_json: bool, text: str):
    print(json.dumps(obj, indent=1, default=str) if as_json else text)


# ---------------------------------------------------------------- commands
def cmd_login(cfg, a):
    st = auth_status(cfg.claude_bin)
    if st.get("loggedIn") and not a.force:
        print(f"Already logged in: {st.get('email') or ''} {st.get('subscriptionType') or ''} via {st.get('via')}.")
        print("Use `clodfarm login --force` to switch accounts.")
        return 0
    print("Starting Claude Code login. Open the URL it prints on any device, approve, and paste the code here.\n")
    rc = subprocess.call([cfg.claude_bin, "auth", "login"])
    st = auth_status(cfg.claude_bin)
    if st.get("loggedIn"):
        print(f"\nLogged in: {st.get('email') or ''} ({st.get('subscriptionType') or 'subscription'}). "
              "The farm picks this up within a few seconds.")
        return 0
    print("\nNot logged in yet. Run `clodfarm login` again, or see docs/auth.md for the token option.")
    return rc or 1


def cmd_logout(cfg, a):
    return subprocess.call([cfg.claude_bin, "auth", "logout"])


def cmd_whoami(cfg, a):
    st = auth_status(cfg.claude_bin)
    keep = {k: st.get(k) for k in ("loggedIn", "via", "authMethod", "subscriptionType", "email", "orgName", "error") if st.get(k) is not None}
    _out(keep, a.json, "\n".join(f"{k}: {v}" for k, v in keep.items()))
    return 0 if st.get("loggedIn") else 1


def _seats(cfg, store) -> list[dict]:
    """Every Claude account (seat) in this farm: its latest usage, the governor's decision for it, its slots."""
    snaps = store.snapshots()
    workers = store.workers()
    names = sorted(set(snaps) | {w.get("seat") for w in workers if w.get("seat")}) or ["default"]
    out = []
    for seat in names:
        snap = snaps.get(seat)
        policy = cfg.policy if seat.startswith("api-") == cfg.policy.api_mode else \
            type(cfg.policy)(**{**vars(cfg.policy), "api_mode": seat.startswith("api-")})
        d = decide(snap, policy, now(), store.spent_today(seat))
        out.append({"seat": seat, "snapshot": snap, "decision": d, "policy": policy, "slots": store.slots(seat),
                    "boxes": sorted({w["SK"].rsplit("/", 1)[0] for w in workers if w.get("seat") == seat})})
    return out


def _seat_text(r) -> str:
    seat, snap, d, p = r["seat"], r["snapshot"], r["decision"], r["policy"]
    boxes = f"  boxes: {', '.join(r['boxes'])}" if r["boxes"] else "  (no live box)"
    lines = [f"SEAT {seat}{boxes}"]
    if p.api_mode:
        cap = f"${p.daily_budget_usd:.2f}/day" if p.daily_budget_usd else "no daily cap (set FARM_DAILY_BUDGET_USD)"
        lines.append(f"  API key, list-price spend ${d.details.get('spent_today_usd', 0):.2f} today, {cap}")
    elif not snap:
        lines.append("  no usage report yet: its first agent run measures it (or `clodfarm budget --refresh` on that box)")
    else:
        for name, w, cap in (("5-hour", snap.five_hour, p.five_hour_ceiling), ("7-day", snap.seven_day, p.weekly_target)):
            if w:
                lines.append(f"  {name:<7}{_bar(w.utilization)} {_pct(w.utilization):>4}  agents stop at {cap:.0%}   resets {_until(w.resets_at)}")
        if snap.status != "allowed" or snap.using_overage:
            lines.append(f"  status  {snap.status}{'  (paid overage in use)' if snap.using_overage else ''}")
        lines.append(f"  measured {_ago(snap.observed_at)} ago")
    lines.append(f"  governor {d.workers}/{p.max_workers} allowed · {len(r['slots'])} running · {d.reason}")
    if d.pause_until:
        lines.append(f"  next check {_until(d.pause_until)}")
    return "\n".join(lines)


def _budget_text(rows) -> str:
    head = "BUDGET per Claude account (seat), from Claude Code's own rate-limit reports"
    return "\n".join([head] + [_seat_text(r) for r in rows])


def _seats_json(rows):
    return [{"seat": r["seat"], "snapshot": r["snapshot"].to_dict() if r["snapshot"] else None,
             "decision": r["decision"].to_dict(), "slots": r["slots"], "boxes": r["boxes"]} for r in rows]


def cmd_budget(cfg, a):
    store = _store(cfg)
    if a.refresh:
        from .runner import build_cmd, run_agent
        from .auth import seat_id
        seat = cfg.seat or seat_id(auth_status(cfg.claude_bin))
        res = run_agent(build_cmd(cfg, "Answer in one word."), "Reply with: ok", cfg.workspace
                        if os.path.isdir(cfg.workspace) else os.getcwd(), dict(os.environ), 180,
                        on_snapshot=lambda sn: store.put_snapshot(sn, seat))
        if not res.snapshots:
            print(f"no rate-limit report received ({res.text[:200]})", file=sys.stderr)
    rows = _seats(cfg, store)
    _out({"seats": _seats_json(rows), "policy": vars(cfg.policy)}, a.json, _budget_text(rows))
    return 0


def _task_line(t) -> str:
    on = t.get("worker", "").split("@")[0] if t.get("status") == "running" else t.get("to") or ""
    return (f"  {t['id']}  {t['status']:<9} {_ago(t.get('updated')):>5}  {('on ' + on) if on else '':<14} "
            f"{t['title'][:64]}" + (f"  (for {t['owner']})" if t.get("owner") else "")
            + (f"  <- {t['parent']}" if t.get("parent") else ""))


def _claudes(cfg, store) -> list[dict]:
    """Every Claude on the farm that is up (by name: the part of a box id before '@'), with its budget."""
    seats = {r["seat"]: r for r in _seats(cfg, store)}
    out: dict[str, dict] = {}
    for w in store.workers():
        box = w["SK"].rsplit("/", 1)[0]
        name = box.split("@")[0]
        c = out.setdefault(name, {"name": name, "me": name == cfg.name, "seat": w.get("seat"), "boxes": set(),
                                  "running": 0})
        c["boxes"].add(box)
        c["seat"] = c["seat"] or w.get("seat")
        c["running"] += w.get("state") == "running"
    for c in out.values():
        r = seats.get(c["seat"]) or {}
        snap, d = r.get("snapshot"), r.get("decision")
        c.update(boxes=sorted(c["boxes"]),
                 five_hour_left=None if not (snap and snap.five_hour) else round(1 - snap.five_hour.utilization, 3),
                 seven_day_left=None if not (snap and snap.seven_day) else round(1 - snap.seven_day.utilization, 3),
                 can_start=max(0, (d.workers if d else 0) - c["running"]), reason=d.reason if d else "",
                 resets=d.pause_until if d else None)
    return sorted(out.values(), key=lambda c: (not c["me"], c["name"]))


def _claude_line(c) -> str:
    pct = lambda x: "?" if x is None else f"{x:.0%}"  # noqa: E731
    left = f"5h {pct(c['five_hour_left'])} left · 7d {pct(c['seven_day_left'])} left"
    busy = f"{c['running']} sub-agent{'s' if c['running'] != 1 else ''} running"
    room = f"can start {c['can_start']} more" if c["can_start"] else \
        f"{'no room for more' if c['running'] else 'resting'}: {c['reason']}" \
        + (f" (until {_until(c['resets'])})" if c.get("resets") else "")
    return f"  {c['name']:<16}{'(you)' if c['me'] else '     '}  {left} · {busy} · {room}"


def cmd_agents(cfg, a):
    rows = _claudes(cfg, _store(cfg))
    _out(rows, a.json, "CLAUDES on this farm (each is its own Claude account and budget)\n"
         + ("\n".join(_claude_line(c) for c in rows) or "  (none up: is `clodfarm run` running?)")
         + "\n\nStart a sub-agent: clodfarm spawn \"<title>\" --prompt \"...\"  (any Claude with budget runs it; "
           "--on NAME picks one)\nMessage a Claude:  clodfarm msg NAME \"<text>\"")
    return 0


def cmd_status(cfg, a):
    store = _store(cfg)
    rows, ctl = _claudes(cfg, store), store.control()
    active = store.list_tasks("running") + store.list_tasks("waiting") + store.list_tasks("queued", 20)
    if a.json:
        _out({"farm": cfg.farm_id, "paused": ctl, "claudes": rows, "subagents": active}, True, "")
        return 0
    print(f"clodfarm {__version__}  {cfg.farm_id}  store {store.describe()}"
          + (f"  PAUSED: {ctl.get('reason') or 'by hand'}" if ctl.get("paused") else ""))
    print("CLAUDES")
    print("\n".join(_claude_line(c) for c in rows) or "  (none up: is `clodfarm run` running?)")
    links = {}
    for e in store.events(now() - 7 * 86400, 2000):
        if e["type"] == "rc.connected":
            links[e["msg"].split("'")[1] if "'" in e["msg"] else "?"] = e["msg"].rsplit(" ", 1)[-1]
    for name, url in sorted(links.items()):
        print(f"  talk to {name}: {url}")
    print("SUB-AGENTS" + ("" if active else "  (none running)"))
    for t in active:
        print(_task_line(t))
    return 0


def _read_prompt(a) -> str:
    if a.prompt_file:
        return open(a.prompt_file).read() if a.prompt_file != "-" else sys.stdin.read()
    if a.prompt == "-":
        return sys.stdin.read()
    return a.prompt or a.title


def _names(cfg, store) -> set[str]:
    return {c["name"] for c in _claudes(cfg, store)}


def cmd_spawn(cfg, a):
    store = _store(cfg)
    me = os.environ.get("FARM_TASK_ID")  # set when a sub-agent spawns: its own sub-agents become its children
    parent = None if a.detach else (a.parent or me)
    try:
        if me and store.count("queued") >= cfg.max_queue:
            print(f"{cfg.max_queue} sub-agents are already waiting: not adding. Do it yourself or wait.", file=sys.stderr)
            return 3
        on = (a.on or "").strip() or None
        if on and on not in _names(cfg, store) and not a.force:
            print(f"no Claude named '{on}' is on the farm right now ({', '.join(sorted(_names(cfg, store))) or 'none'});"
                  " see `clodfarm agents`, or add --force to wait for it", file=sys.stderr)
            return 2
        t = store.add_task(a.title, _read_prompt(a), parent=parent, created_by=me or cfg.name, to=on,
                           owner=os.environ.get("FARM_OWNER") or cfg.name, max_depth=cfg.max_depth,
                           max_attempts=cfg.max_attempts)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    _out(t, a.json, f"started sub-agent {t['id']}: {t['title']}" + (f" on {t['to']}" if t.get("to") else "")
         + (f" (child of {parent})" if parent else "") + f". Its result: clodfarm result {t['id']}")
    return 0


def cmd_subagents(cfg, a):
    store = _store(cfg)
    ts = store.list_tasks("running") + store.list_tasks("waiting") + store.list_tasks("queued", 50)
    if a.all:
        ts += store.list_tasks("done", 20) + store.list_tasks("failed", 10) + store.list_tasks("cancelled", 10)
    if a.mine:
        ts = [t for t in ts if t.get("owner") == cfg.name]
    _out(ts, a.json, "\n".join(_task_line(t) for t in ts) or "(no sub-agents running)")
    return 0


def cmd_result(cfg, a):
    store = _store(cfg)
    t = store.get_task(a.id)
    end = time.time() + a.timeout
    while t and a.wait and t["status"] in ("queued", "running", "waiting") and time.time() < end:
        time.sleep(5)
        t = store.get_task(a.id)
    if not t:
        print("no such sub-agent", file=sys.stderr)
        return 1
    t["runs"] = store.runs(a.id)
    if a.json:
        _out(t, True, "")
        return 0
    on = t.get("worker", "").split("@")[0] or t.get("to") or ""
    print(f"{t['id']}  {t['status']}" + (f"  on {on}" if on else "") + f"  attempts {t.get('attempts', 0)}")
    print(f"title:   {t['title']}")
    for k in ("owner", "parent", "children", "branch"):
        if t.get(k):
            print(f"{k + ':':<9}{t[k]}")
    print(f"\nprompt:\n{t['prompt']}\n")
    print(f"result:\n{t['result']}\n" if t.get("result") else "result:  (not finished yet)\n")
    for r in t["runs"]:
        print(f"run {r['SK'][4:24]}  ok={r.get('ok')}  {r.get('duration_s')}s  turns={r.get('turns')}  "
              f"out_tokens={r.get('output_tokens')}  list-price ${r.get('cost_usd_list_price', 0):.2f}")
    return 0


def cmd_cancel(cfg, a):
    ok = _store(cfg).cancel(a.id)
    print("cancelled" if ok else "not cancelled (already finished?)")
    return 0 if ok else 1


def cmd_retry(cfg, a):
    ok = _store(cfg).retry(a.id)
    print("started again" if ok else "not restarted (still running?)")
    return 0 if ok else 1


def cmd_msg(cfg, a):
    store = _store(cfg)
    if a.to not in _names(cfg, store) and not a.force:
        print(f"no Claude named '{a.to}' is on the farm ({', '.join(sorted(_names(cfg, store))) or 'none'}); "
              "--force leaves it for when it joins", file=sys.stderr)
        return 2
    text = " ".join(a.text) if a.text != ["-"] else sys.stdin.read()
    if not text.strip():
        print("the message is empty", file=sys.stderr)
        return 2
    store.send_message(os.environ.get("FARM_OWNER") or cfg.name, a.to, text.strip())
    print(f"sent to {a.to}; it reads it with `clodfarm inbox`")
    return 0


def cmd_inbox(cfg, a):
    if a.hook:  # from the Claude Code hook: only in conversations with this Claude, never fail the prompt
        if os.environ.get("FARM_TASK_ID"):
            return 0
        try:
            msgs = _store(cfg).inbox(cfg.name)
        except Exception:  # noqa: BLE001
            return 0
        if msgs:
            print("New messages from other Claudes on this farm (reply with `clodfarm msg <name> \"...\"`):\n" + "\n".join(
                f"- from {m['from']} at {iso(m['at'])[11:16]}Z: {m['text']}" for m in msgs))
        return 0
    msgs = _store(cfg).inbox(cfg.name, unread_only=not a.all, mark_read=not a.peek)
    _out(msgs, a.json, "\n".join(f"  {iso(m['at'])[5:16].replace('T', ' ')}  from {m['from']}: {m['text']}" for m in msgs)
         or "(no new messages)")
    return 0


def cmd_schedule(cfg, a):
    from .schedule import describe, parse_at, parse_every
    store = _store(cfg)
    if a.sub == "add":
        try:
            spec = {"cron": a.cron} if a.cron else {"every": parse_every(a.every)} if a.every else \
                {"at": parse_at(a.at, a.tz)}
            sch = store.add_schedule(a.title, _read_prompt(a), tz=a.tz, to=(a.on or None),
                                     created_by=os.environ.get("FARM_TASK_ID") or cfg.name,
                                     owner=os.environ.get("FARM_OWNER") or cfg.name, **spec)
        except (ValueError, KeyError) as e:
            print(f"bad schedule: {e}", file=sys.stderr)
            return 2
        _out(sch, a.json, f"scheduled {sch['id']}: {sch['title']}  {describe(sch)}"
             + (f" on {sch['to']}" if sch.get("to") else "") + f"; next run {_until(sch['next_at'])}")
        return 0
    if a.sub == "list":
        rows = store.schedules()
        _out(rows, a.json, "\n".join(
            f"  {r['id']}  {describe(r):<32} next {_until(r['next_at']):<26} ran {r.get('runs', 0)}x  {r['title'][:60]}"
            + (f"  (on {r['to']})" if r.get("to") else "") for r in rows) or "(no schedules)")
        return 0
    if a.sub == "remove":
        ok = store.remove_schedule(a.id)
        print("removed" if ok else "no such schedule")
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
    store = _store(cfg)
    created = store.ensure_table()
    print(f"{store.describe()}: {'created' if created else 'already exists'}")
    return 0


def cmd_doctor(cfg, a):
    ok = True

    def check(name, good, detail=""):
        nonlocal ok
        ok &= bool(good)
        print(f"  {'ok  ' if good else 'FAIL'}  {name}{': ' + detail if detail else ''}")

    print("clodfarm doctor")
    exe = shutil.which(cfg.claude_bin)
    ver = subprocess.run([cfg.claude_bin, "--version"], capture_output=True, text=True).stdout.strip() if exe else ""
    check("claude binary", exe, ver or "not found")
    st = auth_status(cfg.claude_bin)
    check("subscription login", st.get("loggedIn"), st.get("via") or st.get("error") or "run `clodfarm login`")
    try:
        store = _store(cfg)
        check("farm store", store.ready(), store.describe() + ("" if store.ready() else " (start the farm or run `clodfarm init`)"))
    except Exception as e:  # noqa: BLE001
        check("farm store", False, f"{cfg.store}: {str(e)[:160]}")
    check("git", shutil.which("git"))
    check("workspace", os.path.isdir(cfg.workspace), cfg.workspace)
    return 0 if ok else 1


def cmd_ui(cfg, a):
    from .web import serve
    serve(cfg)
    return 0


def cmd_ui_passwd(cfg, a):
    import getpass
    from .web import Auth
    if os.environ.get("FARM_UI_PASSWORD"):
        print("FARM_UI_PASSWORD is set in the environment and wins over a stored password: change it there.",
              file=sys.stderr)
        return 1
    auth = Auth(os.path.join(cfg.workspace, ".farm", "ui-auth.json"))
    pw = sys.stdin.readline().rstrip("\n") if not sys.stdin.isatty() else getpass.getpass("new farm UI password: ")
    if sys.stdin.isatty() and getpass.getpass("again: ") != pw:
        print("the passwords don't match", file=sys.stderr)
        return 1
    try:
        auth.set_password(pw)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    print("farm UI password saved (every open session is signed out)")
    return 0


def cmd_run(cfg, a):
    from .supervisor import Farm
    Farm(cfg, _store(cfg)).run()
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="clodfarm", description="Always-on Claude Code agents on your subscription.",
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("--version", action="version", version=f"clodfarm {__version__}")
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
    add("status", cmd_status, "the Claudes, their budget and the sub-agents at work")
    add("budget", cmd_budget, "subscription usage and governor decision").add_argument(
        "--refresh", action="store_true", help="run a tiny agent call to measure usage now")
    sw = add("spawn", cmd_spawn, "start a sub-agent (any Claude with budget runs it, or --on NAME)")
    sw.add_argument("title")
    sw.add_argument("--prompt", help="full, self-contained instructions (default: the title); '-' reads stdin")
    sw.add_argument("--prompt-file")
    sw.add_argument("--on", help="run it on this Claude's account (see `clodfarm agents`); default: whoever has budget")
    sw.add_argument("--force", action="store_true", help="with --on: wait for that Claude even if it isn't up now")
    sw.add_argument("--parent", help="make it the sub-agent of this one (default inside a sub-agent: $FARM_TASK_ID)")
    sw.add_argument("--detach", action="store_true", help="inside a sub-agent: start it on its own, not as your child")
    sl = add("subagents", cmd_subagents, "the sub-agents running and waiting")
    sl.add_argument("--all", action="store_true", help="also the recently finished ones")
    sl.add_argument("--mine", action="store_true", help="only the ones this Claude started")
    rs = add("result", cmd_result, "a sub-agent's status and result")
    rs.add_argument("id")
    rs.add_argument("--wait", action="store_true", help="wait until it finishes")
    rs.add_argument("--timeout", type=int, default=1800, help="with --wait: give up after this many seconds")
    add("cancel", cmd_cancel, "stop a sub-agent").add_argument("id")
    add("retry", cmd_retry, "start a failed or cancelled sub-agent again").add_argument("id")
    ms = add("msg", cmd_msg, "send a message to another Claude on the farm")
    ms.add_argument("to")
    ms.add_argument("text", nargs="+", help="the message ('-' reads stdin)")
    ms.add_argument("--force", action="store_true", help="leave it even if that Claude isn't up now")
    ib = add("inbox", cmd_inbox, "messages other Claudes sent you")
    ib.add_argument("--all", action="store_true", help="also the ones already read")
    ib.add_argument("--peek", action="store_true", help="don't mark them read")
    ib.add_argument("--hook", action="store_true", help=argparse.SUPPRESS)
    add("agents", cmd_agents, "the Claudes on this farm and the budget each has left")
    sc = add("schedule", cmd_schedule, "start a sub-agent on a schedule")
    scs = sc.add_subparsers(dest="sub", required=True)
    sa = scs.add_parser("add")
    sa.add_argument("title")
    sa.add_argument("--prompt", help="full instructions (default: the title); '-' reads stdin")
    sa.add_argument("--prompt-file")
    when = sa.add_mutually_exclusive_group(required=True)
    when.add_argument("--cron", help="five-field cron line, e.g. '0 9 * * 1-5' (in --tz)")
    when.add_argument("--every", help="an interval: 30m, 2h, 1d, 1w")
    when.add_argument("--at", help="once: 2026-10-01T09:00 (in --tz), or 'in 3h'")
    sa.add_argument("--tz", default=os.environ.get("FARM_TZ") or "UTC", help="time zone for --cron/--at (default FARM_TZ or UTC)")
    sa.add_argument("--on", help="run it on this Claude's account; default: whoever has budget")
    scs.add_parser("list")
    scs.add_parser("remove").add_argument("id")
    for q in scs.choices.values():
        q.add_argument("--json", action="store_true")
    e = add("events", cmd_events, "the event log")
    e.add_argument("-n", type=int, default=30)
    e.add_argument("-f", "--follow", action="store_true")
    add("pause", cmd_pause, "pause new work everywhere").add_argument("reason", nargs="*")
    add("resume", cmd_resume, "resume work")
    add("ui", cmd_ui, "serve the farm UI (the daemon also serves it unless FARM_UI=0)")
    add("ui-passwd", cmd_ui_passwd, "set the farm UI password (reads stdin when piped)")
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
            print(f"clodfarm: DynamoDB table '{cfg.table}' does not exist yet. Start the farm (`clodfarm run`) "
                  "or run `clodfarm init`.", file=sys.stderr)
        elif "Could not connect" in str(e) or "Unable to locate credentials" in str(e):
            print(f"clodfarm: cannot reach DynamoDB ({cfg.endpoint or cfg.region}): {e}", file=sys.stderr)
        else:
            raise
        return 4


if __name__ == "__main__":
    sys.exit(main())
