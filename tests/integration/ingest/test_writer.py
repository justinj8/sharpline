import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sharpline.db.models import (
    EntityReview,
    EntityXref,
    Event,
    Injury,
    Market,
    OddsSnapshot,
    Player,
    PlayerGameStat,
    Result,
    Team,
)
from sharpline.domain.enums import Side, Sport
from sharpline.ingest.observations import record_fetch
from sharpline.ingest.resolve import EntityResolver
from sharpline.ingest.writer import Writer, WriteStats
from sharpline.sources.fetched import Fetched
from sharpline.sources.nba_stats.parse import parse_player_game_logs
from sharpline.sources.odds_api.markets import describe_market
from sharpline.sources.odds_api.parse import parse_odds
from sharpline.sources.rows import EventRow, InjuryRow, OddsRow, ResultRow
from sharpline.sports.nba.teams import seed_nba_teams

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parents[2] / "fixtures"
T0 = datetime(2026, 11, 4, 18, 0, tzinfo=UTC)
TIP = datetime(2026, 11, 5, 0, 0, tzinfo=UTC)


def _count(session: Session, model: type) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def _source(session: Session, provider: str, payload: Any, at: datetime = T0) -> int:
    return record_fetch(session, Fetched(provider, "test", {}, payload, at))


def _team(session: Session, abbr: str) -> int:
    team_id = session.scalar(select(Team.id).where(Team.abbreviation == abbr))
    assert team_id is not None
    return team_id


def _player(session: Session, name: str, abbr: str | None) -> int:
    p = Player(sport="nba", full_name=name, team_id=None if abbr is None else _team(session, abbr))
    session.add(p)
    session.flush()
    return p.id


def _writer(session: Session) -> Writer:
    return Writer(session, EntityResolver(session))


def _event_row(
    provider: str = "odds_api",
    pid: str = "evt1",
    home: str = "Boston Celtics",
    away: str = "New York Knicks",
    tip: datetime = TIP,
) -> EventRow:
    return EventRow(
        provider=provider,
        provider_event_id=pid,
        sport=Sport.NBA,
        home_team=home,
        away_team=away,
        commence_time=tip,
    )


def _odds(
    key: str,
    side: Side,
    price: int,
    line: str | None,
    *,
    book: str = "draftkings",
    player: str | None = None,
    pid: str = "evt1",
) -> OddsRow:
    spec = describe_market(key)
    return OddsRow(
        provider_event_id=pid,
        market_key=key,
        market_type=spec.market_type,
        period=spec.period,
        stat=spec.stat,
        player_name=player,
        book=book,
        side=side,
        price=price,
        line=None if line is None else Decimal(line),
        captured_at=T0,
    )


def _xref(session: Session, provider: str, kind: str, pid: str) -> int | None:
    return session.scalar(
        select(EntityXref.entity_id).where(
            EntityXref.provider == provider,
            EntityXref.entity_kind == kind,
            EntityXref.provider_id == pid,
        )
    )


@pytest.fixture
def teams(db_session: Session) -> None:
    seed_nba_teams(db_session)


@pytest.mark.usefixtures("teams")
def test_events_create_event_with_season_and_xref(db_session: Session) -> None:
    src = _source(db_session, "odds_api", {"e": 1})
    stats = _writer(db_session).events([_event_row()], src)
    assert stats == WriteStats(written=1, skipped=0, reviews=0)
    event = db_session.scalars(select(Event)).one()
    assert (event.sport, event.season, event.commence_time) == ("nba", 2026, TIP)
    assert event.home_team_id == _team(db_session, "BOS")
    assert event.away_team_id == _team(db_session, "NYK")
    assert _xref(db_session, "odds_api", "event", "evt1") == event.id


@pytest.mark.usefixtures("teams")
def test_events_converge_across_providers_within_two_hours(db_session: Session) -> None:
    writer = _writer(db_session)
    espn = _event_row("espn", "401", "BOS", "NY", TIP + timedelta(minutes=10))
    writer.events([espn], _source(db_session, "espn", {"e": 1}))
    writer.events([_event_row()], _source(db_session, "odds_api", {"e": 1}))
    event = db_session.scalars(select(Event)).one()
    assert _xref(db_session, "espn", "event", "401") == event.id
    assert _xref(db_session, "odds_api", "event", "evt1") == event.id

    # same matchup a day later is a different game
    later = _event_row(pid="evt9", tip=TIP + timedelta(days=1))
    writer.events([later], _source(db_session, "odds_api", {"e": 2}))
    assert _count(db_session, Event) == 2


