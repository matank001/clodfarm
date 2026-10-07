"""Scrubbing: what the farm shows to people who aren't its own (a broadcast, a public page) carries no secret.

Two passes over every text: the farm's own secret values, exactly (its login, its connectors' keys, its bots' keys,
its relays' tokens, the host's), and anything shaped like a key, a token, an email address or a card number.
"""

from __future__ import annotations

import glob
import json
import os
import re
import threading
import time

PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S), "[a private key]"),
    (re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{8,}"), "[a Stripe key]"),
    (re.compile(r"\bwhsec_[A-Za-z0-9]{16,}"), "[a webhook secret]"),
    (re.compile(r"\bsk-(?:proj-|ant-|or-)?[A-Za-z0-9_-]{16,}"), "[an API key]"),
    (re.compile(r"\bxai-[A-Za-z0-9]{20,}"), "[an API key]"),
    (re.compile(r"\bgsk_[A-Za-z0-9]{20,}"), "[an API key]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), "[an API key]"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[an AWS key]"),
    (re.compile(r"\b(?:ghp|gho|ghs|ghu|ghr|github_pat)_[A-Za-z0-9_]{20,}"), "[a GitHub token]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "[a Slack token]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "[a token]"),
    (re.compile(r"(?i)\b(bearer|token|api[_-]?key|secret|password|passwd)(\s*[:=]\s*|\s+)([\"']?)[^\s\"',;]{12,}\3"),
     r"\1\2[hidden]"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[an email]"),
]
CARD = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
SECRET_ENV = re.compile(r"(?i)(token|key|secret|password|passwd|credential)")


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for d in reversed(digits):
        n = int(d)
        if alt:
            n = n * 2 - 9 if n > 4 else n * 2
        total, alt = total + n, not alt
    return total % 10 == 0


def _card(m: re.Match) -> str:
    digits = re.sub(r"\D", "", m.group(0))
    return "[a card number]" if 13 <= len(digits) <= 19 and _luhn(digits) else m.group(0)


def _strings(obj, out: set):
    if isinstance(obj, str):
        if len(obj) >= 12 and not obj.startswith(("http://", "https://", "/")):
            out.add(obj)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and SECRET_ENV.search(str(k)) or not isinstance(v, str):
                _strings(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _strings(v, out)


_cache: tuple[float, list[str]] = (0.0, [])
_lock = threading.Lock()


def secrets(workspace: str | None = None, max_age: float = 60) -> list[str]:
    """The farm's own secret values: the environment's, its connectors', its bots' keys and its relays' tokens,
    longest first (so a longer secret is hidden before a shorter one inside it)."""
    global _cache
    with _lock:
        if time.time() - _cache[0] < max_age:
            return _cache[1]
    ws = workspace or os.environ.get("FARM_WORKSPACE") or "/workspace"
    found: set[str] = set()
    for k, v in os.environ.items():
        if SECRET_ENV.search(k) and v and len(v) >= 12:
            found.add(v)
    paths = glob.glob(os.path.join(ws, ".farm", "connectors", "*.json")) + \
        glob.glob(os.path.join(ws, ".farm", "relay", "*.json")) + glob.glob(os.path.join(ws, ".farm", "ui-keys.json"))
    try:
        agents = json.load(open(os.path.join(ws, ".farm", "agents.json"))).get("agents", [])
        paths += [os.path.join(a["config_dir"], "bot.json") for a in agents if a.get("config_dir")]
    except (OSError, ValueError, AttributeError, KeyError):
        pass
    for p in paths:
        try:
            _strings(json.load(open(p)), found)
        except (OSError, ValueError):
            continue
    out = sorted(found, key=len, reverse=True)
    with _lock:
        _cache = (time.time(), out)
    return out


def text(s: str, known: list[str] | None = None) -> str:
    """``s`` with every secret value and secret-looking string replaced."""
    if not s:
        return s
    for v in known if known is not None else secrets():
        if v in s:
            s = s.replace(v, "[hidden]")
    for rx, repl in PATTERNS:
        s = rx.sub(repl, s)
    return CARD.sub(_card, s)


def obj(o, known: list[str] | None = None):
    """Every string in ``o`` (dicts and lists, all the way down), scrubbed."""
    known = secrets() if known is None else known
    if isinstance(o, str):
        return text(o, known)
    if isinstance(o, dict):
        return {k: obj(v, known) for k, v in o.items()}
    if isinstance(o, list):
        return [obj(v, known) for v in o]
    return o
