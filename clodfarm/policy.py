"""What a Claude may use, as its person chose when they hatched it (and can change in its SETTINGS).

The choice is kept in the store (``CLAUDE/<id>``, see store.py) and copied to ``farm-policy.json`` in that Claude's
Claude Code config dir, so the PreToolUse hook (``clodfarm hook``, run before every tool call) decides without a
round trip. A change applies to the next tool call, in every session, without a restart.

Tools come in groups, the way a person thinks of them. Read, Glob and Grep are always allowed. A Claude without the
shell still runs `clodfarm ...` commands: that is how it talks to the farm.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re

POLICY_FILE = "farm-policy.json"

# group -> (label, tool name patterns)
GROUPS: dict[str, tuple[str, list[str]]] = {
    "shell": ("Shell (Bash)", ["Bash", "BashOutput", "KillShell", "KillBash"]),
    "edit": ("Edit files", ["Edit", "MultiEdit", "Write", "NotebookEdit"]),
    "web": ("Web (fetch, search)", ["WebFetch", "WebSearch"]),
    "agents": ("Sub-agents (Task)", ["Task", "Agent"]),
    "messaging": ("Message other sessions", ["SendMessage"]),
    "browser": ("The farm's browser", ["mcp__browser*"]),
    "stripe": ("Stripe (payments)", ["mcp__stripe__*"]),
    "blender": ("Blender (3D)", ["mcp__blender__*"]),
    "mcp": ("Other MCP tools", ["mcp__*"]),
}
# built-in tools that --disallowedTools can name as they are (the hook covers the shell and MCP patterns)
_FLAGGABLE = {"edit", "web", "agents", "messaging"}
FARM_COMMAND = re.compile(r"^\s*(?:[A-Z_][A-Z0-9_]*=\S*\s+)*(?:\S*/)?clodfarm(?:\s|$)")


def groups_view() -> list[dict]:
    return [{"id": g, "label": label} for g, (label, _) in GROUPS.items()]


def clean(tools) -> dict:
    """``"all"``, or {"deny": [group, ...]} with only known groups."""
    if tools in (None, "all") or (isinstance(tools, dict) and not tools.get("deny")):
        return {"deny": []}
    deny = tools.get("deny") if isinstance(tools, dict) else tools
    if not isinstance(deny, list):
        raise ValueError("tools: 'all' or {\"deny\": [groups]}")
    return {"deny": sorted({str(g) for g in deny if str(g) in GROUPS})}


def load(config_dir: str) -> dict:
    if not config_dir:
        return {"deny": []}
    try:
        with open(os.path.join(config_dir, POLICY_FILE)) as f:
            return clean(json.load(f).get("tools"))
    except (OSError, ValueError, AttributeError):
        return {"deny": []}


def save(config_dir: str, tools, claude: str = ""):
    """Written only when something is turned off: the hook's shell test then starts no Python for this Claude."""
    path = os.path.join(config_dir, POLICY_FILE)
    tools = clean(tools)
    if not tools["deny"]:
        try:
            os.remove(path)
        except OSError:
            pass
        return
    os.makedirs(config_dir, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"tools": tools, "claude": claude}, f)
    os.replace(tmp, path)


def disallowed_flags(pol: dict) -> list[str]:
    out = []
    for g in pol.get("deny", []):
        if g in _FLAGGABLE:
            out += GROUPS[g][1]
    return out


def _group_of(tool: str) -> str | None:
    for g, (_, pats) in GROUPS.items():  # browser is listed before mcp: the first match wins
        if any(fnmatch.fnmatchcase(tool, p) for p in pats):
            return g
    return None


def decide(pol: dict, tool: str, tool_input: dict | None = None) -> tuple[bool, str]:
    """(allowed, reason) for one tool call."""
    deny = set(pol.get("deny", []))
    if not deny or not tool:
        return True, ""
    g = _group_of(tool)  # the browser's tools are their own group: "other MCP tools" leaves them on
    if g is None or g not in deny:
        return True, ""
    if g == "shell" and tool == "Bash":
        cmd = str((tool_input or {}).get("command") or "")
        if FARM_COMMAND.match(cmd) and not re.search(r"[;&|`$<>]", cmd):
            return True, ""  # talking to the farm is always allowed
    label = GROUPS[g][0]
    return False, (f"{label} is turned off for this Claude by its person (farm SETTINGS). Do the work without it, "
                   f"or ask your person to allow it. `clodfarm ...` commands still work.")


def hook_output(allowed: bool, reason: str) -> dict | None:
    if allowed:
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}
