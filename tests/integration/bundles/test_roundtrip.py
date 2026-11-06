"""Record and replay: capture a slate in DB A, export it, load it into a fresh DB B.

DB A is populated the way production capture populates it: a CaptureRunner session against
MockTransport providers, then the next morning's results and box scores. DB B only ever
sees the bundle file. Snapshots compare natural keys (no surrogate ids).
"""

from __future__ import annotations

import copy
import json
from collections import Counter
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from sharpline.bundles.export import export_bundle
from sharpline.bundles.format import read_bundle
from sharpline.bundles.load import load_bundle
from sharpline.capture.quota import QuotaGuard
from sharpline.capture.runner import CaptureRunner
from sharpline.db.models import EntityXref, Player, Team
from sharpline.domain.enums import Sport
from sharpline.ingest import jobs
from sharpline.ingest.reparse import reparse
from sharpline.ingest.writer import WriteStats
from sharpline.sources.espn.client import EspnClient
from sharpline.sources.http import CircuitBreaker, HttpClient
from sharpline.sources.nba_stats.client import NbaStatsClient
from sharpline.sources.odds_api.client import OddsApiClient

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parents[2] / "fixtures"
SLATE = date(2026, 11, 4)  # ET; both tips are that evening
TIP1 = datetime(2026, 11, 5, 0, 0, tzinfo=UTC)
TIP2 = datetime(2026, 11, 5, 2, 30, tzinfo=UTC)
ESPN_TIP2 = datetime(2026, 11, 5, 2, 35, tzinfo=UTC)  # providers disagree by 5 min (R9)
START = datetime(2026, 11, 4, 23, 0, tzinfo=UTC)
MID_CAPTURE_FINAL = datetime(2026, 11, 5, 2, 0, tzinfo=UTC)  # hourly refresh sees e1 final
FINAL_EARLY = datetime(2026, 11, 5, 6, 0, tzinfo=UTC)
FINAL_LATE = datetime(2026, 11, 5, 15, 0, tzinfo=UTC)  # a stat correction
BOX_SCORES_AT = datetime(2026, 11, 5, 16, 0, tzinfo=UTC)
ESPN_GAMES = {"e1": (TIP1, "BOS", "NY"), "e2": (ESPN_TIP2, "LAC", "DEN")}
# Known to DB A before the slate (standing in for players created by earlier slates' box
# scores): Tatum and Landale are injured "Out" and have no box score on this slate.
KNOWN_PLAYERS = [
    ("Jayson Tatum", "BOS", "1628369"),
    ("Jaylen Brown", "BOS", "1627759"),
    ("Jock Landale", "ATL", "1629111"),
]
BOX_SCORE = [  # (player id, name, team, game id, matchup, pts)
    (1627759, "Jaylen Brown", "BOS", "0022600101", "BOS vs. NYK", 31),
    (1628973, "Jalen Brunson", "NYK", "0022600101", "NYK @ BOS", 28),
    (202695, "Kawhi Leonard", "LAC", "0022600102", "LAC vs. DEN", 24),
    (203999, "Nikola Jokić", "DEN", "0022600102", "DEN @ LAC", 33),
]


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def sleep_until(self, at: datetime) -> None:
        self.now = max(self.now, at)


def _retimed(event: dict[str, Any]) -> dict[str, Any]:
    event = copy.deepcopy(event)
    event["commence_time"] = {"evt1": "2026-11-05T00:00:00Z", "evt2": "2026-11-05T02:30:00Z"}[
        event["id"]
    ]
    return event


def _props(event_id: str) -> dict[str, Any]:
    if event_id == "evt2":
        meta = next(e for e in _load("odds_api/nba_events.json") if e["id"] == "evt2")
        return {**meta, "bookmakers": []}
    props = _load("odds_api/nba_event_props.json")
    brown = copy.deepcopy(props["bookmakers"][0])
    for outcome in brown["markets"][0]["outcomes"]:
        outcome["description"], outcome["point"] = "Jaylen Brown", 24.5
    props["bookmakers"][0]["markets"] += brown["markets"]
    return props


def _scoreboard(final: dict[str, tuple[int, int]] | None = None) -> dict[str, Any]:
    events = []
    for espn_id, (tip, home, away) in ESPN_GAMES.items():
        scores = (final or {}).get(espn_id)
        competitors = [
            {"homeAway": side, "team": {"abbreviation": abbr}}
            for side, abbr in (("home", home), ("away", away))
        ]
        if scores is not None:
            competitors[0]["score"], competitors[1]["score"] = str(scores[0]), str(scores[1])
        events.append(
            {
                "id": espn_id,
                "date": tip.strftime("%Y-%m-%dT%H:%MZ"),
                "status": {"type": {"name": "STATUS_FINAL" if scores else "STATUS_SCHEDULED"}},
                "competitions": [{"competitors": competitors}],
            }
        )
    return {"events": events}


