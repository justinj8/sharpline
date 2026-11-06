"""Entry point: `python -m sharpline_worker` runs the worker (ops, plus capture when live)."""

from __future__ import annotations

import asyncio

import structlog

from sharpline.config import get_settings
from sharpline.observability.logging import configure_logging
from sharpline_worker.app import build_app, worker_concurrency, worker_queues


async def _run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json_output=settings.env != "local")
    app = build_app(settings)
    queues = worker_queues(settings)
    concurrency = worker_concurrency(settings)
    log = structlog.get_logger()
    log.info("worker.start", queues=queues, concurrency=concurrency, env=settings.env)
    if settings.live_capture:
        log.warning("capture.live.enabled", setting="SHARPLINE_LIVE_CAPTURE")
    async with app.open_async():
        await app.run_worker_async(queues=queues, concurrency=concurrency)


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
