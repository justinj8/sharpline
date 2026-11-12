import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from sharpline.db.models import (
    EntityReview,
    EntityXref,
    Event,
    Injury,
    Market,
    OddsSnapshot,
    Player,
    PlayerGameStat,
    QuotaLedger,
    Result,
    Source,
    SourceObservation,
    Team,
)
from sharpline.ingest.jobs import (
    backfill_season,
    ingest_game_lines,
    ingest_injuries,
    ingest_player_stats,
    ingest_props,
    ingest_results,
    ingest_schedule,
)
from sharpline.ingest.reparse import reparse
from sharpline.ingest.writer import Writer, WriteStats
from sharpline.sources.espn.client import EspnClient
from sharpline.sources.http import HttpClient
from sharpline.sources.nba_stats.client import NbaStatsClient
from sharpline.sources.odds_api.client import OddsApiClient

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parents[2] / "fixtures"
T0 = datetime(2026, 11, 4, 18, 0, tzinfo=UTC)
QUOTA = {"x-requests-last": "3", "x-requests-used": "120", "x-requests-remaining": "380"}


def _count(session: Session, model: type) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def _http(base: str, fixture: str, headers: dict[str, str] | None = None) -> HttpClient:
    body: Any = json.loads((FIXTURES / fixture).read_text())

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body, headers=headers or {})

    return HttpClient(
        base, transport=httpx.MockTransport(handler), sleep=lambda _s: None, max_retries=0
    )


def _odds(fixture: str) -> OddsApiClient:
    http = _http("https://api.the-odds-api.com/v4", f"odds_api/{fixture}", QUOTA)
    return OddsApiClient(SecretStr("sk-test"), http, clock=lambda: T0)


def _espn(fixture: str) -> EspnClient:
    base = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
    return EspnClient(_http(base, f"espn/{fixture}"), clock=lambda: T0)


def _team(session: Session, abbr: str) -> int:
    team_id = session.scalar(select(Team.id).where(Team.abbreviation == abbr))
    assert team_id is not None
    return team_id


def test_ingest_game_lines_end_to_end_with_fixture(db_session: Session) -> None:
    stats = ingest_game_lines(db_session, _odds("nba_game_odds.json"))
    # 2 events + 22 snapshots (fanduel's Under at +50 is dropped by the parser)
    assert stats == WriteStats(written=24, skipped=0, reviews=0)
    assert _count(db_session, Event) == 2
    assert _count(db_session, Market) == 8  # per event: h2h, one spread, two totals lines
    assert _count(db_session, OddsSnapshot) == 22

    evt1 = db_session.scalar(
        select(EntityXref.entity_id).where(
            EntityXref.provider == "odds_api", EntityXref.provider_id == "evt1"
        )
    )
    spread = db_session.scalars(
        select(Market).where(Market.event_id == evt1, Market.provider_key == "spreads")
    ).one()
    assert spread.line == Decimal("-5.5")
    totals = db_session.scalars(
        select(Market.line).where(Market.event_id == evt1, Market.provider_key == "totals")
    ).all()
    assert sorted(totals) == [Decimal("224.5"), Decimal("225.0")]

    assert db_session.scalars(select(SourceObservation.purpose)).all() == [None]
    ledger = db_session.scalars(select(QuotaLedger)).one()
    assert (ledger.provider, ledger.cost, ledger.remaining) == ("odds_api", 3, 380)
    assert not any(s.is_closing for s in db_session.scalars(select(OddsSnapshot)))


def test_closing_flag_persisted_via_observation_purpose(db_session: Session) -> None:
    db_session.add(Player(sport="nba", full_name="Jayson Tatum", team_id=_team(db_session, "BOS")))
    db_session.flush()
    stats = ingest_props(
        db_session, _odds("nba_event_props.json"), "evt1", ["player_points"], is_closing=True
    )
    assert stats == WriteStats(written=5, skipped=0, reviews=0)  # 1 event + 4 snapshots
    assert db_session.scalars(select(SourceObservation.purpose)).all() == ["closing"]
    snaps = db_session.scalars(select(OddsSnapshot)).all()
    assert len(snaps) == 4 and all(s.is_closing for s in snaps)


