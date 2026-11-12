"""Idempotent writes of normalized provider rows into the typed tables.

Every method takes the `source_id` the rows were parsed from. Rows whose entities do not
resolve are skipped (the resolver queues them for review); nothing here commits.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from sharpline.db.models import (
    Event,
    Injury,
    Market,
    OddsSnapshot,
    Player,
    PlayerGameStat,
    Result,
    Source,
    Team,
)
from sharpline.domain.enums import EventStatus, MarketType, Side, Sport
from sharpline.ingest.resolve import EntityResolver, NeedsReview, Resolution, Resolved
from sharpline.sources.rows import EventRow, InjuryRow, OddsRow, PlayerGameStatRow, ResultRow
from sharpline.sports.nba.season import season_start_year

EASTERN = ZoneInfo("America/New_York")
EVENT_MATCH_WINDOW = timedelta(hours=2)
TIP_MOVE_THRESHOLD = timedelta(minutes=10)


@dataclass(frozen=True)
class WriteStats:
    """`skipped`: rows not written; `reviews`: those skipped for an unresolved entity."""

    written: int = 0
    skipped: int = 0
    reviews: int = 0

    def __add__(self, other: WriteStats) -> WriteStats:
        return WriteStats(
            self.written + other.written,
            self.skipped + other.skipped,
            self.reviews + other.reviews,
        )


@dataclass
class _Tally:
    written: int = 0
    skipped: int = 0
    reviews: int = 0

    def review(self) -> None:
        self.skipped += 1
        self.reviews += 1

    def stats(self) -> WriteStats:
        return WriteStats(self.written, self.skipped, self.reviews)


def _market_line(row: OddsRow) -> Decimal | None:
    """Spreads are keyed by the home line so both sides share one market (R8)."""
    if row.market_type == MarketType.MONEYLINE or row.line is None:
        return None
    if row.market_type == MarketType.SPREAD and row.side == Side.AWAY:
        return -row.line
    return row.line


def _next_status(stored: str, reported: EventStatus | None) -> str:
    """The provider's status, except that a final is never downgraded and a game under way
    never goes back to scheduled (a stale pre-game payload); None leaves it alone."""
    if reported is None or stored == EventStatus.FINAL:
        return stored
    if stored == EventStatus.IN_PROGRESS and reported == EventStatus.SCHEDULED:
        return stored
    return reported.value


def _moves_tip(stored: datetime, reported: datetime) -> bool:
    """R9 (revised): an earlier tip is always taken, so a closing planned from the stored tip
    never lands after the real one; a later tip only on a real reschedule (> 10 min), since
    providers disagree by minutes. Depends only on the rows' order, so reparse replays it."""
    return reported < stored or reported - stored > TIP_MOVE_THRESHOLD