@pytest.mark.usefixtures("teams")
def test_events_unresolved_team_is_skipped_and_counted(db_session: Session) -> None:
    row = _event_row(home="Seattle SuperSonics")
    stats = _writer(db_session).events([row], _source(db_session, "odds_api", {"e": 1}))
    assert stats == WriteStats(written=0, skipped=1, reviews=1)
    assert _count(db_session, Event) == 0
    assert _count(db_session, EntityReview) == 1


@pytest.mark.usefixtures("teams")
def test_writer_is_idempotent_for_events_and_results(db_session: Session) -> None:
    writer = _writer(db_session)
    src = _source(db_session, "espn", {"final": 1})
    rows = [_event_row("espn", "401", "BOS", "NY")]
    results = [ResultRow(provider_event_id="401", home_score=110, away_score=99, final_at=None)]
    for _ in range(2):
        writer.events(rows, src)
        writer.results(results, src)
    assert _count(db_session, Event) == 1
    assert _count(db_session, Result) == 1
    assert _count(db_session, EntityXref) == 120 + 1  # seeded team xrefs + one event xref


@pytest.mark.usefixtures("teams")
def test_results_final_at_comes_from_source_and_never_moves_later(db_session: Session) -> None:
    writer = _writer(db_session)
    t1, t2 = T0 + timedelta(hours=6), T0 + timedelta(hours=7)
    writer.events([_event_row("espn", "401", "BOS", "NY")], _source(db_session, "espn", {"e": 1}))
    row = ResultRow(provider_event_id="401", home_score=110, away_score=99, final_at=None)

    assert writer.results([row], _source(db_session, "espn", {"r": 1}, t1)).written == 1
    result = db_session.scalars(select(Result)).one()
    assert result.final_at == t1
    event = db_session.get(Event, result.event_id)
    assert event is not None and event.status == "final"

    # a later observation (with a score correction) keeps the earlier final_at
    corrected = row.model_copy(update={"home_score": 111})
    writer.results([corrected], _source(db_session, "espn", {"r": 2}, t2))
    assert (result.final_at, result.home_score) == (t1, 111)

    # an earlier observation (e.g. reparse of an older source) moves it earlier
    writer.results([corrected], _source(db_session, "espn", {"r": 0}, T0))
    assert result.final_at == T0

    unknown = ResultRow(provider_event_id="nope", home_score=1, away_score=0, final_at=None)
    stats = writer.results([unknown], _source(db_session, "espn", {"r": 3}, t2))
    assert stats == WriteStats(written=0, skipped=1, reviews=0)


@pytest.mark.usefixtures("teams")
def test_writer_odds_keeps_per_book_lines_as_separate_markets(db_session: Session) -> None:
    tatum = _player(db_session, "Jayson Tatum", "BOS")
    payload = json.loads((FIXTURES / "odds_api" / "nba_event_props.json").read_text())
    src = _source(db_session, "odds_api", payload)
    writer = _writer(db_session)
    writer.events([_event_row()], src)
    stats = writer.odds(parse_odds(payload, T0), src, is_closing=False)
    assert stats == WriteStats(written=4, skipped=0, reviews=0)

    markets = db_session.scalars(select(Market).order_by(Market.line)).all()
    assert [m.line for m in markets] == [Decimal("27.5"), Decimal("28.5")]
    for market in markets:
        assert (market.market_type, market.stat, market.player_id) == (
            "player_prop",
            "points",
            tatum,
        )
        snaps = db_session.scalars(select(OddsSnapshot).where(OddsSnapshot.market_id == market.id))
        assert sorted(s.side for s in snaps) == ["OVER", "UNDER"]
    bos = _team(db_session, "BOS")
    assert _xref(db_session, "odds_api", "player", f"Jayson Tatum|{bos}") == tatum


