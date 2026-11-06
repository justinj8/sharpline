"""procrastinate application: the scheduler/worker process.

Jobs live in Postgres (the same database as everything else), so a job and the rows
it writes commit or roll back together.

Live mode (spec section 6.3, off unless SHARPLINE_LIVE_CAPTURE): every 15 minutes the
`plan_and_enqueue` tick resolves today's slate date, plans it from the stored events for
the log, and enqueues one `run_capture_session` job for the day unless a session for it is
already running or completed. One job per day, not one per action: the session runner
re-plans on every schedule refresh, reschedules a closing when a tip moves and keeps the
closing reserve, and it can only do that while it owns the whole day.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import procrastinate
import structlog
from procrastinate.exceptions import AlreadyEnqueued
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from sharpline.capture import clients as capture_clients
from sharpline.capture.plan import Budget, ClosingExceedsBudget, SlateGame, plan_captures
from sharpline.capture.runner import load_slate
from sharpline.config import Settings
from sharpline.db.models import CaptureRun
from sharpline.db.session import make_engine, make_session_factory, normalize_database_url
from sharpline.domain.enums import Sport

HEARTBEAT_CRON = "*/5 * * * *"
OPS_QUEUE = "ops"
CAPTURE_CRON = "*/15 * * * *"
CAPTURE_QUEUE = "capture"
PLAN_AND_ENQUEUE_TASK = "sharpline_worker.app.plan_and_enqueue"
RUN_CAPTURE_TASK = "sharpline_worker.app.run_capture_session"
LIVE_SPORT = Sport.NBA
# A session that is under way or finished owns the day; failed/interrupted ones may retry.
BLOCKING_STATUSES = frozenset({"running", "completed"})

log = structlog.get_logger()


def procrastinate_conninfo(database_url: str) -> str:
    """psycopg conninfo URL: same as SQLAlchemy's, minus the `+psycopg` driver marker."""
    return normalize_database_url(database_url).replace("postgresql+psycopg://", "postgresql://", 1)


def heartbeat(timestamp: int) -> str:
    """Periodic liveness signal; `timestamp` is the scheduled tick (unix seconds)."""
    log.info("worker.heartbeat", scheduled_for=timestamp)
    return "ok"


@dataclass(frozen=True)
class LiveState:
    """What the tick needs from the database about one slate day."""

    statuses: Sequence[str]  # of that day's capture runs
    games: Sequence[SlateGame]  # stored scheduled games


def capture_lock(slate_date: date, sport: Sport = LIVE_SPORT) -> str:
    return f"capture:{sport.value}:{slate_date.isoformat()}"


def should_enqueue(statuses: Collection[str]) -> bool:
    return not BLOCKING_STATUSES.intersection(statuses)


def read_live_state(sessions: sessionmaker[Session], slate_date: date) -> LiveState:
    with sessions() as session:
        statuses = session.scalars(
            select(CaptureRun.status).where(
                CaptureRun.sport == LIVE_SPORT.value, CaptureRun.slate_date == slate_date
            )
        ).all()
        entries = load_slate(session, LIVE_SPORT, slate_date)
    return LiveState(statuses=list(statuses), games=[e.game for e in entries])


def live_tick(
    timestamp: int,
    *,
    timezone: str,
    cap: int,
    read_state: Callable[[date], LiveState],
    defer: Callable[[date], None],
) -> str:
    """One live-mode tick: "enqueued" or "skipped". `defer` may raise AlreadyEnqueued."""
    now = datetime.fromtimestamp(timestamp, UTC)
    slate_date = now.astimezone(ZoneInfo(timezone)).date()
    state = read_state(slate_date)
    fields = {"slate_date": slate_date.isoformat(), "scheduled_for": timestamp}
    try:  # visibility only: the session plans for itself from a fresh schedule
        plan = plan_captures(state.games, now, Budget(cap))
        log.info(
            "capture.live.plan",
            **fields,
            games=len(state.games),
            actions=len(plan.actions),
            est_credits=plan.est_cost,
            dropped=len(plan.dropped),
        )
    except ClosingExceedsBudget as exc:
        log.warning("capture.live.plan", **fields, games=len(state.games), error=str(exc))
    blocking = sorted(BLOCKING_STATUSES.intersection(state.statuses))
    if blocking:
        log.info("capture.live.skip", **fields, reason=f"run_{blocking[0]}")
        return "skipped"
    try:
        defer(slate_date)
    except AlreadyEnqueued:
        log.info("capture.live.skip", **fields, reason="already_queued")
        return "skipped"
    log.info("capture.live.enqueued", **fields, lock=capture_lock(slate_date))
    return "enqueued"


def worker_queues(settings: Settings) -> list[str]:
    """Queues the worker consumes: ops always, capture only in live mode."""
    return [OPS_QUEUE, CAPTURE_QUEUE] if settings.live_capture else [OPS_QUEUE]


def worker_concurrency(settings: Settings) -> int:
    """A live session job runs for hours: a second slot keeps the tick and heartbeat going."""
    return 2 if settings.live_capture else 1


def build_app(
    settings: Settings,
    connector: procrastinate.BaseConnector | None = None,
    session_factory: sessionmaker[Session] | None = None,
) -> procrastinate.App:
    app = procrastinate.App(
        connector=connector
        or procrastinate.PsycopgConnector(conninfo=procrastinate_conninfo(settings.database_url))
    )
    task = app.task(name="sharpline_worker.app.heartbeat", queue=OPS_QUEUE)(heartbeat)
    app.periodic(cron=HEARTBEAT_CRON, periodic_id="heartbeat")(task)
    if settings.live_capture:  # off by default: no capture job unless explicitly enabled
        _register_live_capture(app, settings, session_factory)
    return app


def _register_live_capture(
    app: procrastinate.App,
    settings: Settings,
    session_factory: sessionmaker[Session] | None,
) -> None:
    sessions = session_factory or make_session_factory(make_engine(settings))

    @app.task(name=RUN_CAPTURE_TASK, queue=CAPTURE_QUEUE)
    def run_capture_session(sport: str, slate_date: str) -> int:
        """The whole day's capture session, with clients built like the CLI's."""
        runner = capture_clients.build_runner(sessions, settings)
        return runner.run(Sport(sport), date.fromisoformat(slate_date), mode="live")

    def defer(slate_date: date) -> None:
        run_capture_session.configure(queueing_lock=capture_lock(slate_date)).defer(
            sport=LIVE_SPORT.value, slate_date=slate_date.isoformat()
        )

    @app.periodic(cron=CAPTURE_CRON, periodic_id="plan_and_enqueue")
    @app.task(name=PLAN_AND_ENQUEUE_TASK, queue=CAPTURE_QUEUE)
    def plan_and_enqueue(timestamp: int) -> str:
        return live_tick(
            timestamp,
            timezone=settings.schedule_timezone,
            cap=settings.capture_session_cap,
            read_state=lambda day: read_live_state(sessions, day),
            defer=defer,
        )
