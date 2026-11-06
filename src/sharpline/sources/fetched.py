"""Envelopes returned by every provider client: the payload plus its provenance."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class QuotaInfo:
    """Provider quota headers (Odds API: x-requests-last/used/remaining)."""

    last: int | None
    used: int | None
    remaining: int | None


@dataclass(frozen=True)
class Fetched:
    """A raw provider response with the request that produced it."""

    provider: str
    endpoint: str
    params: dict[str, Any]
    payload: Any
    fetched_at: datetime
    quota: QuotaInfo | None = None
