from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sharpline.db.models import QuotaLedger, Source, SourceObservation
from sharpline.ingest.observations import latest_source, record_fetch
from sharpline.sources.fetched import Fetched, QuotaInfo

pytestmark = pytest.mark.integration

T1 = datetime(2026, 11, 4, 18, 0, tzinfo=UTC)
ENDPOINT = "sports/basketball_nba/odds"
PARAMS = {"regions": "us", "oddsFormat": "american", "markets": "h2h,spreads,totals"}


def _fetched(payload: object, at: datetime, quota: QuotaInfo | None = None) -> Fetched:
    return Fetched(
        provider="odds_api",
        endpoint=ENDPOINT,
        params=dict(PARAMS),
        payload=payload,
        fetched_at=at,
        quota=quota,
    )


def _count(session: Session, model: type) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def test_record_fetch_writes_source_observation_and_quota(db_session: Session) -> None:
    quota = QuotaInfo(last=3, used=120, remaining=380)
    source_id = record_fetch(db_session, _fetched([{"id": "evt1"}], T1, quota))

    source = db_session.get(Source, source_id)
    assert source is not None and source.provider == "odds_api" and source.fetched_at == T1
    obs = db_session.scalars(select(SourceObservation)).all()
    assert [(o.source_id, o.observed_at, o.purpose) for o in obs] == [(source_id, T1, None)]
    ledger = db_session.scalars(select(QuotaLedger)).all()
    assert len(ledger) == 1
    row = ledger[0]
    assert (row.provider, row.cost, row.used, row.remaining) == ("odds_api", 3, 120, 380)
    assert row.observed_at == T1 and row.source_id == source_id and row.capture_run_id is None


def test_record_fetch_without_quota_writes_no_ledger_and_keeps_purpose(
    db_session: Session,
) -> None:
    record_fetch(db_session, _fetched([], T1), purpose="closing")
    assert _count(db_session, QuotaLedger) == 0
    assert db_session.scalars(select(SourceObservation.purpose)).all() == ["closing"]


def test_refetch_same_payload_adds_observation_not_source(db_session: Session) -> None:
    first = record_fetch(db_session, _fetched([{"id": "evt1"}], T1))
    second = record_fetch(db_session, _fetched([{"id": "evt1"}], T1 + timedelta(minutes=5)))
    assert first == second
    assert _count(db_session, Source) == 1
    assert _count(db_session, SourceObservation) == 2


def test_latest_source_handles_a_b_a(db_session: Session) -> None:
    t2, t3 = T1 + timedelta(hours=1), T1 + timedelta(hours=2)
    a = record_fetch(db_session, _fetched({"v": "A"}, T1))
    b = record_fetch(db_session, _fetched({"v": "B"}, t2))
    assert record_fetch(db_session, _fetched({"v": "A"}, t3)) == a

    def latest(as_of: datetime) -> int | None:
        src = latest_source(db_session, "odds_api", ENDPOINT, PARAMS, as_of)
        return None if src is None else src.id

    assert latest(T1 - timedelta(seconds=1)) is None
    assert latest(T1) == a
    assert latest(t2) == b
    assert latest(t2 + timedelta(minutes=30)) == b
    assert latest(t3) == a
    # different params never match
    assert latest_source(db_session, "odds_api", ENDPOINT, {"markets": "h2h"}, t3) is None