class Writer:
    def __init__(self, session: Session, resolver: EntityResolver) -> None:
        self._s = session
        self._resolver = resolver
        self._markets: dict[tuple[int, str, str, int | None, Decimal | None], int] = {}

    # -- events ---------------------------------------------------------------------------

    def events(self, rows: Iterable[EventRow], source_id: int) -> WriteStats:
        """Upsert events; the same game from two providers converges on one event."""
        tally = _Tally()
        for row in rows:
            home = self._resolver.team(row.provider, row.home_team)
            away = self._resolver.team(row.provider, row.away_team)
            if not (isinstance(home, Resolved) and isinstance(away, Resolved)):
                tally.review()
                continue
            event_id = self._xref(row.provider, "event", row.provider_event_id)
            if event_id is not None:
                event = self._s.get_one(Event, event_id)
            else:
                event = self._match_event(row, home.entity_id, away.entity_id) or self._new_event(
                    row, home.entity_id, away.entity_id
                )
                self._add_xref("event", event.id, row.provider, row.provider_event_id)
            if _moves_tip(event.commence_time, row.commence_time):
                event.commence_time = row.commence_time
            event.status = _next_status(event.status, row.status)
            tally.written += 1
        self._s.flush()
        return tally.stats()

    def _match_event(self, row: EventRow, home_id: int, away_id: int) -> Event | None:
        candidates = self._s.scalars(
            select(Event).where(
                Event.sport == row.sport,
                Event.home_team_id == home_id,
                Event.away_team_id == away_id,
                Event.commence_time.between(
                    row.commence_time - EVENT_MATCH_WINDOW, row.commence_time + EVENT_MATCH_WINDOW
                ),
            )
        ).all()
        return min(candidates, key=lambda e: abs(e.commence_time - row.commence_time), default=None)

    def _new_event(self, row: EventRow, home_id: int, away_id: int) -> Event:
        event = Event(
            sport=row.sport,
            season=season_start_year(row.commence_time.astimezone(EASTERN).date()),
            home_team_id=home_id,
            away_team_id=away_id,
            commence_time=row.commence_time,
            status=EventStatus.SCHEDULED.value,
        )
        self._s.add(event)
        self._s.flush()
        return event

    # -- odds -----------------------------------------------------------------------------

    def odds(self, rows: Iterable[OddsRow], source_id: int, *, is_closing: bool) -> WriteStats:
        """One market per (event, key, period, player, line); one snapshot per side and book.
        Closing rows captured at or after their event's tip are skipped (F6)."""
        provider = self._source(source_id).provider
        tally = _Tally()
        events: dict[str, Event | None] = {}
        players: dict[tuple[int, str], Resolution] = {}
        # A closing fetch can return a payload identical to an earlier fetch (same source):
        # its closing snapshots are distinct rows, so is_closing is part of the identity.
        seen = {
            tuple(r)
            for r in self._s.execute(
                select(OddsSnapshot.market_id, OddsSnapshot.book, OddsSnapshot.side).where(
                    OddsSnapshot.source_id == source_id, OddsSnapshot.is_closing == is_closing
                )
            )
        }
        for row in rows:
            if row.provider_event_id not in events:
                event_id = self._xref(provider, "event", row.provider_event_id)
                events[row.provider_event_id] = (
                    None if event_id is None else self._s.get_one(Event, event_id)
                )
            event = events[row.provider_event_id]
            if event is None or (row.stat is not None and row.player_name is None):
                tally.skipped += 1
                continue
            if is_closing and row.captured_at >= event.commence_time:
                # A price seen at or after tip is in-game, never a closing line (F6). Skipped
                # like any row that cannot be written as asked; the payload stays in sources.
                tally.skipped += 1
                continue
            player_id: int | None = None
            if row.player_name is not None:
                key = (event.id, row.player_name)
                if key not in players:
                    players[key] = self._resolver.player_in_event(
                        provider,
                        row.player_name,
                        sport=Sport(event.sport),
                        team_ids=(event.home_team_id, event.away_team_id),
                    )
                resolution = players[key]
                if isinstance(resolution, NeedsReview):
                    tally.review()
                    continue
                player_id = resolution.entity_id
            market_id = self._market(event.id, row, player_id)
            if (market_id, row.book, row.side) not in seen:
                seen.add((market_id, row.book, row.side))
                self._s.add(
                    OddsSnapshot(
                        market_id=market_id,
                        book=row.book,
                        side=row.side,
                        price=row.price,
                        line=row.line,
                        captured_at=row.captured_at,
                        source_id=source_id,
                        is_closing=is_closing,
                    )
                )
            tally.written += 1
        self._s.flush()
        return tally.stats()

    def _market(self, event_id: int, row: OddsRow, player_id: int | None) -> int:
        line = _market_line(row)
        key = (event_id, row.market_key, row.period.value, player_id, line)
        if key not in self._markets:
            market_id = self._s.scalar(
                select(Market.id).where(
                    Market.event_id == event_id,
                    Market.provider_key == row.market_key,
                    Market.period == row.period,
                    Market.player_id.is_not_distinct_from(player_id),
                    Market.line.is_not_distinct_from(line),
                )
            )
            if market_id is None:
                market = Market(
                    event_id=event_id,
                    market_type=row.market_type,
                    period=row.period,
                    provider_key=row.market_key,
                    stat=row.stat,
                    player_id=player_id,
                    line=line,
                )
                self._s.add(market)
                self._s.flush()
                market_id = market.id
            self._markets[key] = market_id
        return self._markets[key]

    # -- injuries, results, box scores ----------------------------------------------------

    def injuries(self, rows: Iterable[InjuryRow], source_id: int) -> WriteStats:
        """Players resolve within the injury's team (R7a); unchanged reports are not repeated."""
        source = self._source(source_id)
        tally = _Tally()
        for row in rows:
            team = self._resolver.team(source.provider, row.team_provider_id)
            if not isinstance(team, Resolved):
                tally.review()
                continue
            player = self._resolver.player(
                source.provider,
                f"{row.team_provider_id}:{row.player_name}",  # team-scoped key (R7a)
                row.player_name,
                sport=Sport(self._s.get_one(Team, team.entity_id).sport),
                team_id=team.entity_id,
            )
            if not isinstance(player, Resolved):
                tally.review()
                continue
            reported_at = row.reported_at or source.fetched_at
            exists = self._s.scalar(
                select(Injury.id).where(
                    Injury.player_id == player.entity_id,
                    Injury.status == row.status,
                    Injury.reported_at == reported_at,
                    Injury.detail.is_not_distinct_from(row.detail),
                )
            )
            if exists is None:
                self._s.add(
                    Injury(
                        player_id=player.entity_id,
                        status=row.status,
                        detail=row.detail,
                        reported_at=reported_at,
                        source_id=source_id,
                    )
                )
                self._s.flush()
            tally.written += 1
        return tally.stats()

    def results(self, rows: Iterable[ResultRow], source_id: int) -> WriteStats:
        """final_at is when we first saw the final (R4): it may move earlier, never later.
        Scores follow the newest source by fetched_at, whatever order sources are written in."""
        source = self._source(source_id)
        tally = _Tally()
        for row in rows:
            event_id = self._xref(source.provider, "event", row.provider_event_id)
            if event_id is None:
                tally.skipped += 1
                continue
            final_at = row.final_at or source.fetched_at
            result = self._s.scalar(select(Result).where(Result.event_id == event_id))
            if result is None:
                self._s.add(
                    Result(
                        event_id=event_id,
                        home_score=row.home_score,
                        away_score=row.away_score,
                        final_at=final_at,
                        source_id=source_id,
                    )
                )
            else:
                result.final_at = min(result.final_at, final_at)
                # Scores (and source_id, their provenance) come from the newest observation,
                # so reparsing an older source never reverts a correction.
                if source.fetched_at >= self._source(result.source_id).fetched_at:
                    result.home_score, result.away_score = row.home_score, row.away_score
                    result.source_id = source_id
            self._s.get_one(Event, event_id).status = EventStatus.FINAL.value
            tally.written += 1
        self._s.flush()
        return tally.stats()

    def player_stats(self, rows: Iterable[PlayerGameStatRow], source_id: int) -> WriteStats:
        """Box scores create or refresh their players first, then map to our events by
        (team, ET game date) (R1): a row with no stored event is skipped, its player is not.

        nba_stats is the only provider that creates players, so this is how a fresh
        database gets them; a player's team follows his newest game (trades).

        Lookups are batched, because a backfill writes a season of rows over a public
        proxy: the rows' known players and games are loaded in one query each, the players'
        newest stored games in one more, and the box scores go in as one insert.
        """
        batch = list(rows)
        provider = self._source(source_id).provider
        tally = _Tally()
        teams = {
            abbr: team_id
            for abbr, team_id in self._s.execute(
                select(Team.abbreviation, Team.id).where(Team.sport == Sport.NBA)
            )
        }
        self._resolver.prime_xrefs(provider, "player", {r.provider_player_id for r in batch})
        self._resolver.prime_xrefs(provider, "event", {r.provider_game_id for r in batch})
        placed: list[tuple[PlayerGameStatRow, int, int]] = []
        for row in batch:
            team_id = teams.get(row.team_abbr)
            if team_id is None:
                tally.skipped += 1
                continue
            player = self._resolver.player(
                provider, row.provider_player_id, row.player_name, sport=Sport.NBA, team_id=team_id
            )
            if not isinstance(player, Resolved):
                tally.review()
                continue
            placed.append((row, player.entity_id, team_id))
        self._follow_teams(placed)

        games: dict[str, int | None] = {}  # a game with no stored event is looked up once
        values: dict[tuple[int, int], dict[str, object]] = {}
        for row, player_id, team_id in placed:
            if row.provider_game_id not in games:
                games[row.provider_game_id] = self._game_event(provider, row, team_id)
            event_id = games[row.provider_game_id]
            if event_id is None:
                tally.skipped += 1
                continue
            values[(event_id, player_id)] = {
                "event_id": event_id,
                "player_id": player_id,
                "stats": row.stats,
                "source_id": source_id,
            }
            tally.written += 1
        if values:
            stmt = insert(PlayerGameStat).values(list(values.values()))
            self._s.execute(
                stmt.on_conflict_do_update(
                    index_elements=["event_id", "player_id"],
                    set_={"stats": stmt.excluded.stats, "source_id": stmt.excluded.source_id},
                )
            )
        self._s.flush()
        return tally.stats()

    def _follow_teams(self, placed: Sequence[tuple[PlayerGameStatRow, int, int]]) -> None:
        """Move each player to the team of his newest game: his newest row here, unless a
        stored box score is newer (so source and row order do not matter)."""
        newest: dict[int, tuple[date, int]] = {}
        for row, player_id, team_id in placed:
            seen = newest.get(player_id)
            if seen is None or row.game_date >= seen[0]:
                newest[player_id] = (row.game_date, team_id)
        if not newest:
            return
        stored = self._s.execute(
            select(Player.id, Player.team_id, func.max(Event.commence_time))
            .outerjoin(PlayerGameStat, PlayerGameStat.player_id == Player.id)
            .outerjoin(Event, Event.id == PlayerGameStat.event_id)
            .where(Player.id.in_(list(newest)))
            .group_by(Player.id)
        )
        for player_id, current_team, latest in stored:
            game_date, team_id = newest[player_id]
            if latest is not None and game_date < latest.astimezone(EASTERN).date():
                continue
            if current_team != team_id:
                self._s.execute(
                    update(Player).where(Player.id == player_id).values(team_id=team_id)
                )

    def _game_event(self, provider: str, row: PlayerGameStatRow, team_id: int) -> int | None:
        event_id = self._xref(provider, "event", row.provider_game_id)
        if event_id is not None:
            return event_id
        start, end = _eastern_day(row.game_date)
        matches = self._s.scalars(
            select(Event.id).where(
                or_(Event.home_team_id == team_id, Event.away_team_id == team_id),
                Event.commence_time >= start,
                Event.commence_time < end,
            )
        ).all()
        if len(matches) != 1:
            return None
        self._add_xref("event", matches[0], provider, row.provider_game_id)
        return matches[0]

    # -- helpers --------------------------------------------------------------------------

    def _source(self, source_id: int) -> Source:
        return self._s.get_one(Source, source_id)

    def _xref(self, provider: str, kind: str, provider_id: str) -> int | None:
        return self._resolver.xref(provider, kind, provider_id)

    def _add_xref(self, kind: str, entity_id: int, provider: str, provider_id: str) -> None:
        self._resolver.add_xref(kind, entity_id, provider, provider_id)


def _eastern_day(d: date) -> tuple[datetime, datetime]:
    start = datetime.combine(d, time.min, tzinfo=EASTERN)
    return start, start + timedelta(days=1)
