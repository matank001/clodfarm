"""When a scheduled task runs next: a cron line (in a time zone), every N seconds, or once at a time.

Cron is the classic five fields, minute hour day-of-month month day-of-week, with ``*``, lists, ranges and steps
(``*/15``, ``1-5``, ``0,30``); day-of-week 0 or 7 is Sunday. As in cron, when both day fields are restricted a day
matching either runs. Standard library only.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
_NAMES = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10,
          "nov": 11, "dec": 12, "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
_ALIASES = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *", "@weekly": "0 0 * * 0",
            "@monthly": "0 0 1 * *", "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *"}


def _field(text: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in text.lower().split(","):
        body, _, step = part.partition("/")
        if body == "*":
            a, b = lo, hi
        elif "-" in body:
            a, b = (int(_NAMES.get(x, x)) for x in body.split("-", 1))
        else:
            a = int(_NAMES.get(body, body))
            b = hi if step else a
        n = int(step) if step else 1
        if not (lo <= a <= hi and lo <= b <= hi) or a > b or n < 1:
            raise ValueError(f"cron field '{part}' is outside {lo}-{hi}")
        out |= set(range(a, b + 1, n))
    return out


def parse_cron(line: str) -> list[set[int]]:
    parts = _ALIASES.get(line.strip().lower(), line).split()
    if len(parts) != 5:
        raise ValueError("a cron line has five fields: minute hour day-of-month month day-of-week (e.g. '0 9 * * 1-5')")
    f = [_field(p, lo, hi) for p, (lo, hi) in zip(parts, _RANGES)]
    if 7 in f[4]:
        f[4] = (f[4] - {7}) | {0}
    return f + [set(parts[2:3]) != {"*"}, set(parts[4:5]) != {"*"}]  # type: ignore[list-item]


def _day_ok(f, d: dt.datetime) -> bool:
    dom, dow = d.day in f[2], (d.isoweekday() % 7) in f[4]
    dom_set, dow_set = f[5], f[6]
    if dom_set and dow_set:
        return dom or dow
    return dom and dow


def cron_next(line: str, after: float, tz: str = "UTC") -> float | None:
    """The first time strictly after ``after`` (epoch seconds) that the cron line matches, in zone ``tz``."""
    f, zone = parse_cron(line), ZoneInfo(tz)
    t = dt.datetime.fromtimestamp(after, zone).replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
    end = t + dt.timedelta(days=366 * 5)
    while t < end:
        if t.month not in f[3] or not _day_ok(f, t):
            t = (t + dt.timedelta(days=1)).replace(hour=0, minute=0)
            continue
        if t.hour not in f[1]:
            t = (t + dt.timedelta(hours=1)).replace(minute=0)
            continue
        if t.minute not in f[0]:
            t += dt.timedelta(minutes=1)
            continue
        return t.timestamp()
    return None


def parse_every(text: str) -> int:
    """'90s', '15m', '2h', '1d', '1w' (or plain seconds) to seconds."""
    text = text.strip().lower()
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    n = int(float(text[:-1]) * mult[text[-1]]) if text and text[-1] in mult else int(text)
    if n < 60:
        raise ValueError("run it at most once a minute")
    return n


def parse_at(text: str, tz: str = "UTC", now: float | None = None) -> float:
    """'2026-09-27T09:00' (in ``tz``), '...Z' / '+02:00' (as given), or 'in 2h' / '+30m' (from now)."""
    s = text.strip()
    for pre in ("in ", "+"):
        if s.lower().startswith(pre):
            base = now if now is not None else dt.datetime.now(dt.timezone.utc).timestamp()
            return base + parse_every(s[len(pre):])
    d = dt.datetime.fromisoformat(s.replace("Z", "+00:00").replace(" ", "T"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=ZoneInfo(tz))
    return d.timestamp()


def next_run(spec: dict, after: float) -> float | None:
    """For a schedule item (cron / every / at, tz): when it runs next after ``after``; None when never again."""
    if spec.get("cron"):
        return cron_next(spec["cron"], after, spec.get("tz") or "UTC")
    if spec.get("every"):
        start = float(spec.get("next_at") or after)
        n = max(1, int((after - start) // int(spec["every"])) + 1) if after >= start else 0
        return start + n * int(spec["every"])
    if spec.get("at") is not None:
        return float(spec["at"]) if float(spec["at"]) > after else None
    return None


def describe(spec: dict) -> str:
    if spec.get("cron"):
        return f"cron '{spec['cron']}' ({spec.get('tz') or 'UTC'})"
    if spec.get("every"):
        s = int(spec["every"])
        return "every " + next(f"{s // u}{n}" for u, n in ((604800, "w"), (86400, "d"), (3600, "h"), (60, "m"), (1, "s"))
                               if s % u == 0)
    return "once"
