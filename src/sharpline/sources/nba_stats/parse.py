"""Pure nba_api parsers: LeagueGameLog payload -> PlayerGameStatRow. No I/O, no client import."""

import contextlib
import logging
from datetime import date
from typing import Any

from pydantic import ValidationError

from sharpline.sources.rows import PlayerGameStatRow

log = logging.getLogger(__name__)

_SKIPPABLE = (KeyError, IndexError, TypeError, ValueError, AttributeError, ValidationError)

_STAT_HEADERS = {
    "points": "PTS",
    "rebounds": "REB",
    "assists": "AST",
    "threes": "FG3M",
    "blocks": "BLK",
    "steals": "STL",
    "turnovers": "TOV",
}


def _minutes(raw: Any) -> float:
    """MIN is an int/float in some seasons and "mm:ss" in others."""
    if isinstance(raw, str) and ":" in raw:
        mins, secs = raw.split(":", 1)
        return int(mins) + int(secs) / 60
    return float(raw)


def _parse_row(headers: list[str], values: list[Any]) -> PlayerGameStatRow:
    if len(values) != len(headers):
        raise IndexError(f"row has {len(values)} values for {len(headers)} headers")
    row = dict(zip(headers, values, strict=True))
    stats = {name: float(row[header]) for name, header in _STAT_HEADERS.items()}
    stats["minutes"] = _minutes(row["MIN"])
    stats["points_rebounds_assists"] = stats["points"] + stats["rebounds"] + stats["assists"]
    return PlayerGameStatRow(
        provider_game_id=str(row["GAME_ID"]),
        provider_player_id=str(row["PLAYER_ID"]),
        player_name=row["PLAYER_NAME"],
        team_abbr=row["TEAM_ABBREVIATION"],
        game_date=date.fromisoformat(row["GAME_DATE"]),
        matchup=row["MATCHUP"],
        stats=stats,
    )


def parse_player_game_logs(payload: dict[str, Any]) -> list[PlayerGameStatRow]:
    result_sets = payload.get("resultSets") or []
    if not result_sets:
        return []
    result_set = result_sets[0]
    headers: list[str] = result_set["headers"]
    rows: list[PlayerGameStatRow] = []
    for values in result_set["rowSet"]:
        try:
            rows.append(_parse_row(headers, values))
        except _SKIPPABLE as exc:
            ident = "unknown"
            with contextlib.suppress(KeyError, IndexError, ValueError):
                ident = f"{values[headers.index('PLAYER_ID')]}/{values[headers.index('GAME_ID')]}"
            log.warning("nba_stats row %s malformed (%s); skipped", ident, type(exc).__name__)
    return rows
