"""Capture session runner (spec §4, §7): executes a capture plan and records every action.

A session starts with a schedule refresh, plans from the stored slate, then works through
the actions in time order, sleeping in between. Every schedule refresh (session start, then
hourly) re-reads the slate and re-plans the remaining actions against the remaining budget
(R10). Each action ends `done`, `skipped` or `failed` in `capture_runs.plan`.
"""

from __future__ import annotations

import bisect
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import partial
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from sharpline.capture.plan import (
    GAME_LINES_COST,
    Budget,
    CaptureAction,
    ClosingExceedsBudget,
    SlateGame,
    fit_budget,
    plan_captures,
)
from sharpline.capture.quota import QuotaGuard
from sharpline.db.models import CaptureRun, EntityXref, Event
from sharpline.domain.enums import Sport
from sharpline.ingest import jobs
from sharpline.ingest.writer import WriteStats
from sharpline.sources.espn import PROVIDER as ESPN
from sharpline.sources.espn.client import EspnClient
from sharpline.sources.fetched import Fetched, QuotaInfo
from sharpline.sources.http import CircuitBreaker, ProviderUnavailable
from sharpline.sources.odds_api.client import PROVIDER as ODDS
from sharpline.sources.odds_api.client import OddsApiClient, OddsApiError
from sharpline.sources.odds_api.markets import NBA_PROP_MARKETS

EASTERN = ZoneInfo("America/New_York")
REFRESH_EVERY = timedelta(hours=1)
CLOSING_LEAD = timedelta(minutes=3)
_UNCAPPED = 10**9  # plan everything first, then fit what remains into what is left
_ODDS_KINDS = frozenset({"game_lines", "props", "closing"})
# Only these mean the provider is unhealthy (spec 7); parse/resolve/write errors do not.
PROVIDER_ERRORS: tuple[type[Exception], ...] = (OddsApiError, httpx.HTTPError, ProviderUnavailable)

# A provider that answers with one of these is unhealthy (or refusing us); any other 4xx is
# about the request, e.g. a 404 for a delisted event or a 422, and fails only that call.
BREAKER_STATUSES = frozenset({401, 429})

Job = Callable[[Session], WriteStats]


def trips_breaker(exc: Exception) -> bool:
    """Whether a provider error counts toward the circuit breaker (spec 7): 401, 429, 5xx,
    transport errors and an open circuit do; a 404 or 422 does not."""
    status: int | None = None
    if isinstance(exc, OddsApiError):
        status = exc.status
    elif isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
    return status is None or status in BREAKER_STATUSES or status >= 500


