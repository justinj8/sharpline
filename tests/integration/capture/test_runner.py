"""CaptureRunner against a private database, a fake clock and MockTransport providers."""

from __future__ import annotations

import copy
import json
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from sharpline.capture.quota import QuotaGuard
from sharpline.capture.runner import CaptureRunner
from sharpline.db.models import (
    CaptureRun,
    EntityXref,
    Event,
    Market,
    OddsSnapshot,
    QuotaLedger,
)
from sharpline.domain.enums import Sport
from sharpline.ingest import jobs
from sharpline.sources.espn.client import EspnClient
from sharpline.sources.http import CircuitBreaker, HttpClient
from sharpline.sources.odds_api.client import OddsApiClient

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parents[2] / "fixtures"
SLATE = date(2026, 11, 4)  # ET; both tips below are that evening
TIP1 = datetime(2026, 11, 5, 0, 0, tzinfo=UTC)
TIP2 = datetime(2026, 11, 5, 2, 30, tzinfo=UTC)
ODDS_KINDS = ("game_lines", "props", "closing")
ESPN_IDS = {"evt1": "e1", "evt2": "e2", "evt3": "e3"}
# (home, away) ESPN abbreviations
ESPN_TEAMS = {"evt1": ("BOS", "NY"), "evt2": ("LAC", "DEN"), "evt3": ("MIA", "ORL")}
EVT3 = {  # a third game, not in the trimmed fixtures
    "id": "evt3",
    "sport_key": "basketball_nba",
    "sport_title": "NBA",
    "commence_time": "2026-11-05T00:00:00Z",
    "home_team": "Miami Heat",
    "away_team": "Orlando Magic",
}


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def _events() -> list[dict[str, Any]]:
    return [*_load("odds_api/nba_events.json"), EVT3]


def _hm(h: int, m: int) -> datetime:
    """A UTC time on the slate evening: 23:xx is Nov 4, 00:xx-02:xx Nov 5."""
    day = 4 if h >= 12 else 5
    return datetime(2026, 11, day, h, m, tzinfo=UTC)


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self.now = now
        self.sleeps: list[datetime] = []
        self.hooks: dict[datetime, Callable[[], None]] = {}

    def __call__(self) -> datetime:
        return self.now

    def sleep_until(self, at: datetime) -> None:
        self.sleeps.append(at)
        self.now = max(self.now, at)
        hook = self.hooks.pop(at, None)
        if hook is not None:
            hook()


