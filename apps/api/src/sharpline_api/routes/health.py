"""Liveness + database reachability, used by Railway's deploy health check."""

from __future__ import annotations

from typing import Literal

import structlog
from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError

import sharpline

router = APIRouter(tags=["ops"])
log = structlog.get_logger()


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    db: Literal["ok", "unreachable"]
    version: str


@router.get("/health", response_model=HealthResponse)
def health(request: Request, response: Response) -> HealthResponse:
    engine: Engine = request.app.state.engine
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError as exc:
        log.warning("health.db_unreachable", error=type(exc).__name__)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthResponse(status="degraded", db="unreachable", version=sharpline.__version__)
    return HealthResponse(status="ok", db="ok", version=sharpline.__version__)
