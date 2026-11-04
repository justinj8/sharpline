from datetime import UTC, datetime

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sharpline.db.base import Base
from sharpline.db.models import Market, OddsSnapshot

pytestmark = pytest.mark.integration

CORE_TABLES = {
    "sources",
    "teams",
    "players",
    "entity_xref",
    "events",
    "markets",
    "odds_snapshots",
}


def test_all_core_tables_exist(db_session: Session) -> None:
    names = set(inspect(db_session.get_bind()).get_table_names())
    assert names >= CORE_TABLES


def test_vector_extension_installed(db_session: Session) -> None:
    installed = db_session.execute(text("select 1 from pg_extension where extname='vector'"))
    assert installed.scalar() == 1


def test_models_match_migrations(db_session: Session) -> None:
    """The hand-written migration and the ORM models must describe the same schema."""
    ctx = MigrationContext.configure(db_session.connection())
    diffs = [d for d in compare_metadata(ctx, Base.metadata) if d[0] != "remove_table"]
    assert diffs == []


def test_odds_price_check_rejects_invalid(
    db_session: Session, seeded_market: int, seeded_source: int
) -> None:
    db_session.add(
        OddsSnapshot(
            market_id=seeded_market,
            book="draftkings",
            side="OVER",
            price=50,
            captured_at=datetime.now(UTC),
            source_id=seeded_source,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_market_uniqueness_treats_null_player_as_equal(
    db_session: Session, seeded_market: int
) -> None:
    original = db_session.get(Market, seeded_market)
    assert original is not None
    db_session.add(
        Market(
            event_id=original.event_id,
            market_type="total",
            period="FULL",
            provider_key="totals",
            line=original.line,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
