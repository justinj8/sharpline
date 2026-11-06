"""Capture session runner (spec §4, §7): executes a capture plan and records every action.

A session starts with a schedule refresh, plans from the stored slate, then works through
the actions in time order, sleeping in between. Every schedule refresh (session start, then
hourly) re-reads the slate and re-plans the remaining actions against the remaining budget
(R10). Each action ends `done`, `skipped` or `failed` in `capture_runs.plan`. The session
cap is per slate day (earlier runs' credits count), and a transient database error in the
runner's own bookkeeping is retried; three in a row fail the run.
"""

from __future__ import annotations

import bisect
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from functools import partial
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import exc as sa_exc
from sqlalchemy import func, select
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
from sharpline.db.models import CaptureRun, EntityXref, Event, QuotaLedger
from sharpline.domain.enums import EventStatus, Sport
from sharpline.ingest import jobs
from sharpline.ingest.writer import WriteStats
from sharpline.sources.espn import PROVIDER as ESPN
from sharpline.sources.espn.client import EspnClient
from sharpline.sources.fetched import Fetched, QuotaInfo
from sharpline.sources.http import CircuitBreaker, ProviderUnavailable
from sharpline.sources.odds_api.client import PROVIDER as ODDS
from sharpline.sources.odds_api.client import OddsApiClient, OddsApiError
from sharpline.sources.odds_api.markets import NBA_PROP_MARKETS

log = logging.getLogger(__name__)

EASTERN = ZoneInfo("America/New_York")
REFRESH_EVERY = timedelta(hours=1)
CLOSING_LEAD = timedelta(minutes=3)
_UNCAPPED = 10**9  # plan everything first, then fit what remains into what is left
_ODDS_KINDS = frozenset({"game_lines", "props", "closing"})
# Transient database errors in the runner's own bookkeeping (re-plan, progress): retried at
# the next iteration; this many in a row fail the run (F5).
MAX_DB_FAILURES = 3
DB_RETRY_AFTER = timedelta(minutes=1)  # wait before retrying a session-start plan
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
    # A closing call's games, re-checked against their tips right before it runs (F6), and
    # how to rebuild the job for the games still ahead (None: the call covers one game).
    games: tuple[str, ...] = ()
    rescope: Callable[[tuple[str, ...]], Job] | None = None


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
            Event.status == EventStatus.SCHEDULED,
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


def is_transient_db_error(exc: BaseException) -> bool:
    """A dropped or unusable connection, worth retrying on a fresh one."""
    if isinstance(exc, sa_exc.OperationalError | sa_exc.InterfaceError):
        return True
    return isinstance(exc, sa_exc.DBAPIError) and exc.connection_invalidated


