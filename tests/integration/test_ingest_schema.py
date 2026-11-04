from datetime import UTC, datetime

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from sharpline.db.models import Injury, SourceObservation

pytestmark = pytest.mark.integration


def test_ingest_tables_exist(db_session: Session) -> None:
    tables = set(inspect(db_session.get_bind()).get_table_names())
    assert {
        "source_observations",
        "injuries",
        "results",
        "player_game_stats",
        "entity_review",
        "quota_ledger",
        "capture_runs",
    } <= tables


def test_source_observations_are_immutable(db_session: Session, seeded_source: int) -> None:
    db_session.add(SourceObservation(source_id=seeded_source, observed_at=datetime.now(UTC)))
    db_session.flush()
    with pytest.raises(DBAPIError, match="immutable"):
        db_session.execute(text("delete from source_observations"))


def test_injury_status_check(db_session: Session, seeded_source: int, seeded_player: int) -> None:
    db_session.add(
        Injury(
            player_id=seeded_player,
            status="day-to-day",
            reported_at=datetime.now(UTC),
            source_id=seeded_source,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