class _Skip(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class _Call:
    provider: str
    label: str
    est: int
    job: Job


@dataclass(frozen=True)
class SlateEntry:
    """A stored game of the slate: its plan key, our event id, and whose id the key is."""

    game: SlateGame
    event_id: int
    provider: str


def load_slate(
    session: Session,
    sport: Sport,
    slate_date: date,
    *,
    providers: Sequence[str] = (ESPN, ODDS),
) -> list[SlateEntry]:
    """Scheduled games of the slate's ET day, keyed by Odds API id when known (R2).

    Later `providers` win, so with both an event is keyed by its Odds API id when it has
    one and by its ESPN id otherwise; events with no id from `providers` are left out.
    """
    start = datetime.combine(slate_date, time.min, tzinfo=EASTERN)
    end = datetime.combine(slate_date + timedelta(days=1), time.min, tzinfo=EASTERN)
    events = session.scalars(
        select(Event).where(
            Event.sport == sport.value,
            Event.commence_time >= start,
            Event.commence_time < end,
            Event.status == "scheduled",
        )
    ).all()
    keys: dict[int, tuple[str, str]] = {}
    for provider in providers:
        rows = session.execute(
            select(EntityXref.entity_id, EntityXref.provider_id).where(
                EntityXref.provider == provider,
                EntityXref.entity_kind == "event",
                EntityXref.entity_id.in_([e.id for e in events]),
            )
        ).all()
        keys.update({pk: (provider, provider_id) for pk, provider_id in rows})
    return [
        SlateEntry(SlateGame(keys[e.id][1], e.commence_time), e.id, keys[e.id][0])
        for e in events
        if e.id in keys
    ]


def _entry(a: CaptureAction, status: str, detail: str | None) -> dict[str, Any]:
    return {
        "at": a.at.isoformat(),
        "kind": a.kind,
        "event_ids": list(a.event_ids),
        "est_cost": a.est_cost,
        "reason": a.reason,
        "status": status,
        "detail": detail,
    }


def _error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


class _Metered:
    """The Odds API client, recording each response's quota headers for the guard (I2)."""

    def __init__(self, odds: OddsApiClient) -> None:
        self._odds = odds
        self.billed: list[QuotaInfo | None] = []

    def _record(self, fetch: Callable[[], Fetched]) -> Fetched:
        try:
            fetched = fetch()
        except PROVIDER_ERRORS:
            raise  # the Odds API does not bill failed requests
        except Exception:
            self.billed.append(None)  # a 2xx we could not decode: assume it was billed
            raise
        self.billed.append(fetched.quota)
        return fetched

    def events(self) -> Fetched:
        return self._record(self._odds.events)

    def game_odds(self, event_ids: Sequence[str] | None = None) -> Fetched:
        return self._record(lambda: self._odds.game_odds(event_ids))

    def event_odds(self, event_id: str, markets: Sequence[str]) -> Fetched:
        return self._record(lambda: self._odds.event_odds(event_id, markets))


class CaptureRunner:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        espn: EspnClient,
        odds: OddsApiClient | None,
        guard: QuotaGuard,
        breaker: CircuitBreaker,
        clock: Callable[[], datetime],
        sleep_until: Callable[[datetime], None],
        prop_markets: Sequence[str] = NBA_PROP_MARKETS,
    ) -> None:
        self._sessions = session_factory
        self._espn = espn
        self._odds = None if odds is None else _Metered(odds)
        self._guard = guard
        self._breaker = breaker
        self._clock = clock
        self._sleep_until = sleep_until
        self._markets = tuple(prop_markets)
        # The current session's capture_runs id, set once its row has committed: callers
        # that catch an interrupt export exactly this run (None: nothing was recorded).
        self.run_id: int | None = None

    def run(
        self, sport: Sport, slate_date: date, *, mode: Literal["manual", "live"] = "manual"
    ) -> int:
        """Run one capture session to the end of the slate; returns the capture_run id."""
        self.run_id = None
        self._sport, self._slate_date = sport, slate_date
        self._start = self._clock()
        self._used0 = self._guard.credits_used
        self._log: list[dict[str, Any]] = []
        self._pending: list[CaptureAction] = []
        self._dropped: dict[CaptureAction, dict[str, Any]] = {}
        self._replans = 0
        self._keys: dict[str, int] = {}  # plan event id -> events.id
        self._odds_keys: set[str] = set()  # plan event ids that are Odds API ids
        with self._sessions.begin() as session:
            run = CaptureRun(
                sport=sport.value,
                slate_date=slate_date,
                mode=mode,
                started_at=self._start,
                status="running",
                credits_cap=self._guard.session_cap,
                credits_used=0,
                plan=self._plan_json(),
            )
            session.add(run)
            session.flush()
            self._run_id = run.id
        self.run_id = self._run_id

        status = "failed"
        try:
            start = CaptureAction(self._start, "schedule", (), 0, "session_start")
            self._execute(start)
            self._replan(start.at)
            self._persist("running")
            while self._pending:
                action = self._pending[0]
                if action.at > self._clock():
                    self._sleep_until(action.at)
                self._execute(action)
                self._pending.remove(action)
                if action.kind == "schedule":
                    self._replan(action.at)
                self._persist("running")
            status = "completed"
        except KeyboardInterrupt:
            status = "interrupted"
            raise
        finally:
            self._persist(status, finished=True)
        return self._run_id

    # -- planning -------------------------------------------------------------------------

    def _load_slate(self) -> list[SlateGame]:
        providers = (ESPN, ODDS) if self._odds is not None else (ESPN,)
        with self._sessions() as session:
            entries = load_slate(session, self._sport, self._slate_date, providers=providers)
        for entry in entries:
            key = entry.game.provider_event_id
            self._keys[key] = entry.event_id
            if entry.provider == ODDS:
                self._odds_keys.add(key)
        return [entry.game for entry in entries]

    def _replan(self, t: datetime) -> None:
        """Re-plan everything after `t` on the stored slate, fit to what is left (R10):
        cap - credits used, and no more than remaining - floor once the provider said so.

        Planning from the session start keeps the 30-min/hourly grid and the planner's
        reasons (so its drop order) stable; actions before `t` are history and discarded.
        """
        raw = plan_captures(self._load_slate(), self._start, Budget(_UNCAPPED, len(self._markets)))
        remaining = [a for a in raw.actions if a.kind != "schedule" and a.at >= t]
        last_closing = max((a.at for a in raw.actions if a.kind == "closing"), default=None)
        refresh = self._start + REFRESH_EVERY
        while last_closing is not None and refresh < last_closing:
            if refresh > t:
                remaining.append(CaptureAction(refresh, "schedule", (), 0, "hourly"))
            refresh += REFRESH_EVERY
        cap_left = self._guard.session_cap - self._used()
        budget = cap_left  # the guard's floor binds re-plans too (I1)
        if self._guard.remaining is not None:
            budget = min(budget, self._guard.remaining - self._guard.floor)
        try:
            fitted = fit_budget(sorted(remaining), budget)
            kept, dropped = list(fitted.actions), list(fitted.dropped)
        except ClosingExceedsBudget:
            closing = sum(a.est_cost for a in remaining if a.kind == "closing")
            if t == self._start and closing > cap_left:
                raise  # spec 4.2: a session whose closings exceed the cap is rejected
            # Costs ran over the estimates, or the account is near its floor: keep closing
            # and let the guard decide at run time.
            kept = [a for a in remaining if a.kind == "closing" or a.est_cost == 0]
            dropped = [(a, "budget") for a in remaining if a not in kept]
        self._pending = sorted(kept)
        self._dropped = {a: e for a, e in self._dropped.items() if a.at < t}
        self._dropped.update({a: _entry(a, "skipped", why) for a, why in dropped})
        self._replans += 1

    # -- execution ------------------------------------------------------------------------

    def _execute(self, a: CaptureAction) -> None:
        try:
            status, detail = self._dispatch(a)
        except _Skip as skip:
            status, detail = "skipped", skip.reason
        except Exception as exc:
            status, detail = "failed", _error(exc)
        self._log.append(_entry(a, status, detail))

    def _dispatch(self, a: CaptureAction) -> tuple[str, str | None]:
        if a.kind in _ODDS_KINDS and self._odds is None:
            raise _Skip("no_api_key")
        rid = self._run_id
        if a.kind == "injuries":
            job = partial(jobs.ingest_injuries, espn=self._espn, capture_run_id=rid)
            return self._run_calls([_Call(ESPN, "espn", 0, job)], [])
        if a.kind == "schedule":  # R2: the free Odds API event list links its event ids
            day = self._slate_date
            job = partial(jobs.ingest_schedule, espn=self._espn, slate_date=day, capture_run_id=rid)
            calls = [_Call(ESPN, "espn", 0, job)]
            if self._odds is not None:
                job = partial(jobs.ingest_odds_events, odds=self._odds, capture_run_id=rid)
                calls.append(_Call(ODDS, "odds_api", 0, job))
            return self._run_calls(calls, [])
        if a.kind == "closing":
            return self._closing(a)
        return self._odds_action(a, a.event_ids, [])

    def _closing(self, a: CaptureAction) -> tuple[str, str | None]:
        """Re-read each game first (R9): price only games still ahead of this slot."""
        now, keep, notes = self._clock(), [], []
        moved: dict[datetime, list[str]] = {}
        with self._sessions() as session:
            for key in a.event_ids:
                event = session.get_one(Event, self._keys[key])
                if event.status == "postponed":
                    notes.append(f"{key}: tip_moved")
                elif event.status != "scheduled" or event.commence_time <= now:
                    notes.append(f"{key}: started")
                elif event.commence_time - CLOSING_LEAD > a.at:
                    notes.append(f"{key}: tip_moved")
                    moved.setdefault(event.commence_time - CLOSING_LEAD, []).append(key)
                else:
                    keep.append(key)
        planned = {(p.at, k) for p in self._pending if p.kind == "closing" for k in p.event_ids}
        for at, keys in moved.items():
            new = tuple(sorted(k for k in keys if (at, k) not in planned))
            if new:
                cost = GAME_LINES_COST + len(self._markets) * len(new)
                bisect.insort(self._pending, CaptureAction(at, "closing", new, cost, "tip_moved"))
        return self._odds_action(a, tuple(keep), notes)

    def _odds_action(
        self, a: CaptureAction, ids: tuple[str, ...], notes: list[str]
    ) -> tuple[str, str | None]:
        """Game lines (scoped to `ids`) and/or props per event, behind breaker and guard."""
        assert self._odds is not None
        if not self._breaker.allow(ODDS):
            raise _Skip("circuit_open")
        priced = tuple(i for i in ids if i in self._odds_keys)
        notes = notes + [f"{i}: no_odds_event" for i in ids if i not in self._odds_keys]
        if not priced:
            raise _Skip("; ".join(notes) or "no_events")
        common: dict[str, Any] = {
            "odds": self._odds,
            "is_closing": a.kind == "closing",
            "capture_run_id": self._run_id,
        }
        calls: list[_Call] = []
        if a.kind != "props":
            job = partial(jobs.ingest_game_lines, event_ids=priced, **common)
            calls.append(_Call(ODDS, "game_lines", GAME_LINES_COST, job))
        if a.kind != "game_lines":
            for event_id in priced:
                job = partial(jobs.ingest_props, event_id=event_id, markets=self._markets, **common)
                calls.append(_Call(ODDS, event_id, len(self._markets), job))
        # Never spend what a pending closing needs (I1): closings run in time order.
        reserve = 0
        if a.kind != "closing":
            reserve = sum(p.est_cost for p in self._pending if p.kind == "closing")
        decision = self._guard.allows(sum(c.est for c in calls), reserve)
        if not decision.allowed:
            raise _Skip(decision.reason or "quota")
        return self._run_calls(calls, notes)

    def _run_calls(self, calls: list[_Call], notes: list[str]) -> tuple[str, str | None]:
        """Every call runs; one failing call fails the action but not its siblings."""
        errors: list[str] = []
        done = 0
        for call in calls:
            try:
                self._call(call)
                done += 1
            except _Skip as skip:
                notes.append(f"{call.label}: {skip.reason}")
            except Exception as exc:
                errors.append(_error(exc))
        detail = "; ".join(errors + notes) or None
        if errors:
            return "failed", detail
        if not done:
            raise _Skip(detail or "skipped")
        return "done", detail

    def _call(self, call: _Call) -> None:
        if not self._breaker.allow(call.provider):
            raise _Skip("circuit_open")
        try:
            with self._sessions() as session:
                call.job(session)
        except PROVIDER_ERRORS as exc:
            if trips_breaker(exc):
                self._breaker.failure(call.provider)
            raise
        except Exception:
            self._breaker.success(call.provider)  # the fetch worked; parse/write did not
            raise
        finally:
            if call.provider == ODDS:
                self._charge(call.est)
        self._breaker.success(call.provider)

    def _charge(self, est: int) -> None:
        """Charge the guard from the call's own quota headers (estimate when missing).

        The ledger rows written by the jobs stay the record; the guard is charged from the
        response itself so a missing `x-requests-last` falls back to the estimate.
        """
        assert self._odds is not None
        for quota in self._odds.billed:
            self._guard.charge(quota, est)
        self._odds.billed.clear()

    # -- persistence ----------------------------------------------------------------------

    def _used(self) -> int:
        return self._guard.credits_used - self._used0

    def _plan_json(self) -> dict[str, Any]:
        return {
            "actions": list(self._log),
            "pending": [_entry(a, "pending", None) for a in self._pending],
            "dropped": sorted(self._dropped.values(), key=lambda e: (e["at"], e["kind"])),
            "replans": self._replans,
        }

    def _persist(self, status: str, *, finished: bool = False) -> None:
        with self._sessions.begin() as session:
            run = session.get_one(CaptureRun, self._run_id)
            run.status = status
            run.credits_used = self._used()
            run.plan = self._plan_json()
            if finished:
                run.finished_at = self._clock()