@pytest.mark.usefixtures("teams")
def test_spread_pair_lands_in_one_market_with_home_perspective_line(db_session: Session) -> None:
    src = _source(db_session, "odds_api", {"e": 1})
    writer = _writer(db_session)
    writer.events([_event_row()], src)
    rows = [
        _odds("spreads", Side.HOME, -110, "-5.5"),
        _odds("spreads", Side.AWAY, -110, "5.5"),
        _odds("h2h", Side.HOME, -230, None),
        _odds("h2h", Side.AWAY, 190, None),
        _odds("totals", Side.OVER, -110, "224.5"),
        _odds("totals", Side.UNDER, -110, "224.5"),
    ]
    assert writer.odds(rows, src, is_closing=False).written == 6

    by_key = {m.provider_key: m for m in db_session.scalars(select(Market))}
    assert set(by_key) == {"spreads", "h2h", "totals"}
    assert by_key["spreads"].line == Decimal("-5.5")
    assert by_key["h2h"].line is None
    assert by_key["totals"].line == Decimal("224.5")
    spread_snaps = db_session.scalars(
        select(OddsSnapshot).where(OddsSnapshot.market_id == by_key["spreads"].id)
    ).all()
    assert sorted((s.side, s.line) for s in spread_snaps) == [
        ("AWAY", Decimal("5.5")),
        ("HOME", Decimal("-5.5")),
    ]


@pytest.mark.usefixtures("teams")
def test_writer_odds_is_idempotent_per_source(db_session: Session) -> None:
    src = _source(db_session, "odds_api", {"e": 1})
    writer = _writer(db_session)
    writer.events([_event_row()], src)
    rows = [_odds("h2h", Side.HOME, -230, None), _odds("h2h", Side.AWAY, 190, None)]
    writer.odds(rows, src, is_closing=False)
    writer.odds(rows, src, is_closing=False)
    assert _count(db_session, Market) == 1
    assert _count(db_session, OddsSnapshot) == 2


@pytest.mark.usefixtures("teams")
def test_writer_odds_skips_unresolved_player_and_counts_review(db_session: Session) -> None:
    src = _source(db_session, "odds_api", {"e": 1})
    writer = _writer(db_session)
    writer.events([_event_row()], src)
    rows = [
        _odds("player_points", Side.OVER, -115, "27.5", player="Nobody Here"),
        _odds("player_points", Side.UNDER, -105, "27.5", player="Nobody Here"),
        _odds("h2h", Side.HOME, -230, None, pid="unknown-event"),
    ]
    stats = writer.odds(rows, src, is_closing=False)
    assert stats == WriteStats(written=0, skipped=3, reviews=2)
    assert _count(db_session, Market) == 0
    review = db_session.scalars(select(EntityReview)).one()
    bos, nyk = sorted((_team(db_session, "BOS"), _team(db_session, "NYK")))
    assert (review.provider, review.provider_id) == ("odds_api", f"Nobody Here|{bos},{nyk}")


@pytest.mark.usefixtures("teams")
def test_props_resolve_within_the_events_teams_then_sport_wide(db_session: Session) -> None:
    celtic = _player(db_session, "Marcus Morris", "BOS")
    _player(db_session, "Marcus Morris", "LAL")
    traded = _player(db_session, "Jrue Holiday", "POR")  # stale team, unique sport-wide
    src = _source(db_session, "odds_api", {"e": 1})
    writer = _writer(db_session)
    writer.events([_event_row()], src)
    rows = [
        _odds("player_points", Side.OVER, -115, "10.5", player="Marcus Morris"),
        _odds("player_points", Side.OVER, -115, "12.5", player="Jrue Holiday"),
    ]
    assert writer.odds(rows, src, is_closing=True).written == 2
    assert {m.player_id for m in db_session.scalars(select(Market))} == {celtic, traded}
    assert all(s.is_closing for s in db_session.scalars(select(OddsSnapshot)))


@pytest.mark.usefixtures("teams")
def test_injuries_resolve_with_the_injury_team(db_session: Session) -> None:
    hawk = _player(db_session, "Marcus Morris", "ATL")
    _player(db_session, "Marcus Morris", "BOS")
    reported = datetime(2026, 11, 3, 22, 8, tzinfo=UTC)
    src = _source(db_session, "espn", {"i": 1})
    rows = [
        InjuryRow(
            team_provider_id="1",  # ESPN id for Atlanta
            player_name="Marcus Morris",
            status="out",
            detail="ankle",
            reported_at=reported,
        ),
        InjuryRow(
            team_provider_id="1",
            player_name="Nobody Here",
            status="questionable",
            detail=None,
            reported_at=None,
        ),
        InjuryRow(
            team_provider_id="999", player_name="X", status="out", detail=None, reported_at=None
        ),
    ]
    writer = _writer(db_session)
    assert writer.injuries(rows, src) == WriteStats(written=1, skipped=2, reviews=2)
    assert writer.injuries(rows, src).written == 1  # idempotent re-write
    injury = db_session.scalars(select(Injury)).one()
    assert (injury.player_id, injury.status, injury.detail) == (hawk, "out", "ankle")
    assert (injury.reported_at, injury.source_id) == (reported, src)
    assert _xref(db_session, "espn", "player", "1:Marcus Morris") == hawk


