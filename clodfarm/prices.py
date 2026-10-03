"""List prices for the models bots run through the relay: what a bot's work really costs.

Claude Code prices every run as if it were a Claude model, which is no bot's real cost. A bot on the relay is priced
here instead: US dollars per million tokens (input, cached input, output), by the longest model-name prefix that
matches. Prices change: the farm manager sets a bot's own (``--price-in/--price-out/--price-cached``), which wins over
this table. A model in neither costs $0 (and `clodfarm bot add` says so).
"""

from __future__ import annotations

# provider -> model prefix -> (input, cached input, output) USD per million tokens, list prices as published (the
# short-context tier: a model that charges more past 200k-272k input tokens is counted at its base price)
TABLE: dict[str, dict[str, tuple[float, float, float]]] = {
    "openai": {"gpt-4o-mini": (0.15, 0.075, 0.60), "gpt-4o": (2.50, 1.25, 10.00), "gpt-4.1-nano": (0.10, 0.025, 0.40),
               "gpt-4.1-mini": (0.40, 0.10, 1.60), "gpt-4.1": (2.00, 0.50, 8.00), "gpt-5-nano": (0.05, 0.005, 0.40),
               "gpt-5-mini": (0.25, 0.025, 2.00), "gpt-5": (1.25, 0.125, 10.00), "o4-mini": (1.10, 0.275, 4.40),
               "o3": (2.00, 0.50, 8.00), "gpt-6.1-sol": (2.00, 0.10, 10.00)},
    "xai": {"grok-4.7": (2.00, 0.50, 6.00), "grok-4": (3.00, 0.75, 15.00), "grok-3-mini": (0.30, 0.075, 0.50),
            "grok-3": (3.00, 0.75, 15.00), "grok-code-fast": (0.20, 0.02, 1.50)},
    "gemini": {"gemini-3.1-pro": (2.00, 0.20, 12.00), "gemini-2.5-pro": (1.25, 0.31, 10.00),
               "gemini-2.5-flash-lite": (0.10, 0.025, 0.40), "gemini-2.5-flash": (0.30, 0.075, 2.50),
               "gemini-2.0-flash": (0.10, 0.025, 0.40)},
    "groq": {"llama-3.3-70b": (0.59, 0.59, 0.79), "llama-3.1-8b": (0.05, 0.05, 0.08),
             "openai/gpt-oss-120b": (0.15, 0.15, 0.75), "openai/gpt-oss-20b": (0.10, 0.10, 0.50),
             "moonshotai/kimi-k2": (1.00, 0.50, 3.00)},
}


def price(provider: str, model: str, own: dict | None = None) -> tuple[float, float, float] | None:
    """(input, cached input, output) per million tokens: the bot's own price, else the table's, else None."""
    own = own or {}
    if own.get("in") is not None or own.get("out") is not None:
        i, o = float(own.get("in") or 0), float(own.get("out") or 0)
        return i, float(own["cached"]) if own.get("cached") is not None else i, o
    m = (model or "").lower().split("/", 1)[1] if provider == "openrouter" else (model or "").lower()
    rows = TABLE.get(provider) or {}
    best = max((p for p in rows if m.startswith(p)), key=len, default=None)
    return rows[best] if best else None


def cost(provider: str, model: str, usage: dict | None, own: dict | None = None) -> float:
    """What a run's usage (Anthropic-shaped, as Claude Code reports it) cost at list price."""
    p = price(provider, model, own)
    if not p:
        return 0.0
    u = usage or {}
    fresh = int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0)
    return (fresh * p[0] + int(u.get("cache_read_input_tokens") or 0) * p[1]
            + int(u.get("output_tokens") or 0) * p[2]) / 1e6