def test_ingest_schedule_then_results(db_session: Session) -> None:
    stats = ingest_schedule(db_session, _espn("nba_scoreboard.json"), date(2026, 11, 4))
    assert stats == WriteStats(written=2, skipped=0, reviews=0)
    homes = set(db_session.scalars(select(Event.home_team_id)))
    assert homes == {_team(db_session, "DET"), _team(db_session, "WAS")}

    stats = ingest_results(db_session, _espn("nba_scoreboard_final.json"), date(2026, 11, 2))
    assert stats == WriteStats(written=4, skipped=0, reviews=0)  # 2 events + 2 results
    results = db_session.scalars(select(Result)).all()
    assert len(results) == 2 and all(r.final_at == T0 for r in results)
    finals = db_session.scalars(select(Event).where(Event.status == "final")).all()
    assert len(finals) == 2


def test_ingest_injuries(db_session: Session) -> None:
    db_session.add_all(
        [
            Player(sport="nba", full_name="Jayson Tatum", team_id=_team(db_session, "BOS")),
            Player(sport="nba", full_name="Jock Landale", team_id=_team(db_session, "ATL")),
        ]
    )
    db_session.flush()
    stats = ingest_injuries(db_session, _espn("nba_injuries.json"))
    assert stats == WriteStats(written=2, skipped=5, reviews=5)
    assert _count(db_session, Injury) == 2
    assert _count(db_session, EntityReview) == 5


def test_ingest_player_stats_derives_season_and_maps_events(db_session: Session) -> None:
    db_session.add(
        Event(
            sport="nba",
            season=2025,
            home_team_id=_team(db_session, "GSW"),
            away_team_id=_team(db_session, "LAC"),
            commence_time=datetime(2025, 10, 29, 2, 30, tzinfo=UTC),
        )
    )
    db_session.flush()
    payload = json.loads((FIXTURES / "nba_stats" / "league_game_log_players.json").read_text())
    calls: list[dict[str, Any]] = []

    class _Endpoint:
        def get_dict(self) -> dict[str, Any]:
            return payload

    def factory(**kw: Any) -> _Endpoint:
        calls.append(kw)
        return _Endpoint()

    nba = NbaStatsClient(endpoint_factory=factory, sleep=lambda _s: None, clock=lambda: T0)
    stats = ingest_player_stats(db_session, nba, date(2025, 10, 28))
    assert calls[0]["season"] == "2025-26"
    assert calls[0]["date_from_nullable"] == calls[0]["date_to_nullable"] == "10/28/2025"
    assert stats == WriteStats(written=2, skipped=3, reviews=0)  # only GSW-LAC has an event
    assert _count(db_session, PlayerGameStat) == 2


