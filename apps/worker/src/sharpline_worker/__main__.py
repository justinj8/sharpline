"""Entry point: `python -m sharpline_worker` runs the worker (ops, plus capture when live)."""

from __future__ import annotations

import asyncio

import structlog

from sharpline.config import get_settings
from sharpline.observability.logging import configure_logging
from sharpline_worker.app import build_app, worker_queues


async def _run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json_output=settings.env != "local")
    app = build_app(settings)
    queues = worker_queues(settings)
    structlog.get_logger().info("worker.start", queues=queues, env=settings.env)
    async with app.open_async():
        await app.run_worker_async(queues=queues)


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
