"""NBA season labels derived from a date: seasons start in October."""

from datetime import date

_SEASON_START_MONTH = 10


def season_start_year(d: date) -> int:
    """Calendar year the season containing `d` started in (Oct-Dec: this year, else last)."""
    return d.year if d.month >= _SEASON_START_MONTH else d.year - 1


def season_for(d: date) -> str:
    """nba_api season label, e.g. 2026-11-05 -> "2026-27", 2027-03-01 -> "2026-27"."""
    start = season_start_year(d)
    return f"{start}-{(start + 1) % 100:02d}"
