"""Live mode through the worker app: the tick reads the stored day and enqueues one session."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import procrastinate.testing
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from sharpline.bundles.format import read_bundle
from sharpline.capture import clients
from sharpline.capture.quota import QuotaGuard
from sharpline.capture.runner import CaptureRunner
from sharpline.config import Settings
from sharpline.db.models import CaptureRun
from sharpline.ingest import jobs
from sharpline.sources.espn.client import EspnClient
from sharpline.sources.http import CircuitBreaker, HttpClient
from sharpline_worker.app import (
    CAPTURE_QUEUE,
    PLAN_AND_ENQUEUE_TASK,
    RUN_CAPTURE_TASK,
    build_app,
    read_live_state,
    recover_orphaned_runs,
)

pytestmark = pytest.mark.integration

SLATE = date(2026, 11, 12)
TIP = datetime(2026, 11, 13, 0, 30, tzinfo=UTC)  # 19:30 ET
NOW = datetime(2026, 11, 12, 22, 0, tzinfo=UTC)  # 17:00 ET
TICK = int(NOW.timestamp())


def _espn(clock: Any = lambda: NOW) -> EspnClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/injuries"):
            return httpx.Response(200, json={"injuries": []})
        event = {
            "id": "e1",
            "date": TIP.strftime("%Y-%m-%dT%H:%MZ"),
            "status": {"type": {"name": "STATUS_SCHEDULED"}},
            "competitions": [
                {
                    "competitors": [
                        {"homeAway": "home", "team": {"abbreviation": "BOS"}},
                        {"homeAway": "away", "team": {"abbreviation": "NY"}},
                    ]
                }
            ],
        }
        return httpx.Response(200, json={"events": [event]})

    http = HttpClient(
        clients.ESPN_BASE_URL,
        transport=httpx.MockTransport(handler),
        sleep=lambda _s: None,
        max_retries=0,
    )
    return EspnClient(http, clock=clock)


def _run(sessions: sessionmaker[Session], status: str, mode: str = "live") -> int:
    with sessions.begin() as session:
        run = CaptureRun(
            sport="nba",
            slate_date=SLATE,
            mode=mode,
            started_at=NOW,
            status=status,
            credits_cap=180,
            plan={},
        )
        session.add(run)
        session.flush()
        return run.id


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None, database_url="postgresql://u:p@h/d", env="test", live_capture=True
    )


def test_read_live_state_sees_stored_games_and_runs(
    db_session_factory: sessionmaker[Session],
) -> None:
    with db_session_factory() as session:
        jobs.ingest_schedule(session, _espn(), SLATE)
    _run(db_session_factory, "failed")
    state = read_live_state(db_session_factory, SLATE)
    assert state.statuses == ["failed"]
    assert [(g.provider_event_id, g.commence_time) for g in state.games] == [("e1", TIP)]
    assert read_live_state(db_session_factory, date(2026, 11, 13)).games == []


def test_tick_enqueues_one_session_job_with_a_day_lock(
    db_session_factory: sessionmaker[Session],
) -> None:
    connector = procrastinate.testing.InMemoryConnector()
    app = build_app(_settings(), connector=connector, session_factory=db_session_factory)
    tick = app.tasks[PLAN_AND_ENQUEUE_TASK]

    assert tick(timestamp=TICK) == "enqueued"
    (job,) = connector.jobs.values()
    assert job["task_name"] == RUN_CAPTURE_TASK and job["queue_name"] == CAPTURE_QUEUE
    assert job["queueing_lock"] == "capture:nba:2026-11-12"
    assert job["args"] == {"sport": "nba", "slate_date": "2026-11-12"}

    assert tick(timestamp=TICK + 900) == "skipped"  # still queued: the lock holds
    assert len(connector.jobs) == 1

    connector.reset()
    _run(db_session_factory, "running")
    assert tick(timestamp=TICK + 1800) == "skipped"
    assert connector.jobs == {}


def test_session_job_runs_the_capture_in_live_mode(
    db_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[tuple[Settings, int | None]] = []

    class Clock:
        now = NOW

        def __call__(self) -> datetime:
            return self.now

        def sleep_until(self, at: datetime) -> None:
            self.now = max(self.now, at)

    def build_runner(
        sessions: sessionmaker[Session], settings: Settings, *, cap: int | None = None
    ) -> CaptureRunner:
        built.append((settings, cap))
        clock = Clock()
        return CaptureRunner(
            sessions,
            espn=_espn(clock),
            odds=None,
            guard=QuotaGuard(settings.capture_session_cap, settings.capture_quota_floor),
            breaker=CircuitBreaker(clock=clock),
            clock=clock,
            sleep_until=clock.sleep_until,
        )

    monkeypatch.setattr(clients, "build_runner", build_runner)
    settings = _settings()
    app = build_app(
        settings,
        connector=procrastinate.testing.InMemoryConnector(),
        session_factory=db_session_factory,
    )
    run_id = app.tasks[RUN_CAPTURE_TASK](sport="nba", slate_date="2026-11-12")
    assert built == [(settings, None)]
    with db_session_factory() as session:
        run = session.scalars(select(CaptureRun)).one()
    assert (run.id, run.mode, run.status, run.slate_date) == (run_id, "live", "completed", SLATE)


def test_startup_recovers_an_orphaned_live_run_and_the_next_tick_retries(
    db_session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """A worker killed mid-session leaves its live run `running`; without recovery every
    later tick would skip the day for good (spec section 7)."""
    with db_session_factory() as session:
        jobs.ingest_schedule(session, _espn(), SLATE)
    live = _run(db_session_factory, "running")
    manual = _run(db_session_factory, "running", mode="manual")
    restart = NOW + timedelta(hours=1)

    recovered = recover_orphaned_runs(db_session_factory, out_dir=tmp_path, now=restart)

    assert [run_id for run_id, _path in recovered] == [live]
    (path,) = (p for _run_id, p in recovered)
    assert path is not None and path.parent == tmp_path
    assert read_bundle(path)[0].capture_run_id == live
    with db_session_factory() as session:
        live_run = session.get_one(CaptureRun, live)
        assert (live_run.status, live_run.finished_at) == ("interrupted", restart)
        assert session.get_one(CaptureRun, manual).status == "running"  # not ours to touch
    assert recover_orphaned_runs(db_session_factory, out_dir=tmp_path, now=restart) == []

    with db_session_factory.begin() as session:  # the manual run would block the day too
        session.get_one(CaptureRun, manual).status = "interrupted"
    connector = procrastinate.testing.InMemoryConnector()
    app = build_app(_settings(), connector=connector, session_factory=db_session_factory)
    assert app.tasks[PLAN_AND_ENQUEUE_TASK](timestamp=int(restart.timestamp())) == "enqueued"
    (job,) = connector.jobs.values()
    assert job["args"] == {"sport": "nba", "slate_date": "2026-11-12"}
