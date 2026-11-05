"""Fetch recording and as-of lookups over `source_observations`.

`sources` dedupes identical payloads, so "when did we see this?" lives in
`source_observations`: one row per fetch, even when the payload was unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from sharpline.db.models import QuotaLedger, Source, SourceObservation
from sharpline.db.sources import canonical_hash, record_source
from sharpline.domain.time import ensure_utc
from sharpline.sources.fetched import Fetched


def record_fetch(
    session: Session,
    fetched: Fetched,
    *,
    capture_run_id: int | None = None,
    purpose: str | None = None,
) -> int:
    """Store the payload (deduped), log the observation and any quota usage; return source_id."""
    observed_at = ensure_utc(fetched.fetched_at)
    source_id = record_source(
        session,
        provider=fetched.provider,
        endpoint=fetched.endpoint,
        params=fetched.params,
        payload=fetched.payload,
        fetched_at=observed_at,
    )
    session.add(SourceObservation(source_id=source_id, observed_at=observed_at, purpose=purpose))
    if fetched.quota is not None:
        session.add(
            QuotaLedger(
                provider=fetched.provider,
                cost=fetched.quota.last or 0,
                used=fetched.quota.used,
                remaining=fetched.quota.remaining,
                observed_at=observed_at,
                source_id=source_id,
                capture_run_id=capture_run_id,
            )
        )
    session.flush()
    return source_id


def latest_source(
    session: Session,
    provider: str,
    endpoint: str,
    params: Mapping[str, Any],
    as_of: datetime,
) -> Source | None:
    """The payload most recently observed at or before `as_of` for this request."""
    return session.scalars(
        select(Source)
        .join(SourceObservation, SourceObservation.source_id == Source.id)
        .where(
            Source.provider == provider,
            Source.endpoint == endpoint,
            Source.params_hash == canonical_hash(dict(params)),
            SourceObservation.observed_at <= ensure_utc(as_of),
        )
        .order_by(SourceObservation.observed_at.desc(), SourceObservation.id.desc())
        .limit(1)
    ).first()
