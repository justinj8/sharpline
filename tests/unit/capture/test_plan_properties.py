from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from hypothesis import given, settings
from hypothesis import strategies as st

from sharpline.capture.plan import (
    Budget,
    ClosingExceedsBudget,
    SlateGame,
    plan_captures,
)

ET = ZoneInfo("America/New_York")
DAY = datetime(2026, 11, 5, 12, 0, tzinfo=ET)


@st.composite
def scenarios(draw: st.DrawFn) -> tuple[list[SlateGame], datetime, int]:
    n = draw(st.integers(1, 15))
    # tips 12:00-23:00 ET on a 5-minute grid; duplicates allowed (batching)
    offsets = draw(st.lists(st.integers(0, 11 * 12), min_size=n, max_size=n))
    games = [
        SlateGame(f"e{i}", (DAY + timedelta(minutes=5 * o)).astimezone(UTC))
        for i, o in enumerate(offsets)
    ]
    first = min(g.commence_time for g in games)
    lead = draw(st.integers(1, 12 * 60))  # now is strictly before the first tip
    now = first - timedelta(minutes=lead)
    cap = draw(st.integers(40, 400))
    return games, now, cap


@settings(max_examples=200, deadline=None)
@given(scenarios())
def test_plan_invariants(scenario: tuple[list[SlateGame], datetime, int]) -> None:
    games, now, cap = scenario
    tips = {g.provider_event_id: g.commence_time for g in games}
    try:
        plan = plan_captures(games, now=now, budget=Budget(cap))
    except ClosingExceedsBudget:
        return

    assert plan.est_cost == sum(a.est_cost for a in plan.actions)
    assert plan.est_cost <= cap
    assert list(plan.actions) == sorted(plan.actions)
    for a in plan.actions:
        assert a.at >= now
        for e in a.event_ids:
            assert a.at < tips[e]
    for a, why in plan.dropped:
        assert why == "budget" and a.kind != "closing"

    closings = [a for a in plan.actions if a.kind == "closing"]
    for gm in games:
        closes_at = gm.commence_time - timedelta(minutes=3)
        covering = [a for a in closings if gm.provider_event_id in a.event_ids]
        if closes_at >= now:
            assert len(covering) == 1 and covering[0].at == closes_at
        else:
            assert covering == []


@settings(max_examples=200, deadline=None)
@given(scenarios())
def test_plan_is_deterministic(scenario: tuple[list[SlateGame], datetime, int]) -> None:
    games, now, cap = scenario
    try:
        a = plan_captures(games, now=now, budget=Budget(cap))
        b = plan_captures(list(reversed(games)), now=now, budget=Budget(cap))
    except ClosingExceedsBudget:
        return
    assert a == b
