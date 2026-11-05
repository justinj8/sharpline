"""Pure ESPN parsers: raw payload dict -> normalized rows. No I/O, no client import."""

import logging
from datetime import datetime
from typing import Any

from pydantic import ValidationError

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


_SKIPPABLE = (KeyError, IndexError, TypeError, ValueError, AttributeError, ValidationError)


def _parse_event(ev: dict[str, Any]) -> tuple[EventRow, ResultRow | None] | None:
    competitors = ev["competitions"][0]["competitors"]
    home = _competitor(competitors, "home")
    away = _competitor(competitors, "away")
    if home is None or away is None:
        log.warning("espn event %s missing home/away competitor; skipped", ev.get("id"))
        return None
    event_id = str(ev["id"])
    event = EventRow(
        provider=PROVIDER,
        provider_event_id=event_id,
        sport=Sport.NBA,
        home_team=home["team"]["abbreviation"],
        away_team=away["team"]["abbreviation"],
        commence_time=_dt(ev["date"]),
    )
    result: ResultRow | None = None
    if ev["status"]["type"]["name"] == FINAL:
        result = ResultRow(
            provider_event_id=event_id,
            home_score=int(home["score"]),
            away_score=int(away["score"]),
            # ESPN exposes no finish timestamp and the tip time would leak the result
            # ~2.5h early to as-of logic, so leave it unset: the writer stamps it from
            # the observing source's fetched_at.
            final_at=None,
        )
    return event, result


def parse_scoreboard(payload: dict[str, Any]) -> tuple[list[EventRow], list[ResultRow]]:
    events: list[EventRow] = []
    results: list[ResultRow] = []
    for ev in payload.get("events", []):
        try:
            parsed = _parse_event(ev)
        except _SKIPPABLE as exc:
            ident = ev.get("id", "unknown") if isinstance(ev, dict) else "unknown"
            log.warning("espn event %s malformed (%s); skipped", ident, type(exc).__name__)
            continue
        if parsed is None:
            continue
        events.append(parsed[0])
        if parsed[1] is not None:
            results.append(parsed[1])
    return events, results


def _parse_injury(team_id: str, entry: dict[str, Any]) -> InjuryRow | None:
    raw_status = str(entry.get("status", ""))
    status = _STATUS_MAP.get(raw_status.strip().lower())
    name = entry["athlete"]["displayName"]
    if status is None:
        log.warning(
            "unknown espn injury status %r for %s (team %s); dropped", raw_status, name, team_id
        )
        return None
    date_raw = entry.get("date")
    return InjuryRow(
        team_provider_id=team_id,
        player_name=name,
        status=status,
        detail=entry.get("shortComment") or entry.get("longComment") or None,
        reported_at=_dt(date_raw) if date_raw else None,
    )


def parse_injuries(payload: dict[str, Any]) -> list[InjuryRow]:
    rows: list[InjuryRow] = []
    for team in payload.get("injuries", []):
        try:
            team_id = str(team["id"])
            entries = team.get("injuries", [])
            if not isinstance(entries, list):
                raise TypeError("injuries is not a list")
        except _SKIPPABLE as exc:
            ident = team.get("id", "unknown") if isinstance(team, dict) else "unknown"
            log.warning("espn injury team %s malformed (%s); skipped", ident, type(exc).__name__)
            continue
        for entry in entries:
            try:
                row = _parse_injury(team_id, entry)
            except _SKIPPABLE as exc:
                ident = entry.get("id", "unknown") if isinstance(entry, dict) else "unknown"
                log.warning(
                    "espn injury entry %s (team %s) malformed (%s); skipped",
                    ident,
                    team_id,
                    type(exc).__name__,
                )
                continue
            if row is not None:
                rows.append(row)
    return rows
