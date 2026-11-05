import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
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
    QuotaLedger,
    Result,
    SourceObservation,
    Team,
)
from sharpline.ingest.jobs import (
    ingest_game_lines,
    ingest_injuries,
    ingest_player_stats,
    ingest_props,
    ingest_results,
    ingest_schedule,
)
from sharpline.ingest.writer import WriteStats
from sharpline.sources.espn.client import EspnClient
from sharpline.sources.http import HttpClient
from sharpline.sources.nba_stats.client import NbaStatsClient
from sharpline.sources.odds_api.client import OddsApiClient
from sharpline.sports.nba.teams import seed_nba_teams

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


@pytest.fixture
def teams(db_session: Session) -> None:
    seed_nba_teams(db_session)


@pytest.mark.usefixtures("teams")
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


@pytest.mark.usefixtures("teams")
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


@pytest.mark.usefixtures("teams")
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


@pytest.mark.usefixtures("teams")
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


@pytest.mark.usefixtures("teams")
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
