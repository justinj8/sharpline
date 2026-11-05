"""CaptureRunner against a private database, a fake clock and MockTransport providers."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select, update
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
from sharpline.sources.espn.client import EspnClient
from sharpline.sources.http import CircuitBreaker, HttpClient
from sharpline.sources.odds_api.client import OddsApiClient

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parents[2] / "fixtures"
SLATE = date(2026, 11, 4)  # ET; both tips below are that evening
TIP1 = datetime(2026, 11, 5, 0, 0, tzinfo=UTC)
TIP2 = datetime(2026, 11, 5, 2, 30, tzinfo=UTC)
ODDS_KINDS = ("game_lines", "props", "closing")
ESPN_IDS = {"evt1": "e1", "evt2": "e2"}
ESPN_TEAMS = {"evt1": ("BOS", "NY"), "evt2": ("LAC", "DEN")}  # (home, away) ESPN abbreviations


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


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
        self.prop_cost: int | None = None  # override the x-requests-last of props calls
        self._used = 0

    def espn(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/injuries"):
            # Injury rows are the writer's business (tested there); keep the runner fast.
            return httpx.Response(200, json={"injuries": []})
        events = [
            {
                "id": ESPN_IDS[key],
                "date": tip.strftime("%Y-%m-%dT%H:%MZ"),
                "status": {"type": {"name": "STATUS_SCHEDULED"}},
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
            body = [
                self._retimed(e) for e in _load("odds_api/nba_events.json") if e["id"] in self.tips
            ]
            cost = 0
        elif "/events/" in path:
            # Props carry no books here: player resolution is covered by the writer tests.
            event_id = path.split("/")[-2]
            meta = next(e for e in _load("odds_api/nba_events.json") if e["id"] == event_id)
            body = self._retimed({**meta, "bookmakers": []})
            cost = self.prop_cost or len(request.url.params["markets"].split(","))
        else:
            wanted = request.url.params.get("eventIds")
            body = [
                self._retimed(e)
                for e in _load("odds_api/nba_game_odds.json")
                if e["id"] in self.tips and (wanted is None or e["id"] in wanted.split(","))
            ]
            cost = len(request.url.params["markets"].split(","))
        self._used += cost
        headers = {
            "x-requests-last": str(cost),
            "x-requests-used": str(self._used),
            "x-requests-remaining": str(500 - self._used),
        }
        return httpx.Response(200, json=body, headers=headers)


def _http(base: str, handler: Callable[[httpx.Request], httpx.Response]) -> HttpClient:
    return HttpClient(
        base, transport=httpx.MockTransport(handler), sleep=lambda _s: None, max_retries=0
    )


def _runner(
    sessions: sessionmaker[Session],
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
        sessions,
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
    world.prop_cost = 12  # props really cost twice the estimate
    run_id = _runner(sessions, clock, world, cap=60).run(Sport.NBA, SLATE)

    run = _run(sessions, run_id)
    # By the 00:00 refresh: 3 (lines) + 24 (start props) + 15 (evt1 closing) = 42 used, 18 left;
    # remaining est 3 x 3 hourly lines + 6 T-90 props + 9 closing = 24 -> drop latest hourly first.
    dropped = [(d["kind"], d["at"], d["status"], d["detail"]) for d in run.plan["dropped"]]
    assert dropped == [
        ("game_lines", _iso(_hm(1, 0)), "skipped", "budget"),
        ("game_lines", _iso(_hm(2, 0)), "skipped", "budget"),
    ]
    executed_lines = [e["at"] for e in _entries(run, "game_lines")]
    assert executed_lines == [_iso(_hm(23, 0)), _iso(_hm(0, 0))]
    assert run.credits_used <= 60


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
