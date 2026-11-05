"""Shared fixtures.

Integration tests get a throwaway database: one `sharpline_test_<hex>` database is
created per test session on the server named by TEST_DATABASE_URL (the Railway dev
Postgres locally, a service container in CI), migrated to head, and dropped at the end.
Each test then runs inside a transaction that is rolled back, so tests never see each
other's rows.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from dotenv import load_dotenv
from sqlalchemy import Engine, create_engine, make_url, text
from sqlalchemy.orm import Session, sessionmaker

from sharpline.db.models import Event, Market, Player, Team
from sharpline.db.session import normalize_database_url
from sharpline.db.sources import record_source
from sharpline.sports.nba.teams import seed_nba_teams

ROOT = Path(__file__).resolve().parents[1]

# Make `uv run pytest` behave like `make test`: pick up .env without overriding real env vars.
load_dotenv(ROOT / ".env", override=False)


def _alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    # configparser treats % as interpolation; passwords may contain it.
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


@contextmanager
def _throwaway_database(admin_url: str, template: str | None = None) -> Iterator[str]:
    """Create a uniquely named database, yield its URL, drop it.

    It is migrated to head, or copied server-side from `template` (an already migrated
    database nobody is connected to), which is much faster than migrating again.
    """
    name = f"sharpline_test_{uuid.uuid4().hex[:8]}"
    admin = create_engine(normalize_database_url(admin_url), isolation_level="AUTOCOMMIT")
    clone = f' TEMPLATE "{template}"' if template else ""
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"{clone}'))

    url = (
        make_url(normalize_database_url(admin_url))
        .set(database=name)
        .render_as_string(hide_password=False)
    )
    try:
        if template is None:
            command.upgrade(_alembic_config(url), "head")
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _admin_url() -> str:
    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL not set")
    return admin_url


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    with _throwaway_database(_admin_url()) as url:
        yield url


@pytest.fixture(scope="session")
def migrated_template() -> Iterator[str]:
    """Name of a migrated database that is never connected to, cloned by `private_engine`."""
    with _throwaway_database(_admin_url()) as url:
        name = make_url(url).database
        assert name is not None
        yield name


@pytest.fixture
def private_engine(migrated_template: str) -> Iterator[Engine]:
    """A private migrated database for tests that must really commit (dropped afterwards),
    so their committed rows never reach the shared per-run test database."""
    with _throwaway_database(_admin_url(), template=migrated_template) as url:
        eng = create_engine(url)
        try:
            yield eng
        finally:
            eng.dispose()


@pytest.fixture(scope="session")
def nba_template(migrated_template: str) -> Iterator[str]:
    """Name of a migrated database with the NBA teams (and their xrefs) seeded, idle."""
    with _throwaway_database(_admin_url(), template=migrated_template) as url:
        eng = create_engine(url)
        try:
            with Session(eng) as session, session.begin():
                seed_nba_teams(session)
        finally:
            eng.dispose()  # a template must have no open connections when it is copied
        name = make_url(url).database
        assert name is not None
        yield name


@pytest.fixture
def db_session_factory(nba_template: str) -> Iterator[sessionmaker[Session]]:
    """Sessions for code that commits its own work (the capture runner), on a private copy
    of the NBA-seeded template that is dropped after the test: no table cleanup needed."""
    with _throwaway_database(_admin_url(), template=nba_template) as url:
        eng = create_engine(url)
        try:
            yield sessionmaker(bind=eng, expire_on_commit=False)
        finally:
            eng.dispose()


@pytest.fixture(scope="session")
def engine(pg_url: str) -> Iterator[Engine]:
    eng = create_engine(pg_url)
    yield eng
    eng.dispose()


@pytest.fixture
def db_session(engine: Engine) -> Iterator[Session]:
    conn = engine.connect()
    outer = conn.begin()
    session = Session(bind=conn, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        outer.rollback()
        conn.close()


@pytest.fixture
def seeded_source(db_session: Session) -> int:
    return record_source(
        db_session,
        provider="test",
        endpoint="/seed",
        params={},
        payload={"seed": True},
        fetched_at=datetime.now(UTC),
    )


@pytest.fixture
def seeded_market(db_session: Session) -> int:
    home = Team(sport="nba", name="Boston Celtics", abbreviation="BOS")
    away = Team(sport="nba", name="New York Knicks", abbreviation="NYK")
    db_session.add_all([home, away])
    db_session.flush()
    event = Event(
        sport="nba",
        season=2026,
        home_team_id=home.id,
        away_team_id=away.id,
        commence_time=datetime(2026, 11, 5, 0, 30, tzinfo=UTC),
    )
    db_session.add(event)
    db_session.flush()
    market = Market(
        event_id=event.id,
        market_type="total",
        period="FULL",
        provider_key="totals",
        line=Decimal("221.5"),
    )
    db_session.add(market)
    db_session.flush()
    return market.id


@pytest.fixture
def seeded_player(db_session: Session) -> int:
    player = Player(sport="nba", full_name="Jayson Tatum")
    db_session.add(player)
    db_session.flush()
    return player.id
