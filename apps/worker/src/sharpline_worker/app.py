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

log = structlog.get_logger()


def procrastinate_conninfo(database_url: str) -> str:
    """psycopg conninfo URL: same as SQLAlchemy's, minus the `+psycopg` driver marker."""
    return normalize_database_url(database_url).replace("postgresql+psycopg://", "postgresql://", 1)


def heartbeat(timestamp: int) -> str:
    """Periodic liveness signal; `timestamp` is the scheduled tick (unix seconds)."""
    log.info("worker.heartbeat", scheduled_for=timestamp)
    return "ok"


def build_app(
    settings: Settings, connector: procrastinate.BaseConnector | None = None
) -> procrastinate.App:
    app = procrastinate.App(
        connector=connector
        or procrastinate.PsycopgConnector(conninfo=procrastinate_conninfo(settings.database_url))
    )
    task = app.task(name="sharpline_worker.app.heartbeat", queue=OPS_QUEUE)(heartbeat)
    app.periodic(cron=HEARTBEAT_CRON, periodic_id="heartbeat")(task)
    return app