def test_parser_error_still_records_the_fetch(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise ValueError("parser bug")

    monkeypatch.setattr("sharpline.ingest.jobs.parse_odds", broken)
    with pytest.raises(ValueError, match="parser bug"):
        ingest_game_lines(db_session, _odds("nba_game_odds.json"))
    assert _count(db_session, Source) == 1
    assert _count(db_session, SourceObservation) == 1
    assert _count(db_session, QuotaLedger) == 1
    assert _count(db_session, Event) == 0  # the write step rolled back as a unit


def test_closing_fetch_with_an_unchanged_payload_still_writes_closing_snapshots(
    db_session: Session,
) -> None:
    db_session.add(Player(sport="nba", full_name="Jayson Tatum", team_id=_team(db_session, "BOS")))
    db_session.flush()
    client = _odds("nba_event_props.json")
    ingest_props(db_session, client, "evt1", ["player_points"])
    ingest_props(db_session, client, "evt1", ["player_points"], is_closing=True)

    assert _count(db_session, Source) == 1  # identical payload: one source, two observations
    purposes = db_session.scalars(select(SourceObservation.purpose)).all()
    assert set(purposes) == {"closing", None} and len(purposes) == 2
    flags = db_session.scalars(select(OddsSnapshot.is_closing)).all()
    assert flags.count(True) == flags.count(False) == 4


def test_idle_session_commits_the_fetch_before_a_failing_write(
    private_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production path: a plain session, no outer transaction, real commits."""

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise ValueError("parser bug")

    monkeypatch.setattr("sharpline.ingest.jobs.parse_odds", broken)
    sessions = sessionmaker(bind=private_engine)
    with sessions() as session:
        assert not session.in_transaction()
        with pytest.raises(ValueError, match="parser bug"):
            ingest_game_lines(session, _odds("nba_game_odds.json"))
        session.rollback()  # anything left uncommitted is discarded here

    with sessions() as fresh:  # a new session on a new connection sees committed rows only
        assert _count(fresh, Source) == 1
        assert _count(fresh, SourceObservation) == 1
        assert _count(fresh, QuotaLedger) == 1
        assert _count(fresh, Event) == 0


def test_schedule_seeing_a_final_writes_the_result(db_session: Session) -> None:
    """Ruling: a schedule refresh that sees a final stamps final_at like the results job (R4),
    so the live path and reparse (which treats every scoreboard as RESULTS) agree."""
    stats = ingest_schedule(db_session, _espn("nba_scoreboard_final.json"), date(2026, 11, 2))
    assert stats == WriteStats(written=4, skipped=0, reviews=0)  # 2 events + 2 results
    results = db_session.scalars(select(Result)).all()
    assert len(results) == 2 and all(r.final_at == T0 for r in results)
    assert set(db_session.scalars(select(Event.status))) == {"final"}


def test_closing_fetched_after_a_tip_has_no_closing_snapshot_even_on_reparse(
    db_session: Session,
) -> None:
    """F6: evt1 tipped at 00:00; a closing fetch at 00:05 prices only evt2 (tip 02:30), and
    reparsing the stored closing source does not flag evt1's prices either."""
    late = datetime(2026, 11, 5, 0, 5, tzinfo=UTC)
    http = _http("https://api.the-odds-api.com/v4", "odds_api/nba_game_odds.json", QUOTA)
    client = OddsApiClient(SecretStr("sk-test"), http, clock=lambda: late)
    ingest_game_lines(db_session, client, is_closing=True)

    def closing_events() -> set[str]:
        rows = db_session.execute(
            select(EntityXref.provider_id)
            .join(Market, Market.event_id == EntityXref.entity_id)
            .join(OddsSnapshot, OddsSnapshot.market_id == Market.id)
            .where(
                EntityXref.provider == "odds_api",
                EntityXref.entity_kind == "event",
                OddsSnapshot.is_closing,
            )
        )
        return {r[0] for r in rows}

    assert closing_events() == {"evt2"}
    before = _count(db_session, OddsSnapshot)
    reparse(db_session, provider="odds_api")
    assert closing_events() == {"evt2"}
    assert _count(db_session, OddsSnapshot) == before


def _game_log_over_two_days() -> dict[str, Any]:
    """The fixture game log (2025-10-28) plus a copy of its rows two days later."""
    payload = json.loads((FIXTURES / "nba_stats" / "league_game_log_players.json").read_text())
    headers = payload["resultSets"][0]["headers"]
    rows = payload["resultSets"][0]["rowSet"]
    game_id, game_date = headers.index("GAME_ID"), headers.index("GAME_DATE")
    later = []
    for row in rows:
        copy = list(row)
        copy[game_id] = f"{row[game_id]}-b"
        copy[game_date] = "2025-10-30"
        later.append(copy)
    payload["resultSets"][0]["rowSet"] = rows + later
    return payload


def test_backfill_commits_box_scores_per_game_day(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dropped connection mid-season loses one game day, not the season: each day's box
    scores commit on their own, and a rerun completes the rest."""
    gsw, lac = _team(db_session, "GSW"), _team(db_session, "LAC")
    for tip in (
        datetime(2025, 10, 29, 2, 30, tzinfo=UTC),
        datetime(2025, 10, 31, 2, 30, tzinfo=UTC),
    ):
        db_session.add(
            Event(sport="nba", season=2025, home_team_id=gsw, away_team_id=lac, commence_time=tip)
        )
    db_session.flush()
    payload = _game_log_over_two_days()

    class _Endpoint:
        def get_dict(self) -> dict[str, Any]:
            return payload

    nba = NbaStatsClient(endpoint_factory=lambda **_kw: _Endpoint(), sleep=lambda _s: None)
    base = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
    no_games = HttpClient(
        base,
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"events": []})),
        sleep=lambda _s: None,
        max_retries=0,
    )
    espn = EspnClient(no_games, clock=lambda: T0)

    real = Writer.player_stats
    calls: list[int] = []

    def drop_on_second_day(self: Writer, rows: Any, source_id: int) -> WriteStats:
        calls.append(source_id)
        if len(calls) == 2:
            raise RuntimeError("server closed the connection unexpectedly")
        return real(self, rows, source_id)

    monkeypatch.setattr(Writer, "player_stats", drop_on_second_day)
    with pytest.raises(RuntimeError, match="server closed"):
        backfill_season(db_session, nba, espn, "2025-26")
    assert _count(db_session, PlayerGameStat) == 2  # day one (GSW-LAC) survived

    monkeypatch.setattr(Writer, "player_stats", real)
    done = backfill_season(db_session, nba, espn, "2025-26")
    assert done.stats == WriteStats(written=4, skipped=6, reviews=0)
    assert _count(db_session, PlayerGameStat) == 4