class World:
    """Provider state served by the MockTransports; tests mutate it mid-session."""

    def __init__(self, clock: FakeClock, tips: dict[str, datetime]) -> None:
        self.clock = clock
        self.tips = dict(tips)
        self.odds_calls: list[tuple[datetime, httpx.URL]] = []
        self.fail_odds_from: datetime | None = None
        # x-requests-last overrides by ("lines" | "props", call time)
        self.costs: dict[tuple[str, datetime], int] = {}
        self.omit_last = False  # drop the x-requests-last header (proxies, caches)
        self.props_status: int | None = None  # answer every props call with this HTTP status
        # a game-lines call made at this time takes this long to answer (the clock moves on)
        self.slow_lines: dict[datetime, timedelta] = {}
        self.espn_status: dict[
            str, str
        ] = {}  # ESPN status names by Odds API id (default scheduled)
        self.quota_total = 500  # the account's credits, for x-requests-remaining
        self.events_headers = True  # False: the free event list reports no quota headers
        self._used = 0

    @property
    def billed(self) -> int:
        """Credits the provider actually spent (what x-requests-last would have said)."""
        return self._used

    def espn(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/injuries"):
            # Injury rows are the writer's business (tested there); keep the runner fast.
            return httpx.Response(200, json={"injuries": []})
        events = [
            {
                "id": ESPN_IDS[key],
                "date": tip.strftime("%Y-%m-%dT%H:%MZ"),
                "status": {"type": {"name": self.espn_status.get(key, "STATUS_SCHEDULED")}},
                "competitions": [
                    {
                        "competitors": [
                            {"homeAway": "home", "team": {"abbreviation": ESPN_TEAMS[key][0]}},
                            {"homeAway": "away", "team": {"abbreviation": ESPN_TEAMS[key][1]}},
                        ]
                    }
                ],
            }
            for key, tip in self.tips.items()
        ]
        return httpx.Response(200, json={"events": events})

    def _retimed(self, event: dict[str, Any]) -> dict[str, Any]:
        event = copy.deepcopy(event)
        event["commence_time"] = self.tips[event["id"]].strftime("%Y-%m-%dT%H:%M:%SZ")
        return event

    def odds(self, request: httpx.Request) -> httpx.Response:
        self.odds_calls.append((self.clock(), request.url))
        if self.fail_odds_from is not None and self.clock() >= self.fail_odds_from:
            return httpx.Response(401, json={"message": "API key is not valid"})
        path = request.url.path
        body: Any
        if path.endswith("/events"):
            body = [self._retimed(e) for e in _events() if e["id"] in self.tips]
            cost = 0
        elif "/events/" in path and self.props_status is not None:
            return httpx.Response(self.props_status, json={"message": "Event not found"})
        elif "/events/" in path:
            # Props carry no books here: player resolution is covered by the writer tests.
            event_id = path.split("/")[-2]
            meta = next(e for e in _events() if e["id"] == event_id)
            body = self._retimed({**meta, "bookmakers": []})
            cost = self.costs.get(
                ("props", self.clock()), len(request.url.params["markets"].split(","))
            )
        else:
            delay = self.slow_lines.pop(self.clock(), None)
            if delay is not None:
                self.clock.now += delay
            wanted = request.url.params.get("eventIds")
            body = [
                self._retimed(e)
                for e in _load("odds_api/nba_game_odds.json")
                if e["id"] in self.tips and (wanted is None or e["id"] in wanted.split(","))
            ]
            cost = self.costs.get(("lines", self.clock()), 3)
        self._used += cost
        headers = {
            "x-requests-last": str(cost),
            "x-requests-used": str(self._used),
            "x-requests-remaining": str(self.quota_total - self._used),
        }
        if self.omit_last:
            del headers["x-requests-last"]
        if path.endswith("/events") and not self.events_headers:
            headers = {}
        return httpx.Response(200, json=body, headers=headers)


class FlakySessions:
    """A session factory whose session opens from one runner method (`caller`) raise a
    dropped-connection OperationalError `times` times, once the clock reaches `armed_from`."""

    def __init__(
        self,
        real: sessionmaker[Session],
        caller: str,
        times: int,
        *,
        clock: FakeClock,
        armed_from: datetime,
    ) -> None:
        self.real, self.caller, self.times = real, caller, times
        self.clock, self.armed_from = clock, armed_from
        self.raised: list[datetime] = []

    def _maybe_fail(self) -> None:
        caller = sys._getframe(2).f_code.co_name
        if self.times and caller == self.caller and self.clock() >= self.armed_from:
            self.times -= 1
            self.raised.append(self.clock())
            raise OperationalError("SELECT 1", {}, Exception("server closed the connection"))

    def __call__(self, **kw: Any) -> Session:
        self._maybe_fail()
        return self.real(**kw)

    def begin(self) -> Any:
        self._maybe_fail()
        return self.real.begin()


def _http(base: str, handler: Callable[[httpx.Request], httpx.Response]) -> HttpClient:
    return HttpClient(
        base, transport=httpx.MockTransport(handler), sleep=lambda _s: None, max_retries=0
    )


def _runner(
    sessions: sessionmaker[Session] | FlakySessions,
    clock: FakeClock,
    world: World,
    *,
    with_odds: bool = True,
    cap: int = 180,
) -> CaptureRunner:
    espn = EspnClient(
        _http("https://site.api.espn.com/apis/site/v2/sports/basketball/nba", world.espn),
        clock=clock,
    )
    odds = (
        OddsApiClient(
            SecretStr("sk-test"), _http("https://api.the-odds-api.com/v4", world.odds), clock=clock
        )
        if with_odds
        else None
    )
    return CaptureRunner(
        sessions,  # type: ignore[arg-type]
        espn=espn,
        odds=odds,
        guard=QuotaGuard(session_cap=cap, floor=20),
        breaker=CircuitBreaker(clock=clock),
        clock=clock,
        sleep_until=clock.sleep_until,
    )


@pytest.fixture
def sessions(db_session_factory: sessionmaker[Session]) -> sessionmaker[Session]:
    return db_session_factory  # NBA teams are already seeded


def _run(sessions: sessionmaker[Session], run_id: int | None = None) -> CaptureRun:
    with sessions() as session:
        query = select(CaptureRun)
        if run_id is not None:
            query = query.where(CaptureRun.id == run_id)
        return session.scalars(query).one()


def _entries(run: CaptureRun, kind: str | None = None) -> list[dict[str, Any]]:
    return [a for a in run.plan["actions"] if kind is None or a["kind"] == kind]


def _event_pk(sessions: sessionmaker[Session], odds_id: str) -> int:
    with sessions() as session:
        pk = session.scalar(
            select(EntityXref.entity_id).where(
                EntityXref.provider == "odds_api",
                EntityXref.entity_kind == "event",
                EntityXref.provider_id == odds_id,
            )
        )
    assert pk is not None
    return pk


def _closing_snapshots(sessions: sessionmaker[Session], event_pk: int) -> list[OddsSnapshot]:
    with sessions() as session:
        return list(
            session.scalars(
                select(OddsSnapshot)
                .join(Market, Market.id == OddsSnapshot.market_id)
                .where(Market.event_id == event_pk, OddsSnapshot.is_closing)
            )
        )


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def test_run_executes_plan_and_records_statuses(sessions: sessionmaker[Session]) -> None:
    clock = FakeClock(_hm(23, 0))
    world = World(clock, {"evt1": TIP1, "evt2": TIP2})
    run_id = _runner(sessions, clock, world).run(Sport.NBA, SLATE)

    run = _run(sessions, run_id)
    assert (run.status, run.mode, run.sport, run.slate_date) == (
        "completed",
        "manual",
        "nba",
        SLATE,
    )
    assert run.started_at == _hm(23, 0) and run.finished_at == _hm(2, 27)
    entries = _entries(run)
    assert {tuple(sorted(e)) for e in entries} == {
        ("at", "detail", "est_cost", "event_ids", "kind", "reason", "status")
    }
    assert all(e["status"] == "done" for e in entries), entries
    assert [e["at"] for e in entries] == sorted(e["at"] for e in entries)
    assert run.plan["pending"] == [] and run.plan["dropped"] == []

    # session start + hourly refreshes while a closing is still ahead, each followed by a re-plan
    assert [e["at"] for e in _entries(run, "schedule")] == [
        _iso(t) for t in (_hm(23, 0), _hm(0, 0), _hm(1, 0), _hm(2, 0))
    ]
    assert run.plan["replans"] == 4
    closings = [(e["at"], e["event_ids"]) for e in _entries(run, "closing")]
    assert closings == [(_iso(_hm(23, 57)), ["evt1"]), (_iso(_hm(2, 27)), ["evt2"])]

    # 4 game_lines x 3 + start props 2 x 6 + evt2 T-90 props 6 + 2 closings x (3 + 6)
    with sessions() as session:
        ledger_total = session.scalar(
            select(func.sum(QuotaLedger.cost)).where(QuotaLedger.capture_run_id == run_id)
        )
    assert run.credits_used == ledger_total == 48
    assert run.credits_cap == 180
    for odds_id in ("evt1", "evt2"):
        assert _closing_snapshots(sessions, _event_pk(sessions, odds_id))


def test_missing_odds_key_skips_odds_but_captures_espn(sessions: sessionmaker[Session]) -> None:
    clock = FakeClock(_hm(23, 0))
    world = World(clock, {"evt1": TIP1, "evt2": TIP2})
    run_id = _runner(sessions, clock, world, with_odds=False).run(Sport.NBA, SLATE)

    run = _run(sessions, run_id)
    assert run.status == "completed" and run.credits_used == 0
    odds_entries = [e for e in _entries(run) if e["kind"] in ODDS_KINDS]
    assert odds_entries and all(
        (e["status"], e["detail"]) == ("skipped", "no_api_key") for e in odds_entries
    )
    espn_entries = [e for e in _entries(run) if e["kind"] in ("schedule", "injuries")]
    assert len(_entries(run, "injuries")) > 1
    assert all(e["status"] == "done" for e in espn_entries), espn_entries
    # Without an Odds API key the slate is planned on ESPN ids (R2).
    assert [e["event_ids"] for e in _entries(run, "closing")] == [["e1"], ["e2"]]
    assert world.odds_calls == []
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(QuotaLedger)) == 0


