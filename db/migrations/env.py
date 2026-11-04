"""Alembic environment.

URL precedence: an explicit `sqlalchemy.url` main option (set by the test fixtures)
wins; otherwise DATABASE_URL from Settings. Both pass through normalize_database_url.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

import sharpline.db.models  # noqa: F401  (registers every table on Base.metadata)
from sharpline.config import get_settings
from sharpline.db.base import Base, include_object
from sharpline.db.session import normalize_database_url

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _url() -> str:
    explicit = config.get_main_option("sqlalchemy.url")
    return normalize_database_url(explicit or get_settings().database_url)


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        include_object=include_object,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url())
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
