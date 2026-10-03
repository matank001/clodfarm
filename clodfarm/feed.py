"""The live feed: what the farm's agents think, say and do, as they do it (the farm UI's and a broadcast's).

Each assistant event of a sub-agent's stream-json becomes feed items: its thinking (Claude's own, or another model's
reasoning through the relay), what it says, and each tool it calls (the tool's name and a short line of what it does).
They are kept for a day in the store (``LIVE``); a run a new release adopts is read again from its first line, so its
feed remembers how far it got (``feed.json`` in the run's directory).
"""

from __future__ import annotations

import json
import os

from . import procs

CLIP = 500
# what a tool call is about, in a line: the first of these its input has
TOOL_KEYS = ("command", "description", "file_path", "path", "pattern", "url", "query", "prompt", "title", "to",
             "recipient", "name")


def _clip(s: str, n: int = CLIP) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def tool_line(name: str, inp) -> str:
    if not isinstance(inp, dict):
        return name
    for k in TOOL_KEYS:
        v = inp.get(k)
        if isinstance(v, str) and v.strip():
            return f"{name}: {_clip(v, 200)}"
    return name


def items(ev: dict) -> list[dict]:
    """The feed items in one stream-json event (none for anything but what the agent itself says and does)."""
    if not isinstance(ev, dict) or ev.get("type") != "assistant":
        return []
    out = []
    for b in (ev.get("message") or {}).get("content") or []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "thinking" and str(b.get("thinking") or "").strip():
            out.append({"kind": "thought", "text": _clip(b["thinking"])})
        elif t == "text" and str(b.get("text") or "").strip():
            out.append({"kind": "say", "text": _clip(b["text"])})
        elif t == "tool_use":
            out.append({"kind": "tool", "text": tool_line(str(b.get("name") or "a tool"), b.get("input")),
                        "tool": str(b.get("name") or "")[:80]})
    return out


def recorder(store, rundir: str, claude: str, task: str, owner: str | None = None):
    """An ``on_line`` for run_agent: feeds the run's items to the store, and skips what an earlier reader of the same
    run (the release before a hand-over) fed already."""
    path = os.path.join(rundir, "feed.json")
    skip = int(procs.read_json(path).get("n") or 0)
    seen = [0]

    def on_line(ev: dict):
        seen[0] += 1
        if seen[0] <= skip:
            return
        got = items(ev)
        if got:
            try:
                store.live_add(got, claude=claude, task=task, owner=owner)
            except Exception as e:  # noqa: BLE001 - the feed must never stop a run
                print(f"feed: {e!r}", flush=True)
            try:
                procs.write_json(path, {"n": seen[0]})
            except OSError:
                pass
    return on_line


def dumps(item: dict) -> str:
    return json.dumps(item, separators=(",", ":"))