def test_odds_401_midsession_marks_remaining_failed_and_finishes(
    sessions: sessionmaker[Session],
) -> None:
    clock = FakeClock(_hm(23, 0))
    world = World(clock, {"evt1": TIP1, "evt2": TIP2})
    world.fail_odds_from = _hm(23, 30)
    run_id = _runner(sessions, clock, world).run(Sport.NBA, SLATE)

    run = _run(sessions, run_id)
    assert run.status == "completed" and run.finished_at is not None
    assert run.credits_used == 3 + 12  # start game lines + start props; failures cost nothing
    late_odds = [
        e for e in _entries(run) if e["kind"] in ODDS_KINDS and e["at"] >= _iso(_hm(23, 30))
    ]
    assert late_odds and all(e["status"] in ("failed", "skipped") for e in late_odds)
    failed = [e for e in late_odds if e["status"] == "failed"]
    assert failed and all(e["detail"].startswith("OddsApiError: ") for e in failed)
    assert "sk-test" not in json.dumps(run.plan)
    skipped = [e for e in late_odds if e["status"] == "skipped"]
    assert skipped and all(e["detail"] == "circuit_open" for e in skipped)
    injuries = _entries(run, "injuries")
    assert len(injuries) > 3 and all(e["status"] == "done" for e in injuries)


