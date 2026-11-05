from datetime import date

import pytest

from sharpline.sports.nba.season import season_for, season_start_year


@pytest.mark.parametrize(
    ("d", "season"),
    [
        (date(2026, 11, 5), "2026-27"),
        (date(2026, 10, 1), "2026-27"),
        (date(2026, 12, 31), "2026-27"),
        (date(2027, 1, 1), "2026-27"),
        (date(2027, 6, 20), "2026-27"),
        (date(2026, 9, 30), "2025-26"),
        (date(2025, 10, 28), "2025-26"),
        (date(1999, 11, 1), "1999-00"),
        (date(2000, 2, 1), "1999-00"),
    ],
)
def test_season_for(d: date, season: str) -> None:
    assert season_for(d) == season


def test_season_start_year() -> None:
    assert season_start_year(date(2026, 11, 5)) == 2026
    assert season_start_year(date(2027, 3, 1)) == 2026
    assert season_start_year(date(2026, 9, 30)) == 2025
