"""Every Claude session on the farm, and its whole conversation, in the farm's store.

Claude Code calls ``clodfarm hook`` (installed in each Claude's settings.json) when a session starts, before each
prompt, after each reply and when it ends. The hook registers the session and copies the transcript lines written
since the last call into the store: your messages, Claude's replies, and each tool call and result in short. So
conversations from the Claude app, the sessions opened from it and every sub-agent run are all recorded, on every box.
"""

from __future__ import annotations

import json
import os

MAX_TEXT = 20000  # per turn; a store item must stay far below DynamoDB's 400 KB
MAX_TOOL = 600


def _clip(text: str, n: int) -> str:
    text = text.strip()
    return text if len(text) <= n else text[:n] + f" … [{len(text) - n} more characters]"


def _blocks(content) -> list[tuple[str, str]]:
    """(kind, text) for one message's content: text, tool calls, tool results. Thinking is left out."""
    if isinstance(content, str):
        return [("text", content)]
    out = []
    for b in content or []:
        t = b.get("type")
        if t == "text" and b.get("text", "").strip():
            out.append(("text", b["text"]))
        elif t == "tool_use":
            args = json.dumps(b.get("input") or {}, ensure_ascii=False)
            out.append(("tool", f"{b.get('name', 'tool')}({_clip(args, MAX_TOOL)})"))
        elif t == "tool_result":
            c = b.get("content")
            if isinstance(c, list):
                c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
            out.append(("tool_result", _clip(str(c or ""), MAX_TOOL)))
    return out


def read_transcript(path: str, offset: int = 0) -> tuple[list[dict], int, dict]:
    """The turns written to a Claude Code transcript (JSON lines) after byte ``offset``.

    Returns (turns, new offset, meta). Only whole lines are read, so a line Claude Code is still writing is picked up
    next time. ``meta`` carries what the transcript says about the session: its title and Remote Control session."""
    turns, meta = [], {}
    try:
        f = open(path, "rb")
    except OSError:
        return turns, offset, meta
    with f:
        f.seek(offset)
        data = f.read()
    end = data.rfind(b"\n") + 1
    for raw in data[:end].splitlines():
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        t = d.get("type")
        if t == "ai-title" and d.get("aiTitle"):
            meta["title"] = d["aiTitle"]
        elif t == "bridge-session" and d.get("bridgeSessionId"):
            meta["remote_session"] = d["bridgeSessionId"]
        elif t in ("user", "assistant") and not d.get("isMeta") and not d.get("isSidechain"):
            role = (d.get("message") or {}).get("role") or t
            for kind, text in _blocks((d.get("message") or {}).get("content")):
                if kind == "text" and role == "user" and text.lstrip().startswith(("<command-", "<local-command", "<system-reminder")):
                    continue  # Claude Code's own bookkeeping, not what anyone said
                turns.append({"role": role, "kind": kind, "text": _clip(text, MAX_TEXT), "at": d.get("timestamp")})
    return turns, offset + end, meta


def session_kind(env=os.environ) -> str:
    tid = env.get("FARM_TASK_ID")
    return "usage" if tid == "usage" else "sub-agent" if tid else "conversation"
