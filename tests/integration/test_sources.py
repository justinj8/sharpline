from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from sharpline.db.models import Source
from sharpline.db.sources import record_source

pytestmark = pytest.mark.integration


def test_record_source_dedupes_identical_payload(db_session: Session) -> None:
    kw: dict[str, Any] = {
        "provider": "odds_api",
        "endpoint": "/v4/sports/basketball_nba/odds",
        "params": {"markets": "h2h"},
        "payload": {"x": 1},
        "fetched_at": datetime.now(UTC),
    }
    assert record_source(db_session, **kw) == record_source(db_session, **kw)
    assert db_session.scalar(select(func.count()).select_from(Source)) == 1


def test_record_source_new_row_for_different_params(db_session: Session) -> None:
    base: dict[str, Any] = {
        "provider": "odds_api",
        "endpoint": "/e",
        "payload": {"x": 1},
        "fetched_at": datetime.now(UTC),
    }
    a = record_source(db_session, params={"markets": "h2h"}, **base)
    b = record_source(db_session, params={"markets": "totals"}, **base)
    assert a != b


def test_record_source_stores_hashes_and_payload(db_session: Session) -> None:
    sid = record_source(
        db_session,
        provider="espn",
        endpoint="/injuries",
        params={"team": "BOS"},
        payload={"status": "questionable"},
        fetched_at=datetime(2026, 11, 4, 17, 0, tzinfo=UTC),
    )
    row = db_session.get(Source, sid)
    assert row is not None
    assert row.payload == {"status": "questionable"}
    assert len(row.content_hash) == 64
    assert row.fetched_at == datetime(2026, 11, 4, 17, 0, tzinfo=UTC)


def test_sources_are_immutable(db_session: Session) -> None:
    sid = record_source(
        db_session,
        provider="espn",
        endpoint="/e",
        params={},
        payload={"y": 1},
        fetched_at=datetime.now(UTC),
    )
    with pytest.raises(DBAPIError, match="immutable"):
        db_session.execute(text("update sources set provider='x' where id=:i"), {"i": sid})


def test_sources_reject_delete(db_session: Session) -> None:
    sid = record_source(
        db_session,
        provider="espn",
        endpoint="/e",
        params={},
        payload={"y": 2},
        fetched_at=datetime.now(UTC),
    )
    with pytest.raises(DBAPIError, match="immutable"):
        db_session.execute(text("delete from sources where id=:i"), {"i": sid})


def test_record_source_rejects_naive_datetime(db_session: Session) -> None:
    with pytest.raises(ValueError):
        record_source(
            db_session,
            provider="espn",
            endpoint="/e",
            params={},
            payload={},
            fetched_at=datetime(2026, 11, 4),
        )
