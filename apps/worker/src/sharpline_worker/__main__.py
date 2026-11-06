"""Entry point: `python -m sharpline_worker` runs the worker (ops, plus capture when live)."""

from __future__ import annotations

import asyncio

import structlog

from sharpline.config import get_settings
from sharpline.db.session import make_engine, make_session_factory
from sharpline.observability.logging import configure_logging
from sharpline_worker.app import build_app, recover_orphaned_runs, worker_options


async def _run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json_output=settings.env != "local")
    sessions = make_session_factory(make_engine(settings))
    app = build_app(settings, session_factory=sessions)
    options = worker_options(settings)
    log = structlog.get_logger()
    log.info("worker.start", **options, env=settings.env)
    if settings.live_capture:
        log.warning("capture.live.enabled", setting="SHARPLINE_LIVE_CAPTURE")
    # Before consuming: a live session the previous worker was running when it died stays
    # `running` and would block its day forever (single replica, see the function).
    await asyncio.to_thread(recover_orphaned_runs, sessions)
    async with app.open_async():
        await app.run_worker_async(**options)


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
