"""Ingest jobs: one fetch -> record_fetch -> parse -> write, in one transaction.

The fetch happens before the transaction opens so no transaction is held across network
I/O. A job on a session with no open transaction commits on success; on a session that
is already inside a transaction it runs in a savepoint and the caller commits.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import date

from sqlalchemy.orm import Session

from sharpline.ingest.observations import record_fetch
from sharpline.ingest.resolve import EntityResolver
from sharpline.ingest.writer import Writer, WriteStats
from sharpline.sources.espn.client import EspnClient
from sharpline.sources.espn.parse import parse_injuries, parse_scoreboard
from sharpline.sources.fetched import Fetched
from sharpline.sources.nba_stats.client import NbaStatsClient
from sharpline.sources.nba_stats.parse import parse_player_game_logs
from sharpline.sources.odds_api.client import OddsApiClient
from sharpline.sources.odds_api.parse import parse_events, parse_odds
from sharpline.sports.nba.season import season_for

CLOSING = "closing"


@contextmanager
def _transaction(session: Session) -> Iterator[None]:
    if session.in_transaction():
        with session.begin_nested():
            yield
    else:
        with session.begin():
            yield


def _run(
    session: Session,
    fetched: Fetched,
    write: Callable[[Writer, int], WriteStats],
    *,
    purpose: str | None = None,
    capture_run_id: int | None = None,
) -> WriteStats:
    with _transaction(session):
        source_id = record_fetch(session, fetched, capture_run_id=capture_run_id, purpose=purpose)
        return write(Writer(session, EntityResolver(session)), source_id)


def ingest_schedule(
    session: Session, espn: EspnClient, slate_date: date, *, capture_run_id: int | None = None
) -> WriteStats:
    fetched = espn.scoreboard(slate_date)
    events, _ = parse_scoreboard(fetched.payload)
    return _run(
        session, fetched, lambda w, sid: w.events(events, sid), capture_run_id=capture_run_id
    )


def ingest_results(
    session: Session, espn: EspnClient, slate_date: date, *, capture_run_id: int | None = None
) -> WriteStats:
    fetched = espn.scoreboard(slate_date)
    events, results = parse_scoreboard(fetched.payload)
    return _run(
        session,
        fetched,
        lambda w, sid: w.events(events, sid) + w.results(results, sid),
        capture_run_id=capture_run_id,
    )


def ingest_injuries(
    session: Session, espn: EspnClient, *, capture_run_id: int | None = None
) -> WriteStats:
    fetched = espn.injuries()
    rows = parse_injuries(fetched.payload)
    return _run(
        session, fetched, lambda w, sid: w.injuries(rows, sid), capture_run_id=capture_run_id
    )


def _write_odds(
    session: Session, fetched: Fetched, is_closing: bool, run: int | None
) -> WriteStats:
    # Odds payloads carry their event's teams and tip, so upsert those events first: that is
    # what links an Odds API event id to our event (by xref, or teams + tip within 2 h).
    payload = fetched.payload
    events = parse_events(payload if isinstance(payload, list) else [payload])
    rows = parse_odds(payload, fetched.fetched_at)
    return _run(
        session,
        fetched,
        lambda w, sid: w.events(events, sid) + w.odds(rows, sid, is_closing=is_closing),
        purpose=CLOSING if is_closing else None,
        capture_run_id=run,
    )


def ingest_game_lines(
    session: Session,
    odds: OddsApiClient,
    *,
    is_closing: bool = False,
    capture_run_id: int | None = None,
) -> WriteStats:
    return _write_odds(session, odds.game_odds(), is_closing, capture_run_id)


def ingest_props(
    session: Session,
    odds: OddsApiClient,
    event_id: str,
    markets: Sequence[str],
    *,
    is_closing: bool = False,
    capture_run_id: int | None = None,
) -> WriteStats:
    return _write_odds(session, odds.event_odds(event_id, markets), is_closing, capture_run_id)


def ingest_player_stats(
    session: Session,
    nba: NbaStatsClient,
    slate_date: date,
    *,
    season: str | None = None,
    capture_run_id: int | None = None,
) -> WriteStats:
    """Box scores for `slate_date`; the season defaults to the one containing it (R5)."""
    fetched = nba.player_game_logs(season or season_for(slate_date), slate_date, slate_date)
    rows = parse_player_game_logs(fetched.payload)
    return _run(
        session, fetched, lambda w, sid: w.player_stats(rows, sid), capture_run_id=capture_run_id
    )
