"""Timezone discipline: every timestamp inside Sharpline is aware and in UTC."""

from datetime import UTC, datetime


def ensure_utc(dt: datetime) -> datetime:
    """Return `dt` converted to UTC; reject naive datetimes outright.

    A naive datetime is ambiguous (ET? UTC? server-local?), and an ambiguous
    timestamp is exactly how a backtest leaks future information.
    """
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"naive datetime not allowed: {dt.isoformat()}")
    return dt.astimezone(UTC)