def test_tip_moved_later_reschedules_closing(sessions: sessionmaker[Session]) -> None:
    clock = FakeClock(_hm(23, 30))
    world = World(clock, {"evt1": TIP1})
    new_tip = _hm(0, 30)

    def reschedule() -> None:  # a schedule refresh elsewhere moved the tip by 30 min
        world.tips["evt1"] = new_tip
        with sessions.begin() as session:
            pk = _event_pk(sessions, "evt1")
            session.execute(update(Event).where(Event.id == pk).values(commence_time=new_tip))

    clock.hooks[_hm(23, 57)] = reschedule
    run_id = _runner(sessions, clock, world).run(Sport.NBA, SLATE)
    assert clock.hooks == {}  # the reschedule really happened

    run = _run(sessions, run_id)
    assert run.status == "completed"
    closings = _entries(run, "closing")
    assert [(e["at"], e["event_ids"], e["status"]) for e in closings] == [
        (_iso(_hm(23, 57)), ["evt1"], "skipped"),
        (_iso(_hm(0, 27)), ["evt1"], "done"),
    ]
    assert closings[0]["detail"] == "evt1: tip_moved"
    assert closings[1]["reason"] == "tip_moved"
    snaps = _closing_snapshots(sessions, _event_pk(sessions, "evt1"))
    assert snaps and {s.captured_at for s in snaps} == {_hm(0, 27)}


def test_started_game_never_priced(sessions: sessionmaker[Session]) -> None:
    clock = FakeClock(_hm(23, 0))
    world = World(clock, {"evt1": TIP1, "evt2": TIP1})  # one shared tip: one closing batch

    def tip_off() -> None:
        with sessions.begin() as session:
            pk = _event_pk(sessions, "evt1")
            session.execute(update(Event).where(Event.id == pk).values(status="in_progress"))

    clock.hooks[_hm(23, 57)] = tip_off
    run_id = _runner(sessions, clock, world).run(Sport.NBA, SLATE)
    assert clock.hooks == {}

    run = _run(sessions, run_id)
    [closing] = _entries(run, "closing")
    assert closing["event_ids"] == ["evt1", "evt2"]
    assert (closing["status"], closing["detail"]) == ("done", "evt1: started")
    assert _closing_snapshots(sessions, _event_pk(sessions, "evt1")) == []
    assert _closing_snapshots(sessions, _event_pk(sessions, "evt2"))
    closing_calls = [url for at, url in world.odds_calls if at == _hm(23, 57)]
    assert closing_calls and not any("evt1" in str(url) for url in closing_calls)
    game_lines_call = next(u for u in closing_calls if u.path.endswith("/basketball_nba/odds"))
    assert game_lines_call.params["eventIds"] == "evt2"


