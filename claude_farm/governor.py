"""Budget governor: decides how many agents may run right now.

Pure functions only (no I/O), so every rule is unit-tested.

Input is the latest ``rate_limit_event`` that Claude Code emits in
``--output-format stream-json`` mode. Its utilization numbers are
account-wide: they include every session on the subscription, including the
human's own, so the governor automatically backs off when you work yourself.

Rules, in order:

1. Never spend money. If the account is drawing on paid overage, stop.
2. Hard stops. ``status == "rejected"``, the 5-hour window above its ceiling or
   the 7-day window at its target: pause until that window resets.
3. Pacing. Spread the weekly allowance over the week so there is always room
   left for the human, and so agents don't burn the whole week on day two.
   The same pacing (with a wider band) applies inside each 5-hour window.
4. Optional end-of-week burst (off by default): within ``burst_hours`` of the
   weekly reset, skip weekly pacing and run at full concurrency up to the target.

API key mode has no subscription windows: only a daily dollar cap applies.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

FIVE_HOURS = 5 * 3600
SEVEN_DAYS = 7 * 24 * 3600


@dataclass
class Window:
    utilization: float  # 0.0 .. 1.0
    resets_at: float  # epoch seconds


@dataclass
class Snapshot:
    """The parts of a rate_limit_event the governor needs."""

    observed_at: float
    status: str = "allowed"  # allowed | allowed_warning | rejected
    rate_limit_type: str | None = None  # which window ``status`` refers to
    resets_at: float | None = None  # reset of that window
    five_hour: Window | None = None
    seven_day: Window | None = None
    using_overage: bool = False

    @classmethod
    def from_event(cls, info: dict, observed_at: float) -> "Snapshot":
        """Build from the ``rate_limit_info`` object of a rate_limit_event."""
        windows = info.get("unifiedWindows") or {}

        def win(key: str) -> Window | None:
            w = windows.get(key)
            if not w or w.get("utilization") is None or not w.get("resetsAt"):
                return None
            return Window(float(w["utilization"]), float(w["resetsAt"]))

        return cls(
            observed_at=observed_at,
            status=info.get("status", "allowed"),
            rate_limit_type=info.get("rateLimitType"),
            resets_at=float(info["resetsAt"]) if info.get("resetsAt") else None,
            five_hour=win("five_hour"),
            seven_day=win("seven_day"),
            using_overage=bool(info.get("isUsingOverage")),
        )

    def to_dict(self) -> dict:
        d = {
            "observed_at": self.observed_at,
            "status": self.status,
            "rate_limit_type": self.rate_limit_type,
            "resets_at": self.resets_at,
            "using_overage": self.using_overage,
        }
        for name in ("five_hour", "seven_day"):
            w = getattr(self, name)
            d[name] = None if w is None else {"utilization": w.utilization, "resets_at": w.resets_at}
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Snapshot":
        def win(x):
            return None if not x else Window(float(x["utilization"]), float(x["resets_at"]))

        return cls(
            observed_at=float(d["observed_at"]),
            status=d.get("status") or "allowed",
            rate_limit_type=d.get("rate_limit_type"),
            resets_at=float(d["resets_at"]) if d.get("resets_at") else None,
            five_hour=win(d.get("five_hour")),
            seven_day=win(d.get("seven_day")),
            using_overage=bool(d.get("using_overage")),
        )


@dataclass
class Policy:
    max_workers: int = 3
    weekly_target: float = 0.90  # stop at 90% of the 7-day window (10% left for you)
    five_hour_ceiling: float = 0.90  # never push a 5-hour window past this
    weekly_band: float = 0.05  # how far ahead of the weekly pace we may run
    five_hour_band: float = 0.30  # 5-hour pacing is loose: bursts are fine
    burst_hours: float = 0.0  # opt-in: this close to a weekly reset, run at full speed up to the target
    allow_overage: bool = False  # never draw on paid extra usage unless told to
    api_mode: bool = False  # ANTHROPIC_API_KEY: pay per token, no subscription windows
    daily_budget_usd: float = 0.0  # API mode: stop for the day at this spend (0 = no cap)


@dataclass
class Decision:
    workers: int
    reason: str
    pause_until: float | None = None  # set when workers == 0 and we know when to retry
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"workers": self.workers, "reason": self.reason,
                "pause_until": self.pause_until, "details": self.details}


def _live(w: Window | None, now: float) -> Window | None:
    """A window whose reset time has passed starts again from zero."""
    if w is None:
        return None
    if w.resets_at <= now:
        return None
    return w


def _pace(w: Window, length: float, target: float, band: float, now: float):
    """Return (fraction of full speed 0..1, time when fraction becomes > 0).

    The pace line is ``target * elapsed_fraction + band``. Running under it
    means we're behind schedule (full speed); running over it means we're
    ahead (stop until the line catches up).
    """
    elapsed = 1.0 - (w.resets_at - now) / length
    elapsed = min(max(elapsed, 0.0), 1.0)
    allowed = min(target, target * elapsed + band)
    headroom = allowed - w.utilization
    if headroom <= 0:
        # When does target*elapsed + band reach utilization?
        need_elapsed = (w.utilization - band) / target if target > 0 else 1.0
        start = w.resets_at - length
        resume = start + need_elapsed * length
        return 0.0, min(max(resume, now + 60), w.resets_at)
    return min(headroom / band, 1.0), None


def _next_utc_midnight(now: float) -> float:
    return (now // 86400 + 1) * 86400


def decide(snap: Snapshot | None, policy: Policy, now: float, spent_today: float = 0.0) -> Decision:
    n = max(policy.max_workers, 0)
    if n == 0:
        return Decision(0, "max_workers is 0")
    if policy.api_mode:
        # API key: there are no subscription windows to pace; the limit is money
        details = {"spent_today_usd": round(spent_today, 2), "daily_budget_usd": policy.daily_budget_usd}
        if snap and snap.status == "rejected" and snap.resets_at and snap.resets_at > now:
            return Decision(0, "API rate limited", snap.resets_at, details)
        if policy.daily_budget_usd and spent_today >= policy.daily_budget_usd:
            return Decision(0, f"daily API budget reached (${spent_today:.2f} of ${policy.daily_budget_usd:.2f})",
                            _next_utc_midnight(now), details)
        return Decision(n, "API key mode: within the daily budget" if policy.daily_budget_usd
                        else "API key mode: no daily cap set", None, details)
    if snap is None:
        return Decision(1, "no usage data yet: running one agent to measure")

    five = _live(snap.five_hour, now)
    seven = _live(snap.seven_day, now)
    details = {
        "five_hour": None if five is None else round(five.utilization, 4),
        "seven_day": None if seven is None else round(seven.utilization, 4),
    }

    # 1. money
    if snap.using_overage and not policy.allow_overage:
        until = snap.resets_at if snap.resets_at and snap.resets_at > now else None
        return Decision(0, "account is using paid overage: stopped (allow_overage is off)", until, details)

    # 2. hard stops
    if snap.status == "rejected" and snap.resets_at and snap.resets_at > now:
        return Decision(0, f"rate limited ({snap.rate_limit_type or 'unknown'} window)", snap.resets_at, details)
    if five and five.utilization >= policy.five_hour_ceiling:
        return Decision(0, f"5-hour window at {five.utilization:.0%} (ceiling {policy.five_hour_ceiling:.0%})",
                        five.resets_at, details)
    if seven and seven.utilization >= policy.weekly_target:
        return Decision(0, f"7-day window at {seven.utilization:.0%} (target {policy.weekly_target:.0%})",
                        seven.resets_at, details)

    # 3/4. pacing, with the use-it-or-lose-it override near the weekly reset
    frac, reasons, resume = 1.0, [], []
    if seven:
        hours_left = (seven.resets_at - now) / 3600
        if hours_left <= policy.burst_hours:
            reasons.append(f"weekly reset in {hours_left:.1f} h with {policy.weekly_target - seven.utilization:.0%} unused: full speed")
        else:
            f, r = _pace(seven, SEVEN_DAYS, policy.weekly_target, policy.weekly_band, now)
            details["weekly_pace"] = round(f, 3)
            if f < 1:
                reasons.append(f"ahead of weekly pace ({seven.utilization:.0%} used)")
            frac = min(frac, f)
            if r:
                resume.append(r)
    if five:
        f, r = _pace(five, FIVE_HOURS, policy.five_hour_ceiling, policy.five_hour_band, now)
        details["five_hour_pace"] = round(f, 3)
        if f < 1:
            reasons.append(f"ahead of 5-hour pace ({five.utilization:.0%} used)")
        frac = min(frac, f)
        if r:
            resume.append(r)

    workers = math.ceil(n * frac - 1e-9) if frac > 0 else 0
    if workers == 0:
        return Decision(0, "; ".join(reasons) or "paced", max(resume) if resume else now + 300, details)
    return Decision(workers, "; ".join(reasons) or "within budget: full speed", None, details)
