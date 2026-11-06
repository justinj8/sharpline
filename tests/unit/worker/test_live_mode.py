from datetime import UTC, date, datetime

import procrastinate.testing
import pytest
import structlog.testing
from procrastinate.exceptions import AlreadyEnqueued

from sharpline.capture.plan import SlateGame
from sharpline.config import Settings
from sharpline_worker.app import (
    CAPTURE_CRON,
    CAPTURE_QUEUE,
    OPS_QUEUE,
    RUN_CAPTURE_TASK,
    SHUTDOWN_GRACE_S,
    LiveState,
    build_app,
    capture_lock,
    live_tick,
    should_enqueue,
    worker_options,
)

# 22:00 ET on Thursday 2026-11-12 (03:00 UTC on the 13th): still the 12th's slate.
TICK = int(datetime(2026, 11, 13, 3, 0, tzinfo=UTC).timestamp())
SLATE = date(2026, 11, 12)
TIP = datetime(2026, 11, 13, 3, 30, tzinfo=UTC)


def _settings(*, live: bool) -> Settings:
    return Settings(database_url="postgresql://u:p@h/d", env="test", live_capture=live)


def _app(live: bool) -> procrastinate.App:
    return build_app(_settings(live=live), connector=procrastinate.testing.InMemoryConnector())


def _periodic(live: bool) -> dict[str, tuple[str, str | None]]:
    app = _app(live)
    return {
        p.periodic_id: (p.cron, p.task.queue) for p in app.periodic_registry.periodic_tasks.values()
    }


def test_live_mode_off_registers_only_heartbeat() -> None:
    assert _periodic(live=False) == {"heartbeat": ("*/5 * * * *", OPS_QUEUE)}
    assert RUN_CAPTURE_TASK not in _app(live=False).tasks
    assert worker_options(_settings(live=False)) == {
        "queues": [OPS_QUEUE],
        "concurrency": 1,
        "shutdown_graceful_timeout": SHUTDOWN_GRACE_S,
    }


def test_live_mode_on_registers_tick_and_session_job() -> None:
    assert CAPTURE_CRON == "*/15 * * * *" and CAPTURE_QUEUE == "capture"
    assert _periodic(live=True) == {
        "heartbeat": ("*/5 * * * *", OPS_QUEUE),
        "plan_and_enqueue": (CAPTURE_CRON, CAPTURE_QUEUE),
    }
    assert _app(live=True).tasks[RUN_CAPTURE_TASK].queue == CAPTURE_QUEUE
    # The worker must consume the capture queue, or the jobs would pile up; a session job
    # runs for hours, so a second slot keeps the tick and the heartbeat running beside it.
    assert worker_options(_settings(live=True)) == {
        "queues": [OPS_QUEUE, CAPTURE_QUEUE],
        "concurrency": 2,
        "shutdown_graceful_timeout": SHUTDOWN_GRACE_S,
    }


def test_shutdown_does_not_wait_forever_on_a_session_job() -> None:
    # SIGTERM must not hang on an hours-long capture thread until the host SIGKILLs us;
    # the orphaned run is recovered at the next startup.
    assert 0 < SHUTDOWN_GRACE_S <= 30


def test_capture_lock_is_per_sport_and_day() -> None:
    assert capture_lock(SLATE) == "capture:nba:2026-11-12"


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([], True),
        (["failed"], True),
        (["interrupted", "failed"], True),
        (["running"], False),
        (["completed"], False),
        (["failed", "completed"], False),
    ],
)
def test_should_enqueue_only_without_a_running_or_completed_run(
    statuses: list[str], expected: bool
) -> None:
    assert should_enqueue(statuses) is expected


class _Tick:
    def __init__(self, statuses: list[str], *, already: bool = False) -> None:
        self.statuses = statuses
        self.already = already
        self.read: list[date] = []
        self.deferred: list[date] = []

    def read_state(self, slate_date: date) -> LiveState:
        self.read.append(slate_date)
        return LiveState(statuses=self.statuses, games=[SlateGame("evt1", TIP)])

    def defer(self, slate_date: date) -> None:
        if self.already:
            raise AlreadyEnqueued("queued")
        self.deferred.append(slate_date)

    def __call__(self) -> str:
        return live_tick(
            TICK,
            timezone="America/New_York",
            cap=180,
            read_state=self.read_state,
            defer=self.defer,
        )


def test_tick_enqueues_one_session_for_the_eastern_slate_day() -> None:
    tick = _Tick([])
    with structlog.testing.capture_logs() as logs:
        assert tick() == "enqueued"
    assert tick.read == [SLATE] and tick.deferred == [SLATE]
    plan = next(entry for entry in logs if entry["event"] == "capture.live.plan")
    # 22:00 ET start, 22:30 ET tip: schedule, injuries x2, game lines, props, closing.
    assert plan["slate_date"] == "2026-11-12"
    assert plan["actions"] == 6 and plan["est_credits"] == 3 + 6 + 9
    assert plan["dropped"] == 0


@pytest.mark.parametrize("status", ["running", "completed"])
def test_tick_skips_when_the_day_already_has_a_session(status: str) -> None:
    tick = _Tick([status])
    with structlog.testing.capture_logs() as logs:
        assert tick() == "skipped"
    assert tick.deferred == []
    assert {"event": "capture.live.skip", "reason": f"run_{status}"}.items() <= logs[-1].items()


def test_tick_skips_when_the_session_job_is_already_queued() -> None:
    tick = _Tick([], already=True)
    with structlog.testing.capture_logs() as logs:
        assert tick() == "skipped"
    assert {"event": "capture.live.skip", "reason": "already_queued"}.items() <= logs[-1].items()
