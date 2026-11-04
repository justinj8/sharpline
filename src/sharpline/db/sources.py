"""Provenance: record every fetched payload exactly once.

`record_source` is the only write path into `sources`. Identical refetches (same
provider, endpoint, params and payload) collapse onto the existing row, so polling an
unchanged endpoint every few minutes does not grow the table.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from sharpline.db.models import Source
from sharpline.domain.time import ensure_utc


def canonical_hash(obj: Any) -> str:
    """sha256 of a key-order-independent JSON encoding."""
    encoded = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def record_source(
    session: Session,
    *,
    provider: str,
    endpoint: str,
    params: Mapping[str, Any],
    payload: Any,
    fetched_at: datetime,
) -> int:
    """Insert a source row (or find the identical existing one) and return its id."""
    fetched_at = ensure_utc(fetched_at)
    params_dict = dict(params)
    params_hash = canonical_hash(params_dict)
    content_hash = canonical_hash(payload)

    inserted = session.execute(
        insert(Source)
        .values(
            provider=provider,
            endpoint=endpoint,
            params=params_dict,
            params_hash=params_hash,
            content_hash=content_hash,
            payload=payload,
            fetched_at=fetched_at,
        )
        .on_conflict_do_nothing(
            index_elements=["provider", "endpoint", "params_hash", "content_hash"]
        )
        .returning(Source.id)
    ).scalar_one_or_none()
    if inserted is not None:
        return inserted

    existing = session.execute(
        select(Source.id).where(
            Source.provider == provider,
            Source.endpoint == endpoint,
            Source.params_hash == params_hash,
            Source.content_hash == content_hash,
        )
    ).scalar_one()
    return existing
