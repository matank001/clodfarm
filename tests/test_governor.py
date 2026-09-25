from clodfarm.governor import FIVE_HOURS, SEVEN_DAYS, Policy, Snapshot, Window, decide

NOW = 1_800_000_000.0
P = Policy(max_workers=4, weekly_target=0.90, five_hour_ceiling=0.90, weekly_band=0.05, five_hour_band=0.30, burst_hours=12)


def snap(u5=0.1, u7=0.1, left5=FIVE_HOURS / 2, left7=SEVEN_DAYS / 2, status="allowed", overage=False):
    return Snapshot(observed_at=NOW, status=status, rate_limit_type="five_hour", resets_at=NOW + left5,
                    five_hour=Window(u5, NOW + left5), seven_day=Window(u7, NOW + left7), using_overage=overage)


def test_no_data_runs_one_agent_to_measure():
    d = decide(None, P, NOW)
    assert d.workers == 1


def test_behind_pace_runs_full_speed():
    d = decide(snap(u5=0.1, u7=0.2), P, NOW)  # half the week gone, 20% used
    assert d.workers == 4 and d.pause_until is None


def test_ahead_of_weekly_pace_stops_until_the_line_catches_up():
    d = decide(snap(u7=0.70), P, NOW)  # half the week gone, pace line ~0.50
    assert d.workers == 0
    assert NOW < d.pause_until < NOW + SEVEN_DAYS / 2
    # the resume time is when target * elapsed + band reaches 70%
    elapsed_at_resume = 1 - (NOW + SEVEN_DAYS / 2 - d.pause_until) / SEVEN_DAYS
    assert abs(0.90 * elapsed_at_resume + 0.05 - 0.70) < 1e-6


def test_slightly_ahead_throttles_partially():
    # pace line = 0.9*0.5+0.05 = 0.50; used 0.48 -> headroom 0.02 of a 0.05 band -> 40% -> 2 of 4 workers
    d = decide(snap(u7=0.48), P, NOW)
    assert d.workers == 2


def test_five_hour_ceiling_pauses_until_that_window_resets():
    s = snap(u5=0.92)
    d = decide(s, P, NOW)
    assert d.workers == 0 and d.pause_until == s.five_hour.resets_at


def test_weekly_target_reached_pauses_until_weekly_reset():
    s = snap(u7=0.91, left7=3600 * 30)
    d = decide(s, P, NOW)
    assert d.workers == 0 and d.pause_until == s.seven_day.resets_at


def test_rejected_pauses_until_reset():
    s = snap(status="rejected")
    d = decide(s, P, NOW)
    assert d.workers == 0 and d.pause_until == s.resets_at


def test_overage_stops_unless_allowed():
    assert decide(snap(overage=True), P, NOW).workers == 0
    assert decide(snap(overage=True), Policy(**{**vars(P), "allow_overage": True}), NOW).workers == 4


def test_use_it_or_lose_it_near_weekly_reset():
    # 6 h before the weekly reset with only 40% used: far "ahead" of nothing, spend it
    d = decide(snap(u7=0.40, left7=6 * 3600), P, NOW)
    assert d.workers == 4 and "full speed" in d.reason


def test_windows_that_already_reset_count_as_empty():
    s = snap(u5=0.95, u7=0.95, left5=-10, left7=-10)
    assert decide(s, P, NOW).workers == 4


def test_early_in_a_five_hour_window_bursts_are_allowed():
    # 10 minutes into a fresh 5-hour window, 25% used: under ceiling*elapsed + 0.30 band
    d = decide(snap(u5=0.25, left5=FIVE_HOURS - 600, u7=0.1), P, NOW)
    assert d.workers >= 1


def test_round_trip_through_dict():
    s = snap(u5=0.3, u7=0.4)
    assert Snapshot.from_dict(s.to_dict()) == s


def test_from_real_event_shape():
    info = {"status": "allowed", "resetsAt": 1790293800, "rateLimitType": "five_hour", "overageStatus": "allowed",
            "isUsingOverage": False, "unifiedWindows": {"five_hour": {"utilization": 0.14, "resetsAt": 1790293800},
                                                         "seven_day": {"utilization": 0.16, "resetsAt": 1790348400}}}
    s = Snapshot.from_event(info, NOW)
    assert s.five_hour.utilization == 0.14 and s.seven_day.resets_at == 1790348400 and not s.using_overage


API = Policy(max_workers=3, api_mode=True, daily_budget_usd=20.0)


def test_api_mode_runs_full_speed_without_subscription_data():
    assert decide(None, API, NOW).workers == 3


def test_api_mode_daily_cap_pauses_until_utc_midnight():
    d = decide(None, API, NOW, spent_today=20.5)
    assert d.workers == 0 and d.pause_until % 86400 == 0 and d.pause_until > NOW


def test_defaults_leave_room_for_the_human():
    p = Policy()
    assert p.burst_hours == 0 and not p.allow_overage
