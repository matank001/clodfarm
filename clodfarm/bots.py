"""Bots: farm members that run another model (OpenAI, Grok, Gemini, a free or a local one) through Claude Code.

A bot is added like any other Claude (the farm UI's + ADD AGENT → ADD AGENT WITH API KEY, or ``clodfarm bot add``): its own Claude config
dir, its own ``clodfarm run``. Instead of a Claude login it has a provider, and Claude Code talks to it through
``ANTHROPIC_BASE_URL``: straight to one that speaks Anthropic's Messages API (OpenRouter, a local Ollama, a LiteLLM
gateway...), or through the bot's own relay (relay.py) to one that speaks OpenAI's API (OpenAI through its Responses
API; xAI, Gemini, Groq and any OpenAI-compatible server through Chat Completions). It is still Claude Code, so everything the farm does works the same:
sub-agents, resume, messages, hooks, the browser tools. Only the model differs.

- It runs no Remote Control (that needs a Claude login): nobody talks to it, it takes sub-agents.
- It takes only the sub-agents sent to it (``clodfarm spawn --on <bot>``), unless it was added to take any. Its own
  sub-agents stay on it, so a bot never spends a Claude account's usage.
- It is paced like API key mode (no subscription windows), and pauses when its provider rate-limits it.
- On the relay, its spend is counted at its model's list price (prices.py, or the price it was given), so a daily
  budget (``daily_usd``) can hold it.
- Its API key is kept in its own config dir (``bot.json``, readable by the farm's user only) and never shown again.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

# dialect: "anthropic" (Claude Code talks to it as is), "chat" (OpenAI's Chat Completions, through the relay) or
# "responses" (OpenAI's Responses API, through the relay: OpenAI's newest models take tools with reasoning only there)
PROVIDERS = {
    # latest: the model a bot gets when none is named (the provider's newest one that takes Claude Code's tools)
    "openai": {"label": "OpenAI", "url": "https://api.openai.com/v1", "key": True, "example": "gpt-6.1-sol",
               "latest": "gpt-6.1-sol", "dialect": "responses"},
    "xai": {"label": "xAI", "url": "https://api.x.ai/v1", "key": True, "example": "grok-4.7", "latest": "grok-4.7",
            "dialect": "chat"},
    "gemini": {"label": "Gemini", "url": "https://generativelanguage.googleapis.com/v1beta/openai", "key": True,
               "example": "gemini-3.1-pro-preview", "latest": "gemini-3.1-pro-preview", "dialect": "chat"},
    "groq": {"label": "Groq", "url": "https://api.groq.com/openai/v1", "key": True, "example": "openai/gpt-oss-120b",
             "latest": "openai/gpt-oss-120b", "dialect": "chat"},
    "openrouter": {"label": "OpenRouter", "url": "https://openrouter.ai/api", "key": True,
                   "example": "qwen/qwen3-coder:free", "latest": "qwen/qwen3-coder:free", "dialect": "anthropic"},
    # the farm runs in a container: Ollama on the host is host.docker.internal, not localhost
    "ollama": {"label": "Ollama", "url": "http://host.docker.internal:11434", "key": False, "example": "qwen3-coder",
               "dialect": "anthropic"},
    "custom": {"label": "Anthropic-compatible", "url": "", "key": False, "example": "", "dialect": "anthropic"},
    "openai-compatible": {"label": "OpenAI-compatible", "url": "", "key": False, "example": "", "dialect": "chat"},
}
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}")
KEY_FILE = "bot.json"
MAX_WORKERS = 4
EFFORTS = ("minimal", "low", "medium", "high")


def dialect(bot: dict) -> str:
    return PROVIDERS.get(bot.get("provider") or "", PROVIDERS["custom"])["dialect"]


def relayed(bot: dict) -> bool:
    """It runs through its own relay (relay.py): its provider speaks OpenAI's API, not Anthropic's."""
    return dialect(bot) in ("chat", "responses")


def _num(data: dict, k: str, what: str, lo: float, hi: float) -> float | None:
    v = data.get(k)
    if v is None or v == "":
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{what} is a number") from None
    if not lo <= v <= hi:
        raise ValueError(f"{what} is between {lo:g} and {hi:g}")
    return v


def parse(data: dict) -> dict:
    """A bot's settings from what the UI or the CLI sent: ``provider``, ``url``, ``model``, ``takes`` ("sent": only
    the sub-agents sent to it; "any": any sub-agent) and ``workers``. Raises ValueError with what is wrong."""
    provider = str(data.get("provider") or "custom").strip().lower()
    if provider not in PROVIDERS:
        raise ValueError(f"the provider is one of {', '.join(PROVIDERS)}")
    url = str(data.get("url") or PROVIDERS[provider]["url"]).strip().rstrip("/")
    if PROVIDERS[provider]["dialect"] in ("chat", "responses"):
        url = re.sub(r"/(chat/completions|responses)$", "", url)  # the relay adds it
    else:
        url = re.sub(r"/v1(/messages)?$", "", url)  # Claude Code adds /v1/messages itself
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.query or u.fragment \
            or any(c.isspace() for c in url):
        raise ValueError("the address is the provider's base URL, like https://openrouter.ai/api")
    model = str(data.get("model") or "").strip() or PROVIDERS[provider].get("latest", "")  # none named: its latest
    if not MODEL_RE.fullmatch(model):
        raise ValueError("name the model it runs, like " + (PROVIDERS[provider]["example"] or "the provider calls it"))
    takes = "any" if str(data.get("takes") or "sent") == "any" else "sent"
    try:
        workers = int(data.get("workers") or 1)
    except (TypeError, ValueError):
        raise ValueError("workers is a number") from None
    if not 1 <= workers <= MAX_WORKERS:
        raise ValueError(f"a bot runs 1 to {MAX_WORKERS} sub-agents at a time")
    out = {"provider": provider, "url": url, "model": model, "takes": takes, "workers": workers}
    # the relay's own settings, kept only when given
    effort = str(data.get("effort") or "").strip().lower()
    if effort:
        if effort not in EFFORTS:
            raise ValueError(f"effort is one of {', '.join(EFFORTS)}")
        out["effort"] = effort
    max_out = _num(data, "max_out", "max output tokens", 256, 1_000_000)
    if max_out:
        out["max_out"] = int(max_out)
    price = {k: v for k, v in (("in", _num(data, "price_in", "the input price", 0, 1000)),
                               ("cached", _num(data, "price_cached", "the cached input price", 0, 1000)),
                               ("out", _num(data, "price_out", "the output price", 0, 1000))) if v is not None}
    if price:
        out["price"] = price
    daily = _num(data, "daily_usd", "the daily budget", 0, 100_000)
    if daily:
        out["daily_usd"] = daily
    if (effort or max_out) and PROVIDERS[provider]["dialect"] == "anthropic":
        raise ValueError("effort and max output are for providers on the relay (OpenAI, xAI, Gemini, Groq...)")
    return out


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
    if relayed(bot):
        return _check_chat(bot, key, timeout)
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


PROBE_TOOL = {"name": "farm_probe", "description": "Not needed now.",
              "input_schema": {"type": "object", "properties": {"note": {"type": "string"}}, "required": []}}


def _check_chat(bot: dict, key: str, timeout: float) -> str:
    """The same probe through the relay's translation, with a tool on offer the way Claude Code always has some: a
    provider that can't take Claude Code's tools fails here, not on its first sub-agent."""
    from . import relay
    st = {**relay.settings_from_env({}), "provider": bot["provider"], "dialect": dialect(bot), "url": bot["url"],
          "model": bot["model"], "effort": bot.get("effort") or "", "max_out": 0}
    r = relay.Relay(st, key, token="", timeout=timeout)
    # room to think first: a thinking model spends its first tokens on that
    api = "Responses" if r.responses else "Chat Completions"
    req = {"model": bot["model"], "max_tokens": 2048, "tools": [PROBE_TOOL],
           "messages": [{"role": "user", "content": "Reply with the one word: ok"}]}
    try:
        resp, names = r.ask(req)
        with resp:
            got = json.loads(resp.read(1 << 20) or b"{}")
    except urllib.error.HTTPError as e:
        detail = _error_text(e)
        if e.code == 429:
            return "(rate limited right now: the key works)"
        if e.code in (401, 403):
            raise ValueError(f"the provider refused the key ({e.code}{': ' + detail if detail else ''})") from None
        if e.code == 404:
            raise ValueError(f"no such model, or {bot['url']} doesn't speak OpenAI's {api} API "
                             f"(404{': ' + detail if detail else ''})") from None
        raise ValueError(f"the provider answered {e.code}{': ' + detail if detail else ''}") from None
    except (urllib.error.URLError, OSError) as e:
        raise ValueError(f"can't reach {bot['url']} ({getattr(e, 'reason', e)})") from None
    except ValueError:
        raise ValueError(f"{bot['url']} answered, but not with OpenAI's {api} API") from None
    if not isinstance(got, dict) or not isinstance(got.get("output" if r.responses else "choices"), list):
        raise ValueError(f"{bot['url']} answered, but not with OpenAI's {api} API")
    msg = r.answer(got, names)
    text = "".join(b.get("text", "") for b in msg["content"] if b.get("type") == "text").strip()
    if not text and any(b.get("type") == "tool_use" for b in msg["content"]):
        return "(it called a tool: tools work)"
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
    its model for every model Claude Code picks, and the farm settings of a bot. A bot on the relay gets the relay's
    address and token from its ``clodfarm run`` once the relay is up (relay.ensure); its key stays with the relay."""
    bot, m = agent["bot"], agent["bot"]["model"]
    if relayed(bot):
        return {**_env(agent, m, "http://127.0.0.1:9", "relay-not-started"),
                "FARM_BOT_PROVIDER": bot["provider"], "FARM_BOT_DIALECT": dialect(bot), "FARM_BOT_UPSTREAM": bot["url"],
                "FARM_BOT_OPTS": json.dumps({k: bot[k] for k in ("effort", "max_out", "price") if bot.get(k)}),
                "FARM_DAILY_BUDGET_USD": str(bot.get("daily_usd") or 0)}
    # Claude Code sends ANTHROPIC_AUTH_TOKEN as a Bearer token; a provider without keys (Ollama) takes any
    return _env(agent, m, bot["url"], load_key(agent["config_dir"]) or "none")


def _env(agent: dict, m: str, url: str, token: str) -> dict:
    bot = agent["bot"]
    return {
        "ANTHROPIC_BASE_URL": url,
        "ANTHROPIC_AUTH_TOKEN": token,
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
