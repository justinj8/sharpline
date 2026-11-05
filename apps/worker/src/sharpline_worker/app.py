"""procrastinate application: the scheduler/worker process.

Jobs live in Postgres (the same database as everything else), so a job and the rows
it writes commit or roll back together.
"""

from __future__ import annotations

import procrastinate
import structlog

from sharpline.config import Settings
from sharpline.db.session import normalize_database_url

HEARTBEAT_CRON = "*/5 * * * *"
OPS_QUEUE = "ops"
CAPTURE_CRON = "*/15 * * * *"
CAPTURE_QUEUE = "capture"

log = structlog.get_logger()


def procrastinate_conninfo(database_url: str) -> str:
    """psycopg conninfo URL: same as SQLAlchemy's, minus the `+psycopg` driver marker."""
    return normalize_database_url(database_url).replace("postgresql+psycopg://", "postgresql://", 1)


def heartbeat(timestamp: int) -> str:
    """Periodic liveness signal; `timestamp` is the scheduled tick (unix seconds)."""
    log.info("worker.heartbeat", scheduled_for=timestamp)
    return "ok"


def plan_and_enqueue(timestamp: int) -> str:
    """Live-mode tick (spec §6.3): plan the day's capture and enqueue the due actions.

    Registration only for now: executing an action as its own job needs provider clients
    built from Settings and quota state shared across jobs, which arrive with the CLI
    (Task 12). Until then the tick logs and does nothing, so enabling live mode is inert.
    """
    log.info("capture.plan_and_enqueue", scheduled_for=timestamp, enqueued=0)
    return "noop"


def worker_queues(settings: Settings) -> list[str]:
    """Queues the worker consumes: ops always, capture only in live mode."""
    return [OPS_QUEUE, CAPTURE_QUEUE] if settings.live_capture else [OPS_QUEUE]


def build_app(
    settings: Settings, connector: procrastinate.BaseConnector | None = None
) -> procrastinate.App:
    app = procrastinate.App(
        connector=connector
        or procrastinate.PsycopgConnector(conninfo=procrastinate_conninfo(settings.database_url))
    )
    task = app.task(name="sharpline_worker.app.heartbeat", queue=OPS_QUEUE)(heartbeat)
    app.periodic(cron=HEARTBEAT_CRON, periodic_id="heartbeat")(task)
    if settings.live_capture:  # off by default: no capture job unless explicitly enabled
        capture = app.task(name="sharpline_worker.app.plan_and_enqueue", queue=CAPTURE_QUEUE)(
            plan_and_enqueue
        )
        app.periodic(cron=CAPTURE_CRON, periodic_id="plan_and_enqueue")(capture)
    return app
