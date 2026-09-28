"""Bots: farm members that run another model (a free or a local one) through Claude Code.

A bot is added like any other Claude (the farm UI's + NEW CLAUDE, or ``clodfarm bot add``): its own Claude config
dir, its own ``clodfarm run``. Instead of a Claude login it has a provider that speaks Anthropic's Messages API
(OpenRouter, a local Ollama, a LiteLLM gateway...), and Claude Code talks to it through ``ANTHROPIC_BASE_URL``. It is
still Claude Code, so everything the farm does works the same: sub-agents, resume, messages, hooks, the browser
tools. Only the model differs.

- It runs no Remote Control (that needs a Claude login): nobody talks to it, it takes sub-agents.
- It takes only the sub-agents sent to it (``clodfarm spawn --on <bot>``), unless it was added to take any. Its own
  sub-agents stay on it, so a bot never spends a Claude account's usage.
- It is paced like API key mode (no subscription windows), and pauses when its provider rate-limits it.
- Its API key is kept in its own config dir (``bot.json``, readable by the farm's user only) and never shown again.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

PROVIDERS = {
    "openrouter": {"label": "OpenRouter", "url": "https://openrouter.ai/api", "key": True,
                   "example": "qwen/qwen3-coder:free"},
    # the farm runs in a container: Ollama on the host is host.docker.internal, not localhost
    "ollama": {"label": "Ollama", "url": "http://host.docker.internal:11434", "key": False, "example": "qwen3-coder"},
    "custom": {"label": "Anthropic-compatible", "url": "", "key": False, "example": ""},
}
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}")
KEY_FILE = "bot.json"
MAX_WORKERS = 4


def parse(data: dict) -> dict:
    """A bot's settings from what the UI or the CLI sent: ``provider``, ``url``, ``model``, ``takes`` ("sent": only
    the sub-agents sent to it; "any": any sub-agent) and ``workers``. Raises ValueError with what is wrong."""
    provider = str(data.get("provider") or "custom").strip().lower()
    if provider not in PROVIDERS:
        raise ValueError(f"the provider is one of {', '.join(PROVIDERS)}")
    url = str(data.get("url") or PROVIDERS[provider]["url"]).strip().rstrip("/")
    url = re.sub(r"/v1(/messages)?$", "", url)  # Claude Code adds /v1/messages itself
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.query or u.fragment \
            or any(c.isspace() for c in url):
        raise ValueError("the address is the provider's base URL, like https://openrouter.ai/api")
    model = str(data.get("model") or "").strip()
    if not MODEL_RE.fullmatch(model):
        raise ValueError("name the model it runs, like " + (PROVIDERS[provider]["example"] or "the provider calls it"))
    takes = "any" if str(data.get("takes") or "sent") == "any" else "sent"
    try:
        workers = int(data.get("workers") or 1)
    except (TypeError, ValueError):
        raise ValueError("workers is a number") from None
    if not 1 <= workers <= MAX_WORKERS:
        raise ValueError(f"a bot runs 1 to {MAX_WORKERS} sub-agents at a time")
    return {"provider": provider, "url": url, "model": model, "takes": takes, "workers": workers}


def check_key(provider: str, key: str) -> str:
    key = (key or "").strip()
    if len(key) > 500 or any(c.isspace() or ord(c) < 32 for c in key):
        raise ValueError("that doesn't look like an API key")
    if PROVIDERS[provider]["key"] and not key:
        raise ValueError(f"{PROVIDERS[provider]['label']} needs an API key")
    return key


def check(bot: dict, key: str, timeout: float = 45) -> str:
    """Ask the model for one word, the way Claude Code will (``Authorization: Bearer``), so a bot is kept only once
    its provider answers. Returns what it said. Raises ValueError with what went wrong."""
    body = json.dumps({"model": bot["model"], "max_tokens": 16,
                       "messages": [{"role": "user", "content": "Reply with the one word: ok"}]}).encode()
    headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(bot["url"] + "/v1/messages", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            got = json.loads(r.read(1 << 20) or b"{}")
    except urllib.error.HTTPError as e:
        detail = _error_text(e)
        if e.code == 429:
            return "(rate limited right now: the key works)"
        if e.code in (401, 403):
            raise ValueError(f"the provider refused the key ({e.code}{': ' + detail if detail else ''})") from None
        if e.code == 404:
            raise ValueError(f"no such model, or {bot['url']} doesn't speak Anthropic's Messages API "
                             f"(404{': ' + detail if detail else ''})") from None
        raise ValueError(f"the provider answered {e.code}{': ' + detail if detail else ''}") from None
    except (urllib.error.URLError, OSError) as e:
        raise ValueError(f"can't reach {bot['url']} ({getattr(e, 'reason', e)})") from None
    except ValueError:
        raise ValueError(f"{bot['url']} answered, but not with Anthropic's Messages API") from None
    if not isinstance(got, dict) or not isinstance(got.get("content"), list):
        raise ValueError(f"{bot['url']} answered, but not with Anthropic's Messages API")
    text = "".join(b.get("text", "") for b in got["content"] if isinstance(b, dict)).strip()
    return text[:80] or "(an empty answer)"


def _error_text(e: urllib.error.HTTPError) -> str:
    try:
        d = json.loads(e.read(1 << 16) or b"{}")
    except (OSError, ValueError):
        return ""
    err = d.get("error") if isinstance(d, dict) else None
    msg = err.get("message") if isinstance(err, dict) else err if isinstance(err, str) else ""
    return str(msg or "")[:200]


def save_key(config_dir: str, key: str):
    """Keep the bot's API key in its own config dir, readable by the farm's user only."""
    os.makedirs(config_dir, mode=0o700, exist_ok=True)
    path = os.path.join(config_dir, KEY_FILE)
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"key": key}, f)
    os.replace(path + ".tmp", path)


def load_key(config_dir: str) -> str:
    try:
        return str(json.load(open(os.path.join(config_dir, KEY_FILE))).get("key") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def label(bot: dict) -> str:
    return PROVIDERS.get(bot.get("provider") or "", PROVIDERS["custom"])["label"]


def env(agent: dict) -> dict:
    """What a bot's ``clodfarm run`` (and every Claude Code it starts) gets: its provider in place of a Claude login,
    its model for every model Claude Code picks, and the farm settings of a bot."""
    bot, m = agent["bot"], agent["bot"]["model"]
    return {
        "ANTHROPIC_BASE_URL": bot["url"],
        # Claude Code sends this as a Bearer token; a provider without keys (Ollama) takes any
        "ANTHROPIC_AUTH_TOKEN": load_key(agent["config_dir"]) or "none",
        "ANTHROPIC_API_KEY": "",
        "ANTHROPIC_MODEL": m, "ANTHROPIC_DEFAULT_OPUS_MODEL": m, "ANTHROPIC_DEFAULT_SONNET_MODEL": m,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": m, "ANTHROPIC_SMALL_FAST_MODEL": m, "CLAUDE_CODE_SUBAGENT_MODEL": m,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "FARM_BOT": m, "FARM_BOT_VIA": label(bot), "FARM_BOT_TAKES": bot.get("takes") or "sent",
        "FARM_MODEL": m, "FARM_EFFORT": "", "FARM_REMOTE_CONTROL": "0", "FARM_USAGE_REFRESH": "0",
        "FARM_MAX_WORKERS": str(bot.get("workers") or 1), "FARM_SEAT": f"bot-{agent['id']}",
        # Claude Code prices every run as if it were a Claude model: that is no bot's real cost
        "FARM_DAILY_BUDGET_USD": "0", "FARM_TASK_BUDGET_USD": "0",
    }
