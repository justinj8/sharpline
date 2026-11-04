"""Engine and session construction.

Railway (and most hosts) hand out `postgres://` or `postgresql://` URLs; SQLAlchemy
needs an explicit driver, so every URL is normalized to `postgresql+psycopg://`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from sharpline.config import Settings

_DRIVER_SCHEME = "postgresql+psycopg"
_ACCEPTED_SCHEMES = ("postgres", "postgresql", _DRIVER_SCHEME)


def normalize_database_url(url: str) -> str:
    """Rewrite the URL scheme to `postgresql+psycopg`; reject non-Postgres URLs."""
    scheme, sep, rest = url.partition("://")
    if not sep or scheme not in _ACCEPTED_SCHEMES:
        raise ValueError(f"Unsupported database URL scheme: {scheme!r}")
    return f"{_DRIVER_SCHEME}://{rest}"


def make_engine(settings: Settings) -> Engine:
    """Engine with a bounded connect timeout so health checks fail fast."""
    return create_engine(
        normalize_database_url(settings.database_url),
        pool_pre_ping=True,
        connect_args={"connect_timeout": settings.db_connect_timeout_s},
    )


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)
