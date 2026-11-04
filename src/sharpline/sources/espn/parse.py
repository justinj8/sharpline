"""Pure ESPN parsers: raw payload dict -> normalized rows. No I/O, no client import."""

import logging
from datetime import datetime
from typing import Any

from sharpline.domain.enums import Sport
from sharpline.sources.espn import PROVIDER
from sharpline.sources.rows import EventRow, InjuryRow, ResultRow

log = logging.getLogger(__name__)

FINAL = "STATUS_FINAL"

_STATUS_MAP = {
    "out": "out",
    "doubtful": "doubtful",
    "questionable": "questionable",
    "probable": "probable",
    "day-to-day": "questionable",
    "active": "active",
}


def _dt(raw: str) -> datetime:
    # ESPN emits "2026-11-05T00:00Z" (no seconds); aware UTC.
    return datetime.fromisoformat(raw)


def _competitor(competitors: list[dict[str, Any]], side: str) -> dict[str, Any] | None:
    return next((c for c in competitors if c.get("homeAway") == side), None)


def parse_scoreboard(payload: dict[str, Any]) -> tuple[list[EventRow], list[ResultRow]]:
    events: list[EventRow] = []
    results: list[ResultRow] = []
    for ev in payload.get("events", []):
        competitors = ev["competitions"][0]["competitors"]
        home = _competitor(competitors, "home")
        away = _competitor(competitors, "away")
        if home is None or away is None:
            log.warning("espn event %s missing home/away competitor; skipped", ev.get("id"))
            continue
        tip = _dt(ev["date"])
        event_id = str(ev["id"])
        events.append(
            EventRow(
                provider=PROVIDER,
                provider_event_id=event_id,
                sport=Sport.NBA,
                home_team=home["team"]["abbreviation"],
                away_team=away["team"]["abbreviation"],
                commence_time=tip,
            )
        )
        if ev["status"]["type"]["name"] == FINAL:
            results.append(
                ResultRow(
                    provider_event_id=event_id,
                    home_score=int(home["score"]),
                    away_score=int(away["score"]),
                    # ESPN exposes no finish timestamp; the scheduled tip time is used.
                    final_at=tip,
                )
            )
    return events, results


def parse_injuries(payload: dict[str, Any]) -> list[InjuryRow]:
    rows: list[InjuryRow] = []
    for team in payload.get("injuries", []):
        team_id = str(team["id"])
        for entry in team.get("injuries", []):
            raw_status = str(entry.get("status", ""))
            status = _STATUS_MAP.get(raw_status.strip().lower())
            name = entry["athlete"]["displayName"]
            if status is None:
                log.warning(
                    "unknown espn injury status %r for %s (team %s); dropped",
                    raw_status,
                    name,
                    team_id,
                )
                continue
            date_raw = entry.get("date")
            rows.append(
                InjuryRow(
                    team_provider_id=team_id,
                    player_name=name,
                    status=status,
                    detail=entry.get("shortComment") or entry.get("longComment") or None,
                    reported_at=_dt(date_raw) if date_raw else None,
                )
            )
    return rows