def test_replan_fits_remaining_budget(sessions: sessionmaker[Session]) -> None:
    """R10: after a refresh the remaining actions are fit into cap - credits used so far."""
    clock = FakeClock(_hm(23, 0))
    world = World(clock, {"evt1": TIP1, "evt2": TIP2})
    world.costs[("props", _hm(23, 0))] = 12  # start props really cost twice the estimate
    run_id = _runner(sessions, clock, world, cap=54).run(Sport.NBA, SLATE)

    run = _run(sessions, run_id)
    # By the 00:00 refresh: 3 (lines) + 24 (start props) + 9 (evt1 closing) = 36 used, 18 left;
    # remaining est 3 x 3 hourly lines + 6 T-90 props + 9 closing = 24 -> drop latest hourly first.
    dropped = [(d["kind"], d["at"], d["status"], d["detail"]) for d in run.plan["dropped"]]
    assert dropped == [
        ("game_lines", _iso(_hm(1, 0)), "skipped", "budget"),
        ("game_lines", _iso(_hm(2, 0)), "skipped", "budget"),
    ]
    executed_lines = [e["at"] for e in _entries(run, "game_lines")]
    assert executed_lines == [_iso(_hm(23, 0)), _iso(_hm(0, 0))]
    assert [e["status"] for e in _entries(run, "closing")] == ["done", "done"]
    for odds_id in ("evt1", "evt2"):
        assert _closing_snapshots(sessions, _event_pk(sessions, odds_id))
    assert run.credits_used == 54


def test_props_cannot_spend_the_closing_reserve(sessions: sessionmaker[Session]) -> None:
    """I1: a non-closing action must leave every pending closing's estimate in reserve."""
    clock = FakeClock(_hm(23, 0))
    world = World(clock, {"evt1": TIP1})
    world.costs[("lines", _hm(23, 0))] = 9  # the start lines overrun: 9 of the 18 cap is gone
    run_id = _runner(sessions, clock, world, cap=18).run(Sport.NBA, SLATE)

    run = _run(sessions, run_id)
    assert run.plan["dropped"] == []  # the plan (3 + 6 + 9) fit; the overrun is runtime-only
    [props] = _entries(run, "props")
    assert props["status"] == "skipped" and props["detail"].startswith("session_cap: ")
    assert "reserve 9" in props["detail"]
    [closing] = _entries(run, "closing")
    assert closing["status"] == "done"
    assert _closing_snapshots(sessions, _event_pk(sessions, "evt1"))
    assert run.credits_used == world.billed == 18


def test_missing_last_header_is_charged_the_estimate(sessions: sessionmaker[Session]) -> None:
    """I2: without x-requests-last the guard charges estimates, so the cap still binds."""
    clock = FakeClock(_hm(23, 0))
    world = World(clock, {"evt1": TIP1, "evt2": TIP2})
    world.omit_last = True
    run_id = _runner(sessions, clock, world, cap=30).run(Sport.NBA, SLATE)

    run = _run(sessions, run_id)
    with sessions() as session:
        ledger = session.scalars(
            select(QuotaLedger.cost).where(QuotaLedger.capture_run_id == run_id)
        ).all()
    assert ledger and set(ledger) == {0}  # the ledger still records what the headers said
    # Charged at estimate, every re-plan sees what is left and drops hourly lines: start lines 3
    # + evt1 closing 9 + 00:00 lines 3 + evt2 T-90 props 6 + evt2 closing 9 = 30, never more.
    assert run.credits_used == world.billed == 30
    assert [e["at"] for e in _entries(run, "game_lines")] == [_iso(_hm(23, 0)), _iso(_hm(0, 0))]
    assert [e["status"] for e in _entries(run, "closing")] == ["done", "done"]


