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
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from sharpline.ingest.observations import record_fetch
from sharpline.ingest.resolve import EntityResolver
from sharpline.ingest.writer import Writer, WriteStats
from sharpline.sources.espn.client import EspnClient
from sharpline.sources.espn.parse import parse_injuries, parse_scoreboard
from sharpline.sources.fetched import Fetched
from sharpline.sources.nba_stats.client import NbaStatsClient
from sharpline.sources.nba_stats.parse import parse_player_game_logs
from sharpline.sources.odds_api.client import OddsFeed
from sharpline.sources.odds_api.parse import parse_events, parse_odds
from sharpline.sources.rows import EventRow
from sharpline.sports.nba.season import season_for

CLOSING = "closing"

# Writes the non-event rows of one payload: (writer, payload, source_id, observed_at,
# is_closing). `observed_at` is when that payload was fetched (odds captured_at).
RowWriter = Callable[[Writer, Any, int, datetime, bool], WriteStats]


@dataclass(frozen=True)
class Parser:
    """How one provider endpoint's payload becomes typed rows: pure parser + writer method.

    Shared by the jobs (parse on write) and `reparse` (rebuild from stored sources), so the
    two paths cannot drift. Events are a separate step because reparse writes every
    source's events first, in fetch order (R9), before any rows that hang off them.
    """

    events: Callable[[Any], list[EventRow]] | None = None
    rows: RowWriter | None = None

    def write(
        self,
        w: Writer,
        payload: Any,
        source_id: int,
        observed_at: datetime,
        *,
        is_closing: bool = False,
    ) -> WriteStats:
        stats = WriteStats()
        if self.events is not None:
            stats += w.events(self.events(payload), source_id)
        if self.rows is not None:
            stats += self.rows(w, payload, source_id, observed_at, is_closing)
        return stats


def scoreboard_events(payload: Any) -> list[EventRow]:
    return parse_scoreboard(payload)[0]


def odds_events(payload: Any) -> list[EventRow]:
    # Odds payloads carry their event's teams and tip, so upserting those events first links
    # an Odds API event id to our event (by xref, or teams + tip within 2 h).
    return parse_events(payload if isinstance(payload, list) else [payload])


def write_results(w: Writer, payload: Any, sid: int, _at: datetime, _closing: bool) -> WriteStats:
    return w.results(parse_scoreboard(payload)[1], sid)


def write_injuries(w: Writer, payload: Any, sid: int, _at: datetime, _closing: bool) -> WriteStats:
    return w.injuries(parse_injuries(payload), sid)


def write_odds(w: Writer, payload: Any, sid: int, at: datetime, closing: bool) -> WriteStats:
    return w.odds(parse_odds(payload, at), sid, is_closing=closing)


def write_player_stats(
    w: Writer, payload: Any, sid: int, _at: datetime, _closing: bool
) -> WriteStats:
    return w.player_stats(parse_player_game_logs(payload), sid)


RESULTS = Parser(events=scoreboard_events, rows=write_results)
INJURIES = Parser(rows=write_injuries)
ODDS_EVENTS = Parser(events=odds_events)
ODDS = Parser(events=odds_events, rows=write_odds)
PLAYER_STATS = Parser(rows=write_player_stats)


@contextmanager
def transaction(session: Session) -> Iterator[None]:
    """One unit of work: its own committed transaction on an idle session, else a savepoint."""
    if session.in_transaction():
        with session.begin_nested():
            yield
    else:
        with session.begin():
            yield


def _run(
    session: Session,
    fetched: Fetched,
    parser: Parser,
    *,
    is_closing: bool = False,
    capture_run_id: int | None = None,
) -> WriteStats:
    purpose = CLOSING if is_closing else None
    with transaction(session):
        source_id = record_fetch(session, fetched, capture_run_id=capture_run_id, purpose=purpose)
    with transaction(session):
        writer = Writer(session, EntityResolver(session))
        return parser.write(
            writer, fetched.payload, source_id, fetched.fetched_at, is_closing=is_closing
        )


def ingest_schedule(
    session: Session, espn: EspnClient, slate_date: date, *, capture_run_id: int | None = None
) -> WriteStats:
    # A refresh that already sees a final records it like the results job (R4), so the live
    # path and reparse (every scoreboard is RESULTS) stamp final_at the same way.
    return _run(session, espn.scoreboard(slate_date), RESULTS, capture_run_id=capture_run_id)


def ingest_results(
    session: Session, espn: EspnClient, slate_date: date, *, capture_run_id: int | None = None
) -> WriteStats:
    return _run(session, espn.scoreboard(slate_date), RESULTS, capture_run_id=capture_run_id)


def ingest_injuries(
    session: Session, espn: EspnClient, *, capture_run_id: int | None = None
) -> WriteStats:
    return _run(session, espn.injuries(), INJURIES, capture_run_id=capture_run_id)


def ingest_odds_events(
    session: Session, odds: OddsFeed, *, capture_run_id: int | None = None
) -> WriteStats:
    """The Odds API event list (0 credits): links its event ids to our events (R2)."""
    return _run(session, odds.events(), ODDS_EVENTS, capture_run_id=capture_run_id)


def ingest_game_lines(
    session: Session,
    odds: OddsFeed,
    *,
    event_ids: Sequence[str] | None = None,
    is_closing: bool = False,
    capture_run_id: int | None = None,
) -> WriteStats:
    """Game lines for every upcoming event, or only `event_ids` (Odds API ids)."""
    fetched = odds.game_odds(event_ids)
    return _run(session, fetched, ODDS, is_closing=is_closing, capture_run_id=capture_run_id)


def ingest_props(
    session: Session,
    odds: OddsFeed,
    event_id: str,
    markets: Sequence[str],
    *,
    is_closing: bool = False,
    capture_run_id: int | None = None,
) -> WriteStats:
    fetched = odds.event_odds(event_id, markets)
    return _run(session, fetched, ODDS, is_closing=is_closing, capture_run_id=capture_run_id)


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
    return _run(session, fetched, PLAYER_STATS, capture_run_id=capture_run_id)


def ingest_season_player_stats(
    session: Session, nba: NbaStatsClient, season: str, *, capture_run_id: int | None = None
) -> WriteStats:
    """A whole season's box scores (backfill). Games map to stored events only (R1): rows
    for games with no stored event are skipped, and a later reparse can attach them."""
    fetched = nba.player_game_logs(season)
    return _run(session, fetched, PLAYER_STATS, capture_run_id=capture_run_id)
