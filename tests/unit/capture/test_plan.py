from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from sharpline.capture.plan import (
    Budget,
    CaptureAction,
    ClosingExceedsBudget,
    SlateGame,
    plan_captures,
)

ET = ZoneInfo("America/New_York")


def et(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, 5, hour, minute, tzinfo=ET).astimezone(UTC)


def g(event_id: str, tip: datetime) -> SlateGame:
    return SlateGame(provider_event_id=event_id, commence_time=tip)


T730 = et(19, 30)
T1000AM = et(10, 0)


def of_kind(actions: tuple[CaptureAction, ...], kind: str) -> list[CaptureAction]:
    return [a for a in actions if a.kind == kind]


def test_plan_two_games_same_tip_one_closing_batch() -> None:
    plan = plan_captures([g("a", T730), g("b", T730)], now=T1000AM, budget=Budget(180))
    closing = of_kind(plan.actions, "closing")
    assert len(closing) == 1
    assert closing[0].at == T730 - timedelta(minutes=3)
    assert closing[0].est_cost == 3 + 6 * 2
    assert set(closing[0].event_ids) == {"a", "b"}


def test_plan_drops_actions_in_the_past() -> None:
    now = et(19, 29)
    plan = plan_captures([g("a", T730)], now=now, budget=Budget(180))
    assert all(a.at >= now for a in plan.actions)
    assert not any("a" in a.event_ids for a in plan.actions if a.kind in ("closing", "props"))
    assert of_kind(plan.actions, "closing") == []


def test_plan_nothing_at_or_after_tip_and_trimmed() -> None:
    early, late = et(13), et(19, 30)
    plan = plan_captures([g("early", early), g("late", late)], now=et(10), budget=Budget(400))
    tips = {"early": early, "late": late}
    for a in plan.actions:
        for e in a.event_ids:
            assert a.at < tips[e]
    # an hourly game_lines refresh after the early tip covers only the late game
    after = [a for a in of_kind(plan.actions, "game_lines") if a.at > early]
    assert after and all(a.event_ids == ("late",) for a in after)


def test_plan_expected_shape_single_game() -> None:
    plan = plan_captures([g("a", T730)], now=et(17, 0), budget=Budget(180))
    kinds = ("schedule", "injuries", "game_lines", "props", "closing")
    by_kind = {k: of_kind(plan.actions, k) for k in kinds}
    assert [a.at for a in by_kind["schedule"]] == [et(17, 0)]
    assert by_kind["schedule"][0].est_cost == 0
    inj = [a.at for a in by_kind["injuries"]]
    assert inj[0] == et(17, 0) and et(17, 30) in inj and et(19, 15) in inj
    gl = [a.at for a in by_kind["game_lines"]]
    assert gl == [et(17, 0), et(18, 0), et(19, 0)]  # T-90 (18:00) coincides with hourly
    assert all(a.est_cost == 3 for a in by_kind["game_lines"])
    props = [(a.at, a.est_cost) for a in by_kind["props"]]
    assert props == [(et(17, 0), 6), (et(18, 0), 6)]
    assert plan.est_cost == sum(a.est_cost for a in plan.actions)


def test_plan_first_tip_t90_game_lines() -> None:
    plan = plan_captures([g("a", et(19, 30))], now=et(17, 20), budget=Budget(180))
    assert et(18, 0) in [a.at for a in of_kind(plan.actions, "game_lines")]
    plan2 = plan_captures([g("a", et(19, 0))], now=et(17, 20), budget=Budget(180))
    t90 = [a for a in of_kind(plan2.actions, "game_lines") if a.reason == "t-90"]
    assert [a.at for a in t90] == [et(17, 30)]


def test_actions_sorted_deterministically() -> None:
    games = [g("b", T730), g("a", T730), g("c", et(21, 0))]
    p1 = plan_captures(games, now=T1000AM, budget=Budget(180))
    p2 = plan_captures(list(reversed(games)), now=T1000AM, budget=Budget(180))
    assert p1 == p2
    assert list(p1.actions) == sorted(p1.actions)


def test_budget_drop_order() -> None:
    games = [g("a", et(19, 0)), g("b", et(21, 0))]
    full = plan_captures(games, now=T1000AM, budget=Budget(1000))
    assert full.dropped == ()

    def dropped_reasons(cap: int) -> list[tuple[str, str]]:
        plan = plan_captures(games, now=T1000AM, budget=Budget(cap))
        assert plan.est_cost <= cap
        assert all(why == "budget" for _, why in plan.dropped)
        assert len(of_kind(plan.actions, "closing")) == 2
        return [(a.kind, a.reason) for a, _ in plan.dropped]

    # shave a little: only hourly game_lines go, latest first
    d1 = plan_captures(games, now=T1000AM, budget=Budget(full.est_cost - 3))
    assert [(a.kind, a.reason) for a, _ in d1.dropped] == [("game_lines", "hourly")]
    hourly = [a for a in full.actions if a.kind == "game_lines" and a.reason == "hourly"]
    assert d1.dropped[0][0] == max(hourly, key=lambda a: a.at)

    # tighter: all hourly gone before any t-90 props
    seen = dropped_reasons(full.est_cost - 3 * len(hourly) - 1)
    assert seen[: len(hourly)] == [("game_lines", "hourly")] * len(hourly)
    assert seen[len(hourly)] == ("props", "t-90")

    # very tight: start props go last among the three classes
    closing_cost = sum(a.est_cost for a in full.actions if a.kind == "closing")
    seen = dropped_reasons(closing_cost + 6)
    kinds = [s for s in seen if s[0] in ("game_lines", "props")]
    first_start = kinds.index(("props", "session_start"))
    assert ("game_lines", "hourly") not in kinds[first_start:]
    assert ("props", "t-90") not in kinds[first_start:]


def test_budget_never_drops_closing_and_fits_when_possible() -> None:
    games = [g("a", T730)]
    closing_only = 3 + 6
    plan = plan_captures(games, now=T1000AM, budget=Budget(closing_only))
    assert plan.est_cost == closing_only
    assert len(of_kind(plan.actions, "closing")) == 1


def test_closing_alone_over_cap_raises() -> None:
    with pytest.raises(ClosingExceedsBudget):
        plan_captures([g("a", T730), g("b", et(21, 0))], now=T1000AM, budget=Budget(10))


def test_empty_slate() -> None:
    plan = plan_captures([], now=T1000AM, budget=Budget(180))
    assert of_kind(plan.actions, "closing") == []
    assert plan.est_cost == 0


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValueError):
        plan_captures([g("a", T730)], now=datetime(2026, 11, 5, 10, 0), budget=Budget(180))