@pytest.mark.usefixtures("teams")
def test_injury_without_reported_at_uses_source_fetched_at(db_session: Session) -> None:
    _player(db_session, "Jayson Tatum", "BOS")
    src = _source(db_session, "espn", {"i": 2}, T0)
    row = InjuryRow(
        team_provider_id="2",
        player_name="Jayson Tatum",
        status="out",
        detail=None,
        reported_at=None,
    )
    assert _writer(db_session).injuries([row], src).written == 1
    assert db_session.scalars(select(Injury.reported_at)).one() == T0


@pytest.mark.usefixtures("teams")
def test_player_stats_map_to_events_by_team_and_eastern_date(db_session: Session) -> None:
    writer = _writer(db_session)
    # 2025-10-29 02:30Z is 22:30 on 10-28 in New York: same ET game_date as the fixture
    gsw_lac = _event_row("espn", "e1", "GS", "LAC", datetime(2025, 10, 29, 2, 30, tzinfo=UTC))
    mil_nyk = _event_row("espn", "e2", "MIL", "NY", datetime(2025, 10, 28, 23, 30, tzinfo=UTC))
    writer.events([gsw_lac, mil_nyk], _source(db_session, "espn", {"e": 1}))

    payload = json.loads((FIXTURES / "nba_stats" / "league_game_log_players.json").read_text())
    rows = parse_player_game_logs(payload)
    src = _source(db_session, "nba_stats", payload)
    stats = writer.player_stats(rows, src)
    assert stats == WriteStats(written=4, skipped=1, reviews=0)  # MIA-CHA has no event
    assert writer.player_stats(rows, src).written == 4
    assert _count(db_session, PlayerGameStat) == 4

    gsw_lac_id = _xref(db_session, "espn", "event", "e1")
    assert _xref(db_session, "nba_stats", "event", "0022500127") == gsw_lac_id
    assert _xref(db_session, "nba_stats", "event", "0022500124") is None
    hield = _xref(db_session, "nba_stats", "player", "1627741")
    stat = db_session.scalars(select(PlayerGameStat).where(PlayerGameStat.player_id == hield)).one()
    assert stat.event_id == gsw_lac_id and stat.stats["points"] == 2.0


@pytest.mark.usefixtures("teams")
def test_same_name_players_on_different_teams_stay_distinct(db_session: Session) -> None:
    celtic = _player(db_session, "Marcus Morris", "BOS")
    laker = _player(db_session, "Marcus Morris", "LAL")
    writer = _writer(db_session)
    events = [
        _event_row(),  # evt1: BOS vs NYK
        _event_row(pid="evt2", home="Los Angeles Lakers", away="Denver Nuggets"),
    ]
    props = [
        _odds("player_points", Side.OVER, -115, "10.5", player="Marcus Morris", pid="evt1"),
        _odds("player_points", Side.OVER, -115, "8.5", player="Marcus Morris", pid="evt2"),
    ]
    for n in range(2):  # second pass resolves through the team-scoped xrefs
        src = _source(db_session, "odds_api", {"pass": n})
        writer.events(events, src)
        assert writer.odds(props, src, is_closing=False).written == 2
    by_line = {m.line: m.player_id for m in db_session.scalars(select(Market))}
    assert by_line == {Decimal("10.5"): celtic, Decimal("8.5"): laker}
    bos, lal = _team(db_session, "BOS"), _team(db_session, "LAL")
    assert _xref(db_session, "odds_api", "player", f"Marcus Morris|{bos}") == celtic
    assert _xref(db_session, "odds_api", "player", f"Marcus Morris|{lal}") == laker

    injuries = [
        InjuryRow(
            team_provider_id=espn_id,  # ESPN ids: Boston 2, Lakers 13
            player_name="Marcus Morris",
            status="out",
            detail=None,
            reported_at=T0,
        )
        for espn_id in ("2", "13")
    ]
    assert writer.injuries(injuries, _source(db_session, "espn", {"i": 9})).written == 2
    assert set(db_session.scalars(select(Injury.player_id))) == {celtic, laker}
    assert _xref(db_session, "espn", "player", "2:Marcus Morris") == celtic
    assert _xref(db_session, "espn", "player", "13:Marcus Morris") == laker