def _espn_handler(clock: Callable[[], datetime]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/injuries"):
            return httpx.Response(200, json=_load("espn/nba_injuries.json"))
        early_final = {"e1": (110, 104)} if clock() >= MID_CAPTURE_FINAL else None
        return httpx.Response(200, json=_scoreboard(early_final))

    return handler


def _odds_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    body: Any
    if path.endswith("/events"):
        body, cost = [_retimed(e) for e in _load("odds_api/nba_events.json")], 0
    elif "/events/" in path:
        body = _props(path.split("/")[-2])
        cost = len(request.url.params["markets"].split(","))
    else:
        wanted = request.url.params.get("eventIds")
        body = [
            _retimed(e)
            for e in _load("odds_api/nba_game_odds.json")
            if wanted is None or e["id"] in wanted.split(",")
        ]
        cost = 3
    headers = {"x-requests-last": str(cost), "x-requests-remaining": "400"}
    return httpx.Response(200, json=body, headers=headers)


def _http(base: str, handler: Callable[[httpx.Request], httpx.Response]) -> HttpClient:
    return HttpClient(
        base, transport=httpx.MockTransport(handler), sleep=lambda _s: None, max_retries=0
    )


def _espn(clock: Callable[[], datetime], handler: Callable[[httpx.Request], httpx.Response]) -> Any:
    base = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
    return EspnClient(_http(base, handler), clock=clock)


def _box_scores() -> dict[str, Any]:
    payload = _load("nba_stats/league_game_log_players.json")
    result_set = payload["resultSets"][0]
    template = dict(zip(result_set["headers"], result_set["rowSet"][0], strict=True))
    rows = []
    for player_id, name, team, game_id, matchup, pts in BOX_SCORE:
        row = {
            **template,
            "SEASON_ID": "22026",
            "PLAYER_ID": player_id,
            "PLAYER_NAME": name,
            "TEAM_ABBREVIATION": team,
            "GAME_ID": game_id,
            "GAME_DATE": "2026-11-04",
            "MATCHUP": matchup,
            "MIN": 34,
            "PTS": pts,
        }
        rows.append([row[h] for h in result_set["headers"]])
    result_set["rowSet"] = rows
    return payload


def _seed_known_players(session: Session) -> None:
    for name, abbr, nba_id in KNOWN_PLAYERS:
        team_id = session.scalar(select(Team.id).where(Team.abbreviation == abbr))
        player = Player(sport="nba", full_name=name, team_id=team_id)
        session.add(player)
        session.flush()
        session.add(
            EntityXref(
                entity_kind="player", entity_id=player.id, provider="nba_stats", provider_id=nba_id
            )
        )


def capture_slate(sessions: sessionmaker[Session]) -> int:
    """DB A: a full capture session, then next-morning results and box scores."""
    with sessions.begin() as session:
        _seed_known_players(session)

    clock = Clock(START)
    odds = OddsApiClient(
        SecretStr("sk-test"),
        _http("https://api.the-odds-api.com/v4", _odds_handler),
        clock=clock,
    )
    runner = CaptureRunner(
        sessions,
        espn=_espn(clock, _espn_handler(clock)),
        odds=odds,
        guard=QuotaGuard(session_cap=180, floor=20),
        breaker=CircuitBreaker(clock=clock),
        clock=clock,
        sleep_until=clock.sleep_until,
    )
    run_id = runner.run(Sport.NBA, SLATE)

    finals = [
        (FINAL_EARLY, {"e1": (110, 104), "e2": (99, 120)}),
        (FINAL_LATE, {"e1": (111, 104), "e2": (99, 120)}),
    ]
    for at, scores in finals:
        payload = _scoreboard(scores)
        espn = _espn(lambda at=at: at, lambda _r, p=payload: httpx.Response(200, json=p))
        with sessions() as session:
            jobs.ingest_results(session, espn, SLATE)

    class _Endpoint:
        def get_dict(self) -> dict[str, Any]:
            return _box_scores()

    nba = NbaStatsClient(
        endpoint_factory=lambda **_kw: _Endpoint(),
        sleep=lambda _s: None,
        clock=lambda: BOX_SCORES_AT,
    )
    with sessions() as session:
        jobs.ingest_player_stats(session, nba, SLATE)
    return run_id


_EVENT = "h.abbreviation AS home, a.abbreviation AS away, e.commence_time"
_EVENT_JOIN = "JOIN teams h ON h.id = e.home_team_id JOIN teams a ON a.id = e.away_team_id"
_SOURCE = "s.provider, s.endpoint, s.params_hash, s.content_hash"
SNAPSHOT_SQL = {
    "events": f"SELECT {_EVENT}, e.sport, e.season, e.status FROM events e {_EVENT_JOIN}",
    "markets": f"""
        SELECT {_EVENT}, m.market_type, m.period, m.provider_key, m.stat, p.full_name, m.line
        FROM markets m JOIN events e ON e.id = m.event_id {_EVENT_JOIN}
        LEFT JOIN players p ON p.id = m.player_id""",
    "odds_snapshots": f"""
        SELECT {_EVENT}, m.provider_key, m.period, p.full_name, m.line,
               o.book, o.side, o.price, o.line, o.captured_at, o.is_closing, {_SOURCE}
        FROM odds_snapshots o JOIN markets m ON m.id = o.market_id
        JOIN events e ON e.id = m.event_id {_EVENT_JOIN}
        LEFT JOIN players p ON p.id = m.player_id JOIN sources s ON s.id = o.source_id""",
    "injuries": f"""
        SELECT p.full_name, t.abbreviation, i.event_id IS NULL, i.status, i.detail,
               i.reported_at, {_SOURCE}
        FROM injuries i JOIN players p ON p.id = i.player_id
        LEFT JOIN teams t ON t.id = p.team_id JOIN sources s ON s.id = i.source_id""",
    "results": f"""
        SELECT {_EVENT}, r.home_score, r.away_score, r.final_at, {_SOURCE}
        FROM results r JOIN events e ON e.id = r.event_id {_EVENT_JOIN}
        JOIN sources s ON s.id = r.source_id""",
    "player_game_stats": f"""
        SELECT {_EVENT}, p.full_name, g.stats::text, {_SOURCE}
        FROM player_game_stats g JOIN events e ON e.id = g.event_id {_EVENT_JOIN}
        JOIN players p ON p.id = g.player_id JOIN sources s ON s.id = g.source_id""",
    "players": """
        SELECT p.sport, p.full_name, t.abbreviation
        FROM players p LEFT JOIN teams t ON t.id = p.team_id""",
    "entity_review": "SELECT provider, entity_kind, provider_id, status FROM entity_review",
}


def snapshot(session: Session) -> dict[str, Counter[tuple[Any, ...]]]:
    return {
        table: Counter(tuple(row) for row in session.execute(text(sql)))
        for table, sql in SNAPSHOT_SQL.items()
    }


@pytest.fixture(scope="module")
def bundle(
    module_session_factory: sessionmaker[Session], tmp_path_factory: pytest.TempPathFactory
) -> tuple[Path, Any]:
    """One captured slate in DB A, exported once; tests only load it into fresh databases."""
    run_id = capture_slate(module_session_factory)
    with module_session_factory() as session:
        path = export_bundle(session, run_id, tmp_path_factory.mktemp("bundle"))
        return path, snapshot(session)


def test_captured_slate_is_non_trivial(bundle: tuple[Path, Any]) -> None:
    """Guards the round trip below against comparing two empty databases."""
    path, snap = bundle
    flags = Counter(row[12] for row in snap["odds_snapshots"].elements())  # is_closing
    assert flags[True] > 0 and flags[False] > 0
    assert {row[7] for row in snap["markets"] if row[5] == "player_points"} == {
        "Jayson Tatum",
        "Jaylen Brown",
    }
    assert {row[0] for row in snap["injuries"]} == {"Jayson Tatum", "Jock Landale"}
    assert sorted((r[0], r[3], r[4]) for r in snap["results"]) == [
        ("BOS", 111, 104),
        ("LAC", 99, 120),
    ]
    # R4: first observed final, including the one an hourly schedule refresh saw mid-capture
    assert {(r[0], r[5]) for r in snap["results"]} == {
        ("BOS", MID_CAPTURE_FINAL),
        ("LAC", FINAL_EARLY),
    }
    assert sum(snap["player_game_stats"].values()) == 4
    assert {(r[0], r[2]) for r in snap["events"]} == {("BOS", TIP1), ("LAC", ESPN_TIP2)}

    manifest, records = read_bundle(path)
    assert manifest.sport == "nba" and manifest.slate_date == SLATE
    assert manifest.counts["source"] > 0 and manifest.counts["observation"] > 0
    endpoints = {(r["provider"], r["endpoint"]) for r in records if r["kind"] == "source"}
    assert ("nba_stats", "leaguegamelog") in endpoints  # next-morning box scores ride along


def test_roundtrip_export_load_reparse_reproduces_typed_rows(
    bundle: tuple[Path, Any], other_db_session: Session
) -> None:
    path, snap_a = bundle
    report = load_bundle(other_db_session, path)

    manifest, _ = read_bundle(path)
    assert report.sources_inserted == manifest.counts["source"]
    assert report.sources_existing == 0
    assert report.observations == manifest.counts["observation"]
    assert report.reparsed.written > 0
    snap_b = snapshot(other_db_session)
    for table in SNAPSHOT_SQL:
        assert snap_b[table] == snap_a[table], table


def test_load_is_idempotent(bundle: tuple[Path, Any], other_db_session: Session) -> None:
    path, snap_a = bundle
    first = load_bundle(other_db_session, path)
    second = load_bundle(other_db_session, path)

    assert second.sources_inserted == 0
    assert second.sources_existing == first.sources_inserted
    assert second.observations == 0
    assert snapshot(other_db_session) == snap_a


def test_reparse_after_parser_fix_rebuilds_rows(
    bundle: tuple[Path, Any], other_db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, snap_a = bundle
    monkeypatch.setattr("sharpline.ingest.jobs.parse_injuries", lambda _payload: [])  # the bug
    load_bundle(other_db_session, path)
    assert sum(snapshot(other_db_session)["injuries"].values()) == 0

    monkeypatch.undo()  # the fix
    stats = reparse(other_db_session, provider="espn")
    assert stats.written > 0 and isinstance(stats, WriteStats)
    assert snapshot(other_db_session) == snap_a
