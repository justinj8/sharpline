"""Ingest jobs: fetch -> record_fetch (unit 1) -> parse + write (unit 2).

The fetch is recorded (source, observation, quota row) in its own unit before parsing,
so a parser or writer failure leaves the raw payload in place for a later reparse
(spec section 7).

Error contract (all jobs): exceptions propagate. A fetch error records nothing. A
parse/write error rolls back only unit 2 (that job's typed rows).

Transaction modes, decided by the session the caller passes in:
- Idle session (production: nothing open): each unit is its own transaction and commits
  on exit. The fetch runs before either unit opens, so no transaction is held across
  network I/O, and the recorded fetch is durable even when unit 2 fails.
- Session already inside a transaction: each unit is a savepoint in the caller's
  transaction, which stays open across the fetch. Nothing is durable until the caller
  commits. The caller must commit (not roll back) after a failed job to keep the
  recorded fetch, since rolling back the outer transaction discards unit 1 too.
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
    with _transaction(session):
        return write(Writer(session, EntityResolver(session)), source_id)


def ingest_schedule(
    session: Session, espn: EspnClient, slate_date: date, *, capture_run_id: int | None = None
) -> WriteStats:
    fetched = espn.scoreboard(slate_date)

    def write(w: Writer, sid: int) -> WriteStats:
        events, _ = parse_scoreboard(fetched.payload)
        return w.events(events, sid)

    return _run(session, fetched, write, capture_run_id=capture_run_id)


def ingest_results(
    session: Session, espn: EspnClient, slate_date: date, *, capture_run_id: int | None = None
) -> WriteStats:
    fetched = espn.scoreboard(slate_date)

    def write(w: Writer, sid: int) -> WriteStats:
        events, results = parse_scoreboard(fetched.payload)
        return w.events(events, sid) + w.results(results, sid)

    return _run(session, fetched, write, capture_run_id=capture_run_id)


def ingest_injuries(
    session: Session, espn: EspnClient, *, capture_run_id: int | None = None
) -> WriteStats:
    fetched = espn.injuries()

    def write(w: Writer, sid: int) -> WriteStats:
        return w.injuries(parse_injuries(fetched.payload), sid)

    return _run(session, fetched, write, capture_run_id=capture_run_id)


def _write_odds(
    session: Session, fetched: Fetched, is_closing: bool, run: int | None
) -> WriteStats:
    def write(w: Writer, sid: int) -> WriteStats:
        # Odds payloads carry their event's teams and tip, so upsert those events first:
        # that links an Odds API event id to our event (by xref, or teams + tip within 2 h).
        payload = fetched.payload
        events = parse_events(payload if isinstance(payload, list) else [payload])
        rows = parse_odds(payload, fetched.fetched_at)
        return w.events(events, sid) + w.odds(rows, sid, is_closing=is_closing)

    return _run(
        session, fetched, write, purpose=CLOSING if is_closing else None, capture_run_id=run
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

    def write(w: Writer, sid: int) -> WriteStats:
        return w.player_stats(parse_player_game_logs(fetched.payload), sid)

    return _run(session, fetched, write, capture_run_id=capture_run_id)
