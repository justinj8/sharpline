from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from sharpline.domain.time import ensure_utc


def test_ensure_utc_rejects_naive() -> None:
    with pytest.raises(ValueError):
        ensure_utc(datetime(2026, 11, 4, 12, 0))


def test_ensure_utc_converts_eastern() -> None:
    et = datetime(2026, 11, 4, 14, 0, tzinfo=ZoneInfo("America/New_York"))
    assert ensure_utc(et) == datetime(2026, 11, 4, 19, 0, tzinfo=UTC)
    assert ensure_utc(et).tzinfo is UTC
