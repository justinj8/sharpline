"""Pure, deterministic capture planner (spec §4.1-4.2)."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import total_ordering
from typing import Literal

from sharpline.domain.time import ensure_utc
from sharpline.sources.odds_api.markets import GAME_MARKETS, NBA_PROP_MARKETS

CaptureKind = Literal["schedule", "injuries", "game_lines", "props", "closing"]

_KIND_ORDER: dict[str, int] = {
    "schedule": 0,
    "injuries": 1,
    "game_lines": 2,
    "props": 3,
    "closing": 4,
}

DEFAULT_PROP_MARKETS = len(NBA_PROP_MARKETS)
GAME_LINES_COST = len(GAME_MARKETS)  # 1 credit per market x 1 region
_INJURY_EVERY = timedelta(minutes=30)
_INJURY_LEAD = timedelta(minutes=15)
_HOURLY = timedelta(minutes=60)
_PROPS_LEAD = timedelta(minutes=90)
_CLOSING_LEAD = timedelta(minutes=3)


class ClosingExceedsBudget(Exception):
    """The closing snapshots alone cost more than the session cap."""


@dataclass(frozen=True)
class SlateGame:
    provider_event_id: str  # Odds API event id (R2)
    commence_time: datetime


@dataclass(frozen=True)
class Budget:
    session_cap: int
    prop_markets: int = DEFAULT_PROP_MARKETS


@total_ordering
@dataclass(frozen=True)
class CaptureAction:
    at: datetime
    kind: CaptureKind
    event_ids: tuple[str, ...]
    est_cost: int
    reason: str

    def _key(self) -> tuple[datetime, int, tuple[str, ...], str]:
        return (self.at, _KIND_ORDER[self.kind], self.event_ids, self.reason)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, CaptureAction):
            return NotImplemented
        return self._key() < other._key()


@dataclass(frozen=True)
class CapturePlan:
    actions: tuple[CaptureAction, ...]
    dropped: tuple[tuple[CaptureAction, str], ...]
    est_cost: int


def plan_captures(games: Sequence[SlateGame], now: datetime, budget: Budget) -> CapturePlan:
    now = ensure_utc(now)
    slate = sorted(
        (SlateGame(g.provider_event_id, ensure_utc(g.commence_time)) for g in games),
        key=lambda g: (g.commence_time, g.provider_event_id),
    )
    pm = budget.prop_markets

    def covering(at: datetime) -> tuple[str, ...]:
        """Ids of games still ahead of their closing slot at `at` (so also before tip)."""
        return tuple(g.provider_event_id for g in slate if g.commence_time - _CLOSING_LEAD > at)

    actions: list[CaptureAction] = [CaptureAction(now, "schedule", (), 0, "session_start")]

    def add(at: datetime, kind: CaptureKind, ids: tuple[str, ...], cost: int, reason: str) -> None:
        if ids and at >= now:
            actions.append(CaptureAction(at, kind, ids, cost, reason))

    if slate:
        last_closing = slate[-1].commence_time - _CLOSING_LEAD
        first_tip = slate[0].commence_time

        # injuries: start, every 30 min, T-15 per tip
        add(now, "injuries", covering(now), 0, "session_start")
        t = now + _INJURY_EVERY
        while t < last_closing:
            add(t, "injuries", covering(t), 0, "every_30m")
            t += _INJURY_EVERY
        for tip in sorted({g.commence_time for g in slate}):
            at = tip - _INJURY_LEAD
            if at > now:
                add(at, "injuries", covering(at), 0, "t-15")

        # game lines: start, hourly, T-90 of the first tip
        add(now, "game_lines", covering(now), GAME_LINES_COST, "session_start")
        hourly_times: set[datetime] = set()
        t = now + _HOURLY
        while t < last_closing:
            add(t, "game_lines", covering(t), GAME_LINES_COST, "hourly")
            hourly_times.add(t)
            t += _HOURLY
        t90 = first_tip - _PROPS_LEAD
        if t90 > now and t90 not in hourly_times:
            add(t90, "game_lines", covering(t90), GAME_LINES_COST, "t-90")

        # props: start (all games) and T-90 per game
        start_ids = covering(now)
        add(now, "props", start_ids, pm * len(start_ids), "session_start")
        for g in slate:
            at = g.commence_time - _PROPS_LEAD
            if at > now:
                add(at, "props", (g.provider_event_id,), pm, "t-90")

        # closing: T-3 per tip, identical tips batched
        by_tip: dict[datetime, list[str]] = {}
        for g in slate:
            by_tip.setdefault(g.commence_time, []).append(g.provider_event_id)
        for tip, ids in by_tip.items():
            add(
                tip - _CLOSING_LEAD,
                "closing",
                tuple(sorted(ids)),
                GAME_LINES_COST + pm * len(ids),
                "closing",
            )

    return fit_budget(sorted(actions), budget.session_cap)


def fit_budget(actions: list[CaptureAction], cap: int) -> CapturePlan:
    """Drop actions in R10 order until the total fits `cap`; also used for re-plans."""
    closing_cost = sum(a.est_cost for a in actions if a.kind == "closing")
    if closing_cost > cap:
        raise ClosingExceedsBudget(f"closing snapshots cost {closing_cost} > session_cap {cap}")

    kept = list(actions)
    dropped: list[tuple[CaptureAction, str]] = []

    def total() -> int:
        return sum(a.est_cost for a in kept)

    # Spec order, latest first within a class. The trailing game_lines classes are
    # a safety net so the plan always fits once closing fits: spec lists only three
    # droppable classes, but start/T-90 game_lines would otherwise be undroppable.
    for kind, reason in (
        ("game_lines", "hourly"),
        ("props", "t-90"),
        ("props", "session_start"),
        ("game_lines", "t-90"),
        ("game_lines", "session_start"),
    ):
        candidates = sorted(
            (a for a in kept if a.kind == kind and a.reason == reason), reverse=True
        )
        for a in candidates:
            if total() <= cap:
                break
            kept.remove(a)
            dropped.append((a, "budget"))
        if total() <= cap:
            break

    return CapturePlan(tuple(kept), tuple(dropped), total())
