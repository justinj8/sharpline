"""Entry point: `python -m sharpline_worker` runs the worker on the ops queue."""

from __future__ import annotations

import asyncio

import structlog

from sharpline.config import get_settings
from sharpline.observability.logging import configure_logging
from sharpline_worker.app import OPS_QUEUE, build_app


async def _run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json_output=settings.env != "local")
    app = build_app(settings)
    structlog.get_logger().info("worker.start", queues=[OPS_QUEUE], env=settings.env)
    async with app.open_async():
        await app.run_worker_async(queues=[OPS_QUEUE])


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
