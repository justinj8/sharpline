"""FastAPI application factory.

Run with: uvicorn sharpline_api.app:create_app --factory
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

import sharpline
from sharpline.config import Settings, get_settings
from sharpline.db.session import make_engine
from sharpline.observability.logging import configure_logging
from sharpline_api.middleware import RequestIdMiddleware
from sharpline_api.routes import health


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, json_output=settings.env != "local")
    engine = make_engine(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        engine.dispose()

    app = FastAPI(title="Sharpline API", version=sharpline.__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.add_middleware(RequestIdMiddleware)
    app.include_router(health.router, prefix="/v1")
    return app
