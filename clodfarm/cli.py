"""clodfarm command line. You, and every Claude on the farm, use the same commands.

    clodfarm run                      start the farm daemon (the container does this)
    clodfarm login | logout | whoami  Claude subscription login (see docs/auth.md)
    clodfarm status                   the Claudes, their budget and the sub-agents at work
    clodfarm agents                   the Claudes on this farm, each one's usage (% used) and room for sub-agents
    clodfarm budget [--refresh]       every account's 5-hour and 7-day usage and what the governor allows
    clodfarm spawn TITLE [--prompt TEXT | --prompt-file F | -] [--on NAME]   start a sub-agent
    clodfarm subagents [--all] [--mine] | result ID [--wait] | cancel ID | retry ID
    clodfarm msg NAME|ID TEXT [--urgent] [--wake] | inbox   talk to the other Claudes, sub-agents and connected Claude Codes
    clodfarm connect | connections | disconnect ID   Claude Code on your computer, over MCP (docs/mcp.md)
    clodfarm schedule add TITLE (--cron "0 9 * * 1-5" [--tz Europe/Berlin] | --every 2h | --at "in 3h") [--prompt TEXT] [--on NAME]
    clodfarm schedule list | remove ID
    clodfarm events [-n 30] [-f]      the farm's event log
    clodfarm pause [REASON] | resume  stop or restart new sub-agents on every box
    clodfarm ui | ui-passwd           serve the farm UI on its own | set its password
    clodfarm slack                    Slack: connected or not, and how to connect it (the UI's SLACK button is easier)
    clodfarm browser [status] | start|stop [PROFILE] | open URL [--profile P] | add|remove NAME   the farm's browser
    clodfarm browser proxy on|off [PROFILE] [--country us]   through the farm's proxy (set in the UI), or direct
    clodfarm init                     create the DynamoDB table
    clodfarm doctor                   check claude, login, the store, git and the workspace

Add --json for machine-readable output.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time

from . import __version__
from .auth import auth_status
from .config import load
from .governor import decide
from .store import TASK_ID, Store, iso, now


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
                        if os.path.isdir(cfg.workspace) else os.getcwd(), {**os.environ, "FARM_TASK_ID": "usage"}, 180,
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
                 five_hour_used=None if not (snap and snap.five_hour) else round(snap.five_hour.utilization, 3),
                 seven_day_used=None if not (snap and snap.seven_day) else round(snap.seven_day.utilization, 3),
                 can_start=max(0, (d.workers if d else 0) - c["running"]), reason=d.reason if d else "",
                 resets=d.pause_until if d else None)
    return sorted(out.values(), key=lambda c: (not c["me"], c["name"]))


def _claude_line(c) -> str:
    pct = lambda x: "?" if x is None else f"{x:.0%}"  # noqa: E731
    used = f"5h {pct(c['five_hour_used'])} used · 7d {pct(c['seven_day_used'])} used"
    busy = f"{c['running']} sub-agent{'s' if c['running'] != 1 else ''} running"
    room = f"can start {c['can_start']} more" if c["can_start"] else \
        f"{'no room for more' if c['running'] else 'resting'}: {c['reason']}" \
        + (f" (until {_until(c['resets'])})" if c.get("resets") else "")
    return f"  {c['name']:<16}{'(you)' if c['me'] else '     '}  {used} · {busy} · {room}"


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
         + (f" (child of {parent})" if parent else "") + f". Its result: clodfarm result {t['id']}"
         + ("" if me else f". To hear when it's done without keeping your person waiting, run `clodfarm result "
                          f"{t['id']} --wait --timeout 86400` with Bash in the background and end your turn"))
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


def _my_address(cfg) -> str:
    """Where messages for this session wait: a sub-agent's own task id, else its Claude's name (its conversations)."""
    tid = os.environ.get("FARM_TASK_ID")
    return tid if tid and tid != "usage" else cfg.name


def cmd_msg(cfg, a):
    store = _store(cfg)
    from .mcp import connection_names
    task = store.get_task(a.to) if TASK_ID.fullmatch(a.to) else None
    if not task and a.to not in _names(cfg, store) | connection_names(cfg.workspace) and not a.force:
        print(f"no Claude named '{a.to}' is on the farm ({', '.join(sorted(_names(cfg, store))) or 'none'}) and no "
              "sub-agent has that id; --force leaves it for when it joins", file=sys.stderr)
        return 2
    if a.urgent and not task:
        print("--urgent interrupts a running sub-agent: give its id (`clodfarm subagents`)", file=sys.stderr)
        return 2
    text = " ".join(a.text) if a.text != ["-"] else sys.stdin.read()
    if not text.strip():
        print("the message is empty", file=sys.stderr)
        return 2
    me, frm = _my_address(cfg), os.environ.get("FARM_OWNER") or cfg.name
    # from a conversation (not a sub-agent) to one of its own Claude's sub-agents: the person's own instruction
    person = bool(task) and me == cfg.name and task.get("owner") == frm
    m = store.send_message(frm, a.to, text.strip(),
                           reply=me if me != cfg.name else None, urgent=a.urgent, wake=a.wake,
                           hops=int(os.environ.get("FARM_MAIL_HOPS") or 0), wake_after=cfg.mail_wake_after,
                           person=person)
    wake = f"; if nobody has read it in {cfg.mail_wake_after}s, the farm starts someone to handle it" if a.wake else ""
    status = (task or {}).get("status")
    if not task:
        how = (f"{a.to}'s conversations get it at their next tool call or prompt (one that was active in the last "
               f"10 minutes is woken for it){wake}")
    elif status == "running":
        how = "it is interrupted and gets it now" if a.urgent else "it gets it at its next tool call"
    elif status in ("queued", "waiting"):
        how = "it gets it when it next runs"
    else:
        how = f"it has {status}; " + (wake[2:] if a.wake else "it only reads it if it runs again (--wake resumes it)")
    _out(m, a.json, f"sent {m['id']} to {a.to}: {how}")
    return 0


def _take_mail(store, cfg, how: str) -> list[dict]:
    flag = os.environ.get("FARM_MAIL_FLAG")
    if flag:  # first, so a message that arrives while we read rings (and is flagged) again
        try:
            os.remove(flag)
        except OSError:
            pass
    return store.claim(_my_address(cfg), f"{how}:{cfg.name}")


def _peer(name: str) -> str:
    """A short label for a Claude Code session name: the sub-agent id or the Claude in '[clodfarm] farm · name'."""
    parts = [p.strip() for p in str(name).replace("[clodfarm]", "").split("·") if p.strip()]
    if parts and TASK_ID.fullmatch(parts[-1]):
        return parts[-1]
    return re.sub(r"\s+", "-", parts[-1] if parts else str(name))[:40] or "?"


MAIL_STOP_BLOCKS = 3  # a turn is kept going for new messages at most this often in a row


def cmd_hook(cfg, a):
    """Called by Claude Code (the hooks the farm installs, see auth.farm_hooks) with the event as JSON on stdin.
    Registers the session and copies its new turns into the store, and hands the session the messages other Claudes
    left for it in the store: at a conversation's next prompt, after a batch of tool calls, and before a turn ends
    (the Stop blocks with them, so Claude reads them first). It logs messages sent with Claude Code's SendMessage.
    With --listen (in the background after a conversation's turn) it waits for mail and wakes the conversation.
    It never fails the session: any problem is reported on stderr and it exits 0."""
    from .prompts import mail_text
    from .sessions import session_kind
    try:
        ev = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        ev = {}
    name, sid = ev.get("hook_event_name", ""), ev.get("session_id")
    kind = session_kind()
    if kind == "usage" or (a.listen and not _remote_conversation(kind)):
        return 0
    try:
        store = _store(cfg)
        if a.listen:
            return _listen(cfg, store, sid)
        if name == "PostToolBatch":  # its flag file exists: mail is waiting
            msgs = _take_mail(store, cfg, "tool")
            if msgs:
                print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolBatch",
                                                         "additionalContext": mail_text(msgs)}}))
            return 0
        if name == "PostToolUse":
            if ev.get("tool_name") == "SendMessage":
                _log_native(cfg, store, ev)
            return 0
        if sid:
            owner = os.environ.get("FARM_OWNER") if kind != "conversation" else None  # a sub-agent's Claude
            store.record_session(sid, transcript=ev.get("transcript_path"), claude=owner or cfg.name, runs_on=cfg.name, kind=kind, box=cfg.farm_id, cwd=ev.get("cwd"),
                                 task=os.environ.get("FARM_TASK_ID") if kind == "sub-agent" else None,
                                 ended=True if name == "SessionEnd" else None,
                                 end_reason=ev.get("reason") if name == "SessionEnd" else None,
                                 # mid-turn: the farm shows the Claude at work while a conversation's turn runs
                                 busy=True if name == "UserPromptSubmit" else False if name == "SessionEnd" else None)
        if name == "SessionEnd" and sid and os.environ.get("FARM_MAIL_FLAG"):  # its listener stops too
            try:
                os.remove(_listener_file(os.environ["FARM_MAIL_FLAG"], sid))
            except OSError:
                pass
        if kind == "conversation" and name == "UserPromptSubmit":  # a real prompt: a session that never gets one
            # (aborted, or only opened) must not use the messages up
            msgs = _take_mail(store, cfg, "prompt")
            if msgs:
                print(mail_text(msgs))
        if name == "Stop" and sid:
            blocks = int((store.session(sid) or {}).get("mail_blocks", 0)) if ev.get("stop_hook_active") else 0
            msgs = _take_mail(store, cfg, "stop") if blocks < MAIL_STOP_BLOCKS else []
            if msgs:
                print(json.dumps({"decision": "block", "reason": mail_text(msgs)}))
                store.record_session(sid, mail_blocks=blocks + 1)
            else:  # the turn ends
                store.record_session(sid, busy=False)
    except Exception as e:  # noqa: BLE001
        print(f"clodfarm hook: {e!r}"[:300], file=sys.stderr)
    return 0


def _log_native(cfg, store, ev: dict):
    """A message sent with Claude Code's own SendMessage: into the farm's event log, like `clodfarm msg`."""
    inp, resp = ev.get("tool_input") or {}, ev.get("tool_response")
    if isinstance(resp, str):
        try:
            resp = json.loads(resp)
        except ValueError:
            resp = {}
    if isinstance(resp, dict) and resp.get("success") is False:
        return
    text = inp.get("message") or inp.get("content") or ""
    if not isinstance(text, str) or not text.strip():
        return
    frm = os.environ.get("FARM_OWNER") or cfg.name
    store.event("msg.sent", f"{frm} -> {_peer(inp.get('to') or inp.get('recipient') or '?')}: {text[:200]} (live)",
                by=frm)


def _remote_conversation(kind: str) -> bool:
    """A conversation someone has with this Claude through Remote Control (the Claude app, claude.ai/code): the
    sessions worth keeping a listener for. Claude Code waits up to 30 s for a waiting listener when a session exits, so
    a sub-agent (its run ends with its turn), a terminal session or a one-off `claude -p` doesn't get one."""
    return kind == "conversation" and os.environ.get("CLAUDE_CODE_ENVIRONMENT_KIND") == "bridge"


def _listener_file(flag: str, sid: str) -> str:
    return os.path.join(os.path.dirname(flag), ".listen-" + re.sub(r"[^A-Za-z0-9_-]", "", sid)[:64])


def _listen(cfg, store, sid: str | None) -> int:
    """In the background after a conversation's turn (an async hook with asyncRewake): wait for this Claude's mail
    flag and, when it appears, take the messages and exit 2, which wakes the conversation with them (stderr). One
    listener per session: a new turn's takes over, so it waits about LISTEN_SECONDS after the last turn. It gives up
    when the session goes away."""
    from .auth import LISTEN_SECONDS
    from .prompts import mail_text
    flag = os.environ.get("FARM_MAIL_FLAG")
    if not flag or not sid:
        return 0
    mine = _listener_file(flag, sid)
    with open(mine, "w") as f:  # the session's earlier listener sees this and stops
        f.write(str(os.getpid()))

    def still_mine() -> bool:
        try:
            return open(mine).read().strip() == str(os.getpid())
        except OSError:
            return False
    parent, end = os.getppid(), time.time() + LISTEN_SECONDS - 15
    try:
        while time.time() < end and os.getppid() == parent and still_mine():
            if os.path.exists(flag):
                msgs = _take_mail(store, cfg, "wake")
                if msgs:
                    print(mail_text(msgs), file=sys.stderr)
                    return 2
            time.sleep(1)
        return 0
    finally:
        if still_mine():
            try:
                os.remove(mine)
            except OSError:
                pass


def cmd_sessions(cfg, a):
    rows = [s for s in _store(cfg).sessions(a.claude, a.n + 500) if a.all or s.get("kind") != "usage"][:a.n]
    _out(rows, a.json, "\n".join(
        f"  {r['id'][:8]}  {r.get('kind', '?'):<12} {r.get('claude', '?'):<12} {_ago(r.get('last_at')):>5}  "
        f"{r.get('turns', 0):>4} turns  {'ended ' if r.get('ended') else ''}{(r.get('title') or '')[:60]}" for r in rows)
         or "(no sessions recorded yet)")
    return 0


def cmd_session(cfg, a):
    store = _store(cfg)
    s = store.session(a.id) or next((x for x in store.sessions(limit=1000) if x["id"].startswith(a.id)), None)
    if not s:
        print("no such session", file=sys.stderr)
        return 1
    turns = store.turns(s["id"])
    if a.json:
        _out({**s, "conversation": turns}, True, "")
        return 0
    print(f"{s['id']}  {s.get('kind')}  {s.get('claude')}  {len(turns)} turns  {s.get('title') or ''}")
    for t in turns:
        who = "TOOL" if t["kind"] == "tool_result" else \
            {"user": "YOU" if s.get("kind") == "conversation" else "FARM", "assistant": "CLAUDE"}.get(t["role"], t["role"])
        tag = "" if t["kind"] == "text" else f" [{t['kind']}]"
        print(f"\n{who}{tag}: {t['text']}")
    return 0


def cmd_inbox(cfg, a):
    msgs = _store(cfg).inbox(_my_address(cfg), unread_only=not a.all, mark_read=not a.peek)
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


def cmd_slack(cfg, a):
    from .slack import load_settings, manifest_url
    s = load_settings(cfg)
    info, ok = s.get("info") or {}, bool(s.get("bot_token") and s.get("app_token"))
    _out({"configured": ok, "team": info.get("team"), "bot": info.get("bot_name"), "allow": s.get("allow") or [],
          "manifest_url": manifest_url(cfg.farm)}, a.json,
         (f"Slack: connected to {info.get('team') or '?'} as @{info.get('bot_name') or '?'}"
          + (f"; only {', '.join(s['allow'])} can give it work" if s.get("allow") else "") if ok else
          "Slack: not connected. Easiest: the SLACK button in the farm UI. By hand:\n"
          f"  1. open this, then Create an App > From a manifest (it's filled in) > Next > Create:\n     {manifest_url(cfg.farm)}\n"
          "  2. Install to Workspace; copy the Bot User OAuth Token (xoxb-...)\n"
          "  3. Basic Information > App-Level Tokens > Generate with connections:write (xapp-...)\n"
          "  4. set FARM_SLACK_BOT_TOKEN and FARM_SLACK_APP_TOKEN (optional FARM_SLACK_ALLOW) and restart the farm"))
    return 0


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


def _farm_url() -> str:
    return (os.environ.get("FARM_PUBLIC_URL") or f"http://localhost:{os.environ.get('FARM_UI_PORT', '8080')}"
            + ("/" + os.environ.get("FARM_UI_BASE", "").strip("/") if os.environ.get("FARM_UI_BASE", "").strip("/") else "")).rstrip("/")


def cmd_dashboard(cfg, a):
    from . import dashboards as dash
    from .gitops import git
    from .schedule import parse_every
    store, by = _store(cfg), os.environ.get("FARM_TASK_ID") or cfg.name
    owner = os.environ.get("FARM_OWNER") or cfg.name
    try:
        if a.sub == "list":
            rows = [dash.summary(store, d, 7) for d in dash.all_(store)]
            _out(rows, a.json, "\n".join(
                f"  {r['slug']:<24} {r['title'][:40]:<40} {_ago(r['updated']):>6} ago  by {r['owner'] or '-'}"
                + (f"  live every {r['every'] // 60}m" + ("" if r["ok"] is not False else " (last refresh FAILED)")
                   if r["live"] else "") for r in rows) or "(no dashboards yet: `clodfarm dashboard push <name> --file spec.json`)")
            return 0
        if a.sub == "show":
            d = dash.get(store, a.name)
            if not d:
                print(f"no dashboard {a.name}", file=sys.stderr)
                return 1
            v = dash.view(store, d, 30)
            _out(v, a.json, json.dumps({k: v[k] for k in ("title", "description", "widgets", "refresh")}, indent=1)
                 + f"\n\n{_farm_url()}/dashboards/{d['slug']}")
            return 0
        if a.sub == "push":
            if a.run:  # a live dashboard: run its code now (so errors show here), then the farm runs it on schedule
                every = parse_every(a.every or "1h")
                if every < dash.MIN_EVERY:
                    raise ValueError(f"refresh at most every {dash.MIN_EVERY // 60} minutes")
                cwd = git(os.getcwd(), "rev-parse", "--show-toplevel", check=False).strip() or os.getcwd()
                d = dash.push(store, a.name, dash.run_refresh(a.run, cwd, a.name), by=by, owner=owner)
                d = dash.set_refresh(store, a.name, a.run, every, by=by)
            elif a.no_refresh and not a.file:
                d = dash.set_refresh(store, a.name, None, by=by)
            else:
                d = dash.push(store, a.name, open(a.file).read() if a.file not in (None, "-") else sys.stdin.read(),
                              by=by, owner=owner)
                if a.no_refresh:
                    d = dash.set_refresh(store, a.name, None, by=by)
            live = d.get("refresh")
            _out(d, a.json, f"dashboard {d['slug']}: {len(d['widgets'])} widget(s)"
                 + (f", refreshed every {live['every'] // 60}m by `{live['cmd']}` (run in the repo on main)" if live else "")
                 + f"\n{_farm_url()}/dashboards/{d['slug']}")
            return 0
        if a.sub == "metric":
            d = dash.set_metric(store, a.name, a.key, dash._num(a.value, a.key, True), a.label, a.unit, a.good, by=by,
                                owner=owner)
            _out(d, a.json, f"{d['slug']}/{a.key} = {a.value}{a.unit or ''}  {_farm_url()}/dashboards/{d['slug']}")
            return 0
        if a.sub == "refresh":
            d = dash.get(store, a.name)
            if not d or not d.get("refresh"):
                print(f"{a.name} is not a live dashboard (push it with --run)", file=sys.stderr)
                return 1
            cwd = git(os.getcwd(), "rev-parse", "--show-toplevel", check=False).strip() or os.getcwd()
            r = dash.refresh(store, d, cwd)
            print("refreshed" if r["ok"] else f"refresh failed: {r['error']}")
            return 0 if r["ok"] else 1
        if a.sub == "remove":
            ok = dash.remove(store, a.name, by=by)
            print("removed" if ok else "no such dashboard")
            return 0 if ok else 1
    except (ValueError, OSError) as e:  # SpecError is a ValueError
        print(f"dashboard: {e}", file=sys.stderr)
        return 2
    return 1


def cmd_connect(cfg, a):
    url = (a.url or _farm_url()).rstrip("/")
    print("On your computer, add the farm to Claude Code (once):\n\n"
          f"  claude mcp add --transport http --scope user {cfg.farm} {url}/mcp\n\n"
          "Then run /mcp in Claude Code, pick it and sign in: the farm asks for its UI password and a name for\n"
          "your computer. Claude Code never sees the password; it gets a token for this farm only, which you can\n"
          "end with `clodfarm disconnect ID`. Behind a proxy that rewrites Host (CloudFront), set FARM_PUBLIC_URL.")
    return 0


def cmd_connections(cfg, a):
    from .mcp import OAuthStore, oauth_path
    rows = OAuthStore(oauth_path(cfg.workspace)).connections()
    _out(rows, a.json, "\n".join(
        f"  {r['id']}  {r['name']:<20} {r['scope']:<20} {r['client_name'][:30]:<30} connected {_ago(r['created'])} ago"
        f" · last used {_ago(r['last_used']) + ' ago' if r['last_used'] else 'never'}" for r in rows)
         or "(nothing connected: see `clodfarm connect`)")
    return 0


def cmd_disconnect(cfg, a):
    from .mcp import OAuthStore, oauth_path
    ok = OAuthStore(oauth_path(cfg.workspace)).disconnect(a.id)
    if ok:
        _store(cfg).event("mcp.disconnected", f"connection {a.id} ended by hand", by=cfg.name)
    print("disconnected: its tokens stop working now" if ok else "no such connection (see `clodfarm connections`)")
    return 0 if ok else 1


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


def cmd_browser(cfg, a):
    from . import browser
    bs = browser.Browsers(cfg.workspace)
    name = getattr(a, "profile", None) or browser.DEFAULT
    try:
        if a.sub in ("add", "remove"):
            if a.sub == "add":
                bs.registry.add(name, by=cfg.name)
            elif not bs.registry.remove(name):
                raise ValueError(f"no browser profile named {name}")
            done = {"add": "added", "remove": "removed"}[a.sub]
            _store(cfg).event(f"browser.{done}", f"browser profile {name} {done} by {cfg.name}", by=cfg.name)
            from .agents import AgentManager
            AgentManager(cfg).share_browser_tools()
            print(f"browser profile {name} {done}" + (f"; its tools are mcp__{browser.mcp_name(name)}__*, in the "
                                                      "Claudes' new sessions" if a.sub == "add" else ""))
            return 0
        if a.sub in ("start", "stop"):
            on = a.sub == "start"
            if on and not browser.available():
                raise ValueError("this image has no browser (" + ", ".join(browser.missing() or ["FARM_BROWSER=0"]) + ")")
            slot = bs.slot(name)
            if bs.want(name, on, by=cfg.name):
                _store(cfg).event("browser.started" if on else "browser.stopped",
                                  f"browser profile {name} {'started' if on else 'stopped'} by {cfg.name}", by=cfg.name)
            end = time.time() + (40 if on else 20)
            while time.time() < end and browser.cdp_up(slot) != on:  # the farm UI's process starts and stops it
                time.sleep(0.5)
            if browser.cdp_up(slot) != on:
                print(f"clodfarm: profile {name} is {'not up' if on else 'still up'} yet: the farm (its UI process) "
                      "runs the browser; is `clodfarm run` up with FARM_UI=1? See `clodfarm browser`.", file=sys.stderr)
                return 1
        if a.sub == "proxy":
            on = a.state == "on"
            changed, seen = bs.proxy(name, on, by=cfg.name, country=a.country)
            where = f" (the web sees {seen['ip']}{', ' + seen['country'].upper() if seen.get('country') else ''})" \
                if seen else ""
            if changed:
                _store(cfg).event("browser.proxy", f"browser profile {name} {'through the proxy' if on else 'direct'}"
                                  f"{where} by {cfg.name}", by=cfg.name)
            print(f"browser profile {name} goes {'through the proxy' if on else 'direct'}{where}"
                  + ("; the farm restarts it" if changed and bs.registry.get(name).get("on") else ""))
            return 0
        if a.sub == "open":
            slot = bs.slot(name)
            if not browser.cdp_up(slot):
                raise ValueError(f"profile {name} is off: `clodfarm browser start {name}` first")
            t = browser.open_url(a.url, slot)
            return _out(t, a.json, f"opened {t['url']} in a new tab of profile {name}") or 0
    except ValueError as e:
        print(f"clodfarm: {e}", file=sys.stderr)
        return 1
    st = bs.status()
    if not st["available"]:
        text = "no browser in this image (" + ", ".join(st["missing"]) + ")"
    else:
        lines = []
        for p in st["profiles"]:
            state = "up" if p["ready"] else "starting" if p["on"] else "off"
            lines.append(f"{p['name']:24} {state:9} tools {p['tools']}"
                         + (f"  via the proxy{' from ' + p['country'].upper() if p['country'] else ''}" if p["proxy"] else "")
                         + (f"  error: {p['error'] or p['proxy_error']}" if p["error"] or p["proxy_error"] else ""))
            lines += [f"    {t['title'][:56] or '(untitled)':56}  {t['url'][:90]}" for t in p["tabs"]]
        text = "the farm's browser profiles (the person logs in to sites in the farm UI's BROWSER):\n  " + "\n  ".join(lines)
        if st["proxy"]["set"]:
            text += f"\nproxy: {st['proxy']['server']}" + (" (FARM_BROWSER_PROXY)" if st["proxy"]["from_env"] else "")
    return _out(st, a.json, text) or 0


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
    ms = add("msg", cmd_msg, "send a message to another Claude or to a sub-agent on the farm")
    ms.add_argument("to", help="a Claude's name (see `clodfarm agents`) or a sub-agent's id")
    ms.add_argument("text", nargs="+", help="the message ('-' reads stdin)")
    ms.add_argument("--force", action="store_true", help="leave it even if that Claude isn't up now")
    ms.add_argument("--urgent", action="store_true", help="interrupt that running sub-agent to hand it over now")
    ms.add_argument("--wake", action="store_true",
                    help="if nobody reads it in time, start a sub-agent for that Claude (or resume that sub-agent)")
    ib = add("inbox", cmd_inbox, "messages other Claudes sent you")
    ib.add_argument("--all", action="store_true", help="also the ones already read")
    ib.add_argument("--peek", action="store_true", help="don't mark them read")
    add("hook", cmd_hook, argparse.SUPPRESS).add_argument("--listen", action="store_true", help=argparse.SUPPRESS)
    ss = add("sessions", cmd_sessions, "every Claude session on the farm (conversations and sub-agents)")
    ss.add_argument("--claude", help="only this Claude's")
    ss.add_argument("-n", type=int, default=30)
    ss.add_argument("--all", action="store_true", help="also the usage checks recorded by older versions")
    se = add("session", cmd_session, "one session's whole conversation")
    se.add_argument("id")
    add("agents", cmd_agents, "the Claudes on this farm and how much of its usage each has used")
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
    db = add("dashboard", cmd_dashboard, "dashboards the Claudes keep: pages at /dashboards/<name> that show improvements")
    dbs = db.add_subparsers(dest="sub", required=True)
    dbs.add_parser("list", help="every dashboard")
    dbs.add_parser("show", help="one dashboard's spec and link").add_argument("name")
    dp = dbs.add_parser("push", help="create or replace a dashboard from a JSON spec (see `clodfarm dashboard push -h`)",
                        description="Create or replace a dashboard. The spec is JSON: {title, description, widgets: [...]}; "
                        "widget types: stat {key,label,value,unit,good:up|down,target}, chart {label,unit,from:[stat keys]} "
                        "or {label,unit,series:[{name,points:[[time,value]]}]}, bars {label,unit,items:[{label,value,href}]}, "
                        "table {label,columns,rows}, progress {label,value,max}, text {label,text (**bold**, `code`, "
                        "[links](https://..), - lists)}. Every push records each stat's value, so the page shows its trend.")
    dp.add_argument("name", help="lowercase-and-dashes; the page is /dashboards/<name>")
    dp.add_argument("--file", help="the JSON spec ('-' or nothing: stdin)")
    dp.add_argument("--run", help="make it live: a command, run in the repo, that prints the JSON spec (e.g. "
                    "'python3 dashboards/tests.py'); commit that code so the farm can run it")
    dp.add_argument("--every", help="with --run: how often the farm runs it (default 1h, at least 5m)")
    dp.add_argument("--no-refresh", action="store_true", help="stop refreshing a live dashboard")
    dm = dbs.add_parser("metric", help="set one stat (adds the dashboard and the stat when new)")
    dm.add_argument("name")
    dm.add_argument("key")
    dm.add_argument("value")
    dm.add_argument("--label")
    dm.add_argument("--unit")
    dm.add_argument("--good", choices=["up", "down"], help="which way is better (colors the change)")
    dbs.add_parser("refresh", help="run a live dashboard's command now").add_argument("name")
    dbs.add_parser("remove", help="delete a dashboard and its history").add_argument("name")
    for q in dbs.choices.values():
        q.add_argument("--json", action="store_true")
    add("slack", cmd_slack, "give the farm work from Slack: status, or how to connect it")
    br = add("browser", cmd_browser, "the farm's browser: you log in to sites in the UI, the Claudes use it")
    brs = br.add_subparsers(dest="sub")
    brs.add_parser("status", help="every profile: on or off, and its tabs")
    for verb, help_ in (("start", "start a profile (it stays on until stopped)"), ("stop", "stop a profile (its logins "
                        "are kept)"), ("add", "add a profile: its own Chromium with its own logins"),
                        ("remove", "remove a profile and delete its logins")):
        brs.add_parser(verb, help=help_).add_argument("profile", nargs="?" if verb in ("start", "stop") else None,
                                                      help="the profile (default: default)")
    bp = brs.add_parser("proxy", help="send a profile through the farm's proxy (on) or direct (off); it restarts")
    bp.add_argument("state", choices=["on", "off"])
    bp.add_argument("profile", nargs="?", help="the profile (default: default)")
    bp.add_argument("--country", help="where it comes out, a two-letter code like us (DataImpulse); '' for any")
    bo = brs.add_parser("open", help="open an address in a new tab")
    bo.add_argument("url")
    bo.add_argument("--profile", help="the profile (default: default)")
    for q in brs.choices.values():
        q.add_argument("--json", action="store_true")
    e = add("events", cmd_events, "the event log")
    e.add_argument("-n", type=int, default=30)
    e.add_argument("-f", "--follow", action="store_true")
    add("connect", cmd_connect, "how to connect Claude Code on your computer (MCP)").add_argument(
        "--url", help="the farm's public URL (default FARM_PUBLIC_URL, else localhost)")
    add("connections", cmd_connections, "the MCP clients connected to this farm")
    add("disconnect", cmd_disconnect, "end an MCP connection").add_argument("id")
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