def test_parser_errors_do_not_open_the_breaker(
    sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """I3 / spec 7: a parse error fails its action only; the provider stays healthy."""
    real = jobs.parse_odds

    def props_parser_bug(payload: Any, captured_at: datetime) -> Any:
        if isinstance(payload, dict):  # props payloads are single events; lines are lists
            raise ValueError("props parser bug")
        return real(payload, captured_at)

    monkeypatch.setattr("sharpline.ingest.jobs.parse_odds", props_parser_bug)
    tip = _hm(23, 10)  # closing at 23:07, inside the breaker's 10-min cooldown
    clock = FakeClock(_hm(23, 0))
    world = World(clock, {"evt1": tip, "evt2": tip, "evt3": tip})
    run_id = _runner(sessions, clock, world).run(Sport.NBA, SLATE)

    run = _run(sessions, run_id)
    [props] = _entries(run, "props")  # three calls, three parser failures in a row
    assert props["status"] == "failed" and props["detail"].count("ValueError: ") == 3
    [closing] = _entries(run, "closing")
    assert closing["status"] == "failed" and "circuit_open" not in closing["detail"]
    assert not any("circuit_open" in (e["detail"] or "") for e in _entries(run))
    for odds_id in ("evt1", "evt2"):  # the closing game lines parsed and were written
        assert _closing_snapshots(sessions, _event_pk(sessions, odds_id))


def test_interrupt_marks_run_interrupted(sessions: sessionmaker[Session]) -> None:
    clock = FakeClock(_hm(23, 0))
    world = World(clock, {"evt1": TIP1, "evt2": TIP2})

    def ctrl_c() -> None:
        raise KeyboardInterrupt

    clock.hooks[_hm(23, 45)] = ctrl_c  # while waiting for the T-15 injuries refresh
    runner = _runner(sessions, clock, world)
    with pytest.raises(KeyboardInterrupt):
        runner.run(Sport.NBA, SLATE)

    run = _run(sessions)
    assert run.status == "interrupted" and run.finished_at == _hm(23, 45)
    assert [e["at"] for e in _entries(run, "injuries")] == [_iso(_hm(23, 0)), _iso(_hm(23, 30))]
    assert run.plan["pending"] and run.plan["pending"][0]["at"] == _iso(_hm(23, 45))
    assert all(p["status"] == "pending" for p in run.plan["pending"])
    assert run.credits_used == 15


@pytest.mark.parametrize("status", [404, 422])
def test_delisted_event_errors_do_not_open_the_breaker(
    sessions: sessionmaker[Session], status: int
) -> None:
    """F8: a 404 (delisted event) or 422 fails its action; it is not a provider outage."""
    tip = _hm(23, 10)  # closing at 23:07, inside the breaker's 10-min cooldown
    clock = FakeClock(_hm(23, 0))
    world = World(clock, {"evt1": tip, "evt2": tip, "evt3": tip})
    world.props_status = status
    run_id = _runner(sessions, clock, world).run(Sport.NBA, SLATE)

    run = _run(sessions, run_id)
    [props] = _entries(run, "props")  # three calls, three client errors in a row
    assert props["status"] == "failed"
    assert (
        props["detail"].count("OddsApiError: odds api ") == 3
        and f"HTTP {status}" in props["detail"]
    )
    assert not any("circuit_open" in (e["detail"] or "") for e in _entries(run))
    [closing] = _entries(run, "closing")
    assert closing["status"] == "failed"  # its props 404 again, but its game lines ran
    for odds_id in ("evt1", "evt2"):
        assert _closing_snapshots(sessions, _event_pk(sessions, odds_id))


def test_postponed_game_is_dropped_at_the_next_schedule_refresh(
    sessions: sessionmaker[Session],
) -> None:
    """F3: an ESPN refresh mid-session reports evt2 postponed: its T-90 props and closing are
    dropped (recorded with the reason) and no Odds API props call is made for it."""
    clock = FakeClock(_hm(23, 0))
    world = World(clock, {"evt1": TIP1, "evt2": TIP2})

    def postpone() -> None:  # before the 00:00 hourly refresh
        world.espn_status["evt2"] = "STATUS_POSTPONED"

    clock.hooks[_hm(0, 0)] = postpone
    run_id = _runner(sessions, clock, world).run(Sport.NBA, SLATE)
    assert clock.hooks == {}

    run = _run(sessions, run_id)
    assert run.status == "completed"
    with sessions() as session:
        assert session.get_one(Event, _event_pk(sessions, "evt2")).status == "postponed"
    assert [(e["event_ids"], e["status"]) for e in _entries(run, "closing")] == [(["evt1"], "done")]
    dropped = [(d["kind"], d["at"], d["event_ids"], d["detail"]) for d in run.plan["dropped"]]
    assert dropped == [
        ("props", _iso(_hm(1, 0)), ["evt2"], "evt2: tip_moved"),
        ("closing", _iso(_hm(2, 27)), ["evt2"], "evt2: tip_moved"),
    ]
    assert all(d["status"] == "skipped" for d in run.plan["dropped"])
    assert run.plan["pending"] == []
    late_evt2 = [
        url for at, url in world.odds_calls if at >= _hm(0, 0) and "/events/evt2/" in url.path
    ]
    assert late_evt2 == []
    assert _closing_snapshots(sessions, _event_pk(sessions, "evt2")) == []


def test_closing_rechecks_the_tip_before_each_odds_call(sessions: sessionmaker[Session]) -> None:
    """F6: the closing batch's game-lines call answers after tip, so the props calls that
    would follow are not made ("started"), and the late lines are not closing snapshots."""
    clock = FakeClock(_hm(23, 0))
    world = World(clock, {"evt1": TIP1, "evt2": TIP1})  # one shared tip: one closing batch
    world.slow_lines[_hm(23, 57)] = timedelta(minutes=4)  # answers at 00:01
    run_id = _runner(sessions, clock, world).run(Sport.NBA, SLATE)

    run = _run(sessions, run_id)
    [closing] = _entries(run, "closing")
    assert closing["status"] == "done"
    assert closing["detail"] == "evt1: started; evt2: started"
    closing_props = [
        url for at, url in world.odds_calls if at >= _hm(23, 57) and "/events/" in url.path
    ]
    assert closing_props == []
    for odds_id in ("evt1", "evt2"):
        assert _closing_snapshots(sessions, _event_pk(sessions, odds_id)) == []


def test_transient_db_errors_in_persist_are_survived(sessions: sessionmaker[Session]) -> None:
    """F5: a dropped connection while recording progress is logged and retried next time."""
    clock = FakeClock(_hm(23, 0))
    world = World(clock, {"evt1": TIP1, "evt2": TIP2})
    flaky = FlakySessions(sessions, "_persist", 2, clock=clock, armed_from=_hm(23, 30))
    run_id = _runner(flaky, clock, world).run(Sport.NBA, SLATE)

    assert len(flaky.raised) == 2  # after two consecutive actions
    run = _run(sessions, run_id)
    assert run.status == "completed"
    assert all(e["status"] == "done" for e in _entries(run)), _entries(run)
    assert [e["status"] for e in _entries(run, "closing")] == ["done", "done"]
    assert run.credits_used == 48  # as without the errors


def test_transient_db_errors_in_replan_keep_the_previous_plan(
    sessions: sessionmaker[Session],
) -> None:
    """F5: the 00:00 refresh cannot re-read the slate twice running; the session goes on
    with its previous plan and the third try (after the next action) succeeds."""
    clock = FakeClock(_hm(23, 0))
    world = World(clock, {"evt1": TIP1, "evt2": TIP2})
    flaky = FlakySessions(sessions, "_load_slate", 2, clock=clock, armed_from=_hm(0, 0))
    run_id = _runner(flaky, clock, world).run(Sport.NBA, SLATE)

    assert len(flaky.raised) == 2
    run = _run(sessions, run_id)
    assert run.status == "completed"
    assert all(e["status"] == "done" for e in _entries(run)), _entries(run)
    actions = [(e["at"], e["kind"], tuple(e["event_ids"])) for e in _entries(run)]
    assert len(actions) == len(set(actions))  # nothing ran twice after the late re-plan
    closings = [(e["at"], e["event_ids"]) for e in _entries(run, "closing")]
    assert closings == [(_iso(_hm(23, 57)), ["evt1"]), (_iso(_hm(2, 27)), ["evt2"])]
    for odds_id in ("evt1", "evt2"):
        assert _closing_snapshots(sessions, _event_pk(sessions, odds_id))


@pytest.mark.parametrize(
    ("caller", "armed_from"), [("_load_slate", (0, 0)), ("_persist", (23, 30))]
)
def test_three_consecutive_db_failures_fail_the_run(
    sessions: sessionmaker[Session], caller: str, armed_from: tuple[int, int]
) -> None:
    """F5: the third consecutive failure ends the run; it is still marked failed, from a
    fresh session."""
    clock = FakeClock(_hm(23, 0))
    world = World(clock, {"evt1": TIP1, "evt2": TIP2})
    flaky = FlakySessions(sessions, caller, 3, clock=clock, armed_from=_hm(*armed_from))
    runner = _runner(flaky, clock, world)
    with pytest.raises(OperationalError):
        runner.run(Sport.NBA, SLATE)

    assert len(flaky.raised) == 3
    run = _run(sessions)
    assert runner.run_id == run.id
    assert run.status == "failed" and run.finished_at == clock()


def _interrupted_first_run(sessions: sessionmaker[Session], world: World) -> CaptureRun:
    """An earlier session of the same slate day, stopped at 23:45 after 15 credits."""
    clock = FakeClock(_hm(23, 0))
    world.clock = clock

    def ctrl_c() -> None:
        raise KeyboardInterrupt

    clock.hooks[_hm(23, 45)] = ctrl_c
    with pytest.raises(KeyboardInterrupt):
        _runner(sessions, clock, world).run(Sport.NBA, SLATE)
    first = _run(sessions)
    assert (first.status, first.credits_used) == ("interrupted", 15)
    return first


def test_session_cap_is_per_slate_day(sessions: sessionmaker[Session]) -> None:
    """F5: a second session of the day starts with the first one's credits already used."""
    world = World(FakeClock(_hm(23, 0)), {"evt1": TIP1, "evt2": TIP2})
    first = _interrupted_first_run(sessions, world)
    clock = FakeClock(_hm(23, 45))
    world.clock = clock
    run_id = _runner(sessions, clock, world, cap=40).run(Sport.NBA, SLATE)

    second = _run(sessions, run_id)
    assert second.status == "completed"
    # 40 - 15 = 25 left at the start: start lines 3 + two closings 9 + 9 (start props, T-90
    # props and the 00:45 lines dropped by budget); at 01:45, 13 left fits lines 3 + closing 9
    assert second.credits_used == 24 and first.credits_used + second.credits_used <= 40
    dropped = {(d["kind"], d["reason"], d["detail"]) for d in second.plan["dropped"]}
    assert ("props", "session_start", "budget") in dropped
    assert [e["status"] for e in _entries(second, "closing")] == ["done", "done"]


def test_guard_starts_from_the_last_known_remaining(sessions: sessionmaker[Session]) -> None:
    """F5: with no quota headers yet this session, the plan already respects the floor
    against the remaining credits the ledger last recorded."""
    world = World(FakeClock(_hm(23, 0)), {"evt1": TIP1, "evt2": TIP2})
    world.quota_total = 60
    _interrupted_first_run(sessions, world)  # the ledger now says 45 remaining
    world.events_headers = False
    clock = FakeClock(_hm(23, 45))
    world.clock = clock
    run_id = _runner(sessions, clock, world, cap=1000).run(Sport.NBA, SLATE)

    second = _run(sessions, run_id)
    # 45 remaining - floor 20 = 25 to spend at the start: start lines 3 + two closings 18
    # (start props dropped); at 01:45 the headers say 33 left: 13 more fits lines + closing
    assert second.credits_used == 24
    dropped = {(d["kind"], d["reason"], d["detail"]) for d in second.plan["dropped"]}
    assert ("props", "session_start", "budget") in dropped