def _gone_reason(status: str) -> str:
    """Why a game left the slate (or a closing skips it), from its stored status."""
    if status in (EventStatus.SCHEDULED, EventStatus.POSTPONED):
        return "tip_moved"  # postponed, or moved off the slate day
    if status == EventStatus.CANCELED:
        return "canceled"
    return "started"


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
        self._log: list[dict[str, Any]] = []
        self._pending: list[CaptureAction] = []
        self._dropped: dict[CaptureAction, dict[str, Any]] = {}
        # Props and closings of games that left the slate (postponed, canceled, moved to
        # another day): kept across re-plans, unlike budget drops, which each re-plan redoes.
        self._off_slate: dict[CaptureAction, dict[str, Any]] = {}
        self._replans = 0
        self._keys: dict[str, int] = {}  # plan event id -> events.id
        self._odds_keys: set[str] = set()  # plan event ids that are Odds API ids
        self._executed: set[CaptureAction] = set()
        self._db_failures: dict[str, int] = {}  # per bookkeeping step, consecutive
        with self._sessions.begin() as session:
            self._seed_guard(session)
            self._used0 = self._guard.credits_used
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
            self._executed.add(start)
            replan_due = not self._db_step("re-plan", partial(self._replan, start.at))
            self._db_step("persist", partial(self._persist, "running"))
            while self._pending or replan_due:
                replan_at: datetime | None = None
                if self._pending:
                    action = self._pending[0]
                    if action.at > self._clock():
                        self._sleep_until(action.at)
                    self._execute(action)
                    self._executed.add(action)
                    self._pending.remove(action)
                    if action.kind == "schedule":
                        replan_at = action.at
                    elif replan_due:  # the last re-plan failed: retry, from now on
                        replan_at = self._clock()
                else:  # no plan at all yet (the session-start re-plan failed): wait, retry
                    self._sleep_until(self._clock() + DB_RETRY_AFTER)
                    replan_at = self._clock()
                if replan_at is not None:
                    replan_due = not self._db_step("re-plan", partial(self._replan, replan_at))
                self._db_step("persist", partial(self._persist, "running"))
            status = "completed"
        except KeyboardInterrupt:
            status = "interrupted"
            raise
        finally:
            self._persist(status, finished=True)  # its own (fresh) session, like every persist
        return self._run_id

    def _db_step(self, what: str, step: Callable[[], None]) -> bool:
        """Run a bookkeeping step; a transient DB error is logged and reported as False (the
        caller keeps its previous plan and retries next iteration) until the same step has
        failed MAX_DB_FAILURES times in a row, which re-raises and fails the run. Counted per
        step: a persist that works between two failed re-plans does not reset the re-plan's
        count."""
        try:
            step()
        except sa_exc.SQLAlchemyError as exc:
            if not is_transient_db_error(exc):
                raise
            failures = self._db_failures[what] = self._db_failures.get(what, 0) + 1
            log.warning(
                "capture run %s: %s failed (%s), %d in a row",
                self._run_id,
                what,
                type(exc).__name__,
                failures,
            )
            if failures >= MAX_DB_FAILURES:
                raise
            return False
        self._db_failures[what] = 0
        return True

    def _seed_guard(self, session: Session) -> None:
        """The cap is per slate day (F5): count what earlier runs of this sport and slate
        date spent, and start from the provider's last reported remaining credits."""
        used = session.scalar(
            select(func.coalesce(func.sum(CaptureRun.credits_used), 0)).where(
                CaptureRun.sport == self._sport.value, CaptureRun.slate_date == self._slate_date
            )
        )
        remaining = session.scalar(
            select(QuotaLedger.remaining)
            .where(QuotaLedger.provider == ODDS, QuotaLedger.remaining.is_not(None))
            .order_by(QuotaLedger.observed_at.desc(), QuotaLedger.id.desc())
            .limit(1)
        )
        self._guard.seed(used=int(used or 0), remaining=remaining)

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
        cap - credits used on this slate day, and no more than remaining - floor once known.
        Actions already executed are never planned again (a late retry after a DB error).

        Planning from the session start keeps the 30-min/hourly grid and the planner's
        reasons (so its drop order) stable; actions before `t` are history and discarded.
        """
        slate = self._load_slate()
        self._drop_off_slate(t, {g.provider_event_id for g in slate})
        raw = plan_captures(slate, self._start, Budget(_UNCAPPED, len(self._markets)))
        remaining = [
            a for a in raw.actions if a.kind != "schedule" and a.at >= t and a not in self._executed
        ]
        last_closing = max((a.at for a in raw.actions if a.kind == "closing"), default=None)
        refresh = self._start + REFRESH_EVERY
        while last_closing is not None and refresh < last_closing:
            if refresh > t:
                remaining.append(CaptureAction(refresh, "schedule", (), 0, "hourly"))
            refresh += REFRESH_EVERY
        cap_left = self._guard.session_cap - self._guard.credits_used  # per slate day (F5)
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

    def _drop_off_slate(self, t: datetime, on_slate: set[str]) -> None:
        """Record the pending props and closings of games no longer on the slate (F3): a
        refresh that reports a game postponed or canceled takes it out of `load_slate`."""
        self._off_slate = {
            a: e for a, e in self._off_slate.items() if not on_slate.intersection(a.event_ids)
        }
        pending = [a for a in self._pending if a.kind in ("props", "closing") and a.at >= t]
        gone = sorted({k for a in pending for k in a.event_ids if k not in on_slate})
        if not gone:
            return
        with self._sessions() as session:
            why = {k: _gone_reason(session.get_one(Event, self._keys[k]).status) for k in gone}
        for a in pending:
            ids = tuple(k for k in a.event_ids if k in why)
            if not ids:
                continue
            cost = len(self._markets) * len(ids)
            if a.kind == "closing" and ids == a.event_ids:
                cost += GAME_LINES_COST  # the whole batch is gone, its game lines too
            off = replace(a, event_ids=ids, est_cost=cost)
            self._off_slate[off] = _entry(off, "skipped", "; ".join(f"{k}: {why[k]}" for k in ids))

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
        tips: dict[str, datetime] = {}
        with self._sessions() as session:
            for key in a.event_ids:
                event = session.get_one(Event, self._keys[key])
                if event.status != EventStatus.SCHEDULED:
                    notes.append(f"{key}: {_gone_reason(event.status)}")
                elif event.commence_time <= now:
                    notes.append(f"{key}: started")
                elif event.commence_time - CLOSING_LEAD > a.at:
                    notes.append(f"{key}: tip_moved")
                    moved.setdefault(event.commence_time - CLOSING_LEAD, []).append(key)
                else:
                    keep.append(key)
                    tips[key] = event.commence_time
        planned = {(p.at, k) for p in self._pending if p.kind == "closing" for k in p.event_ids}
        for at, keys in moved.items():
            new = tuple(sorted(k for k in keys if (at, k) not in planned))
            if new:
                cost = GAME_LINES_COST + len(self._markets) * len(new)
                bisect.insort(self._pending, CaptureAction(at, "closing", new, cost, "tip_moved"))
        return self._odds_action(a, tuple(keep), notes, tips)

    def _odds_action(
        self,
        a: CaptureAction,
        ids: tuple[str, ...],
        notes: list[str],
        tips: dict[str, datetime] | None = None,
    ) -> tuple[str, str | None]:
        """Game lines (scoped to `ids`) and/or props per event, behind breaker and guard.

        With `tips` (a closing), each call first drops the games whose tip has passed.
        """
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
        closing = tips is not None

        def game_lines(scope: tuple[str, ...]) -> Job:
            return partial(jobs.ingest_game_lines, event_ids=scope, **common)

        if a.kind != "props":
            calls.append(
                _Call(
                    ODDS,
                    "game_lines",
                    GAME_LINES_COST,
                    game_lines(priced),
                    games=priced if closing else (),
                    rescope=game_lines,
                )
            )
        if a.kind != "game_lines":
            for event_id in priced:
                job = partial(jobs.ingest_props, event_id=event_id, markets=self._markets, **common)
                games = (event_id,) if closing else ()
                calls.append(_Call(ODDS, event_id, len(self._markets), job, games=games))
        # Never spend what a pending closing needs (I1): closings run in time order.
        reserve = 0
        if a.kind != "closing":
            reserve = sum(p.est_cost for p in self._pending if p.kind == "closing")
        decision = self._guard.allows(sum(c.est for c in calls), reserve)
        if not decision.allowed:
            raise _Skip(decision.reason or "quota")
        return self._run_calls(calls, notes, tips or {})

    def _run_calls(
        self, calls: list[_Call], notes: list[str], tips: dict[str, datetime] | None = None
    ) -> tuple[str, str | None]:
        """Every call runs; one failing call fails the action but not its siblings.

        A closing never prices a game at or after its tip (F6): before each call, games
        whose tip has passed are noted `started` and left out of this and later calls.
        """
        errors: list[str] = []
        done = 0
        started: set[str] = set()
        for planned in calls:
            call: _Call | None = planned
            if planned.games and tips:
                call = self._before_tip(planned, tips, started, notes)
            if call is None:
                continue
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

    def _before_tip(
        self, call: _Call, tips: dict[str, datetime], started: set[str], notes: list[str]
    ) -> _Call | None:
        """`call` narrowed to its games still before tip; None when none are."""
        now = self._clock()
        live = tuple(k for k in call.games if now < tips[k])
        for key in call.games:
            if key not in live and key not in started:
                started.add(key)
                notes.append(f"{key}: started")
        if not live:
            return None
        if live == call.games:
            return call
        assert call.rescope is not None  # only a multi-game call can lose some of its games
        return replace(call, games=live, job=call.rescope(live))

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
            "dropped": sorted(
                [*self._dropped.values(), *self._off_slate.values()],
                key=lambda e: (e["at"], e["kind"]),
            ),
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
