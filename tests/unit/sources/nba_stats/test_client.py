import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from sharpline.sources.nba_stats import PROVIDER
from sharpline.sources.nba_stats.client import NbaStatsClient

FIXTURE = Path(__file__).parents[3] / "fixtures" / "nba_stats" / "league_game_log_players.json"
NOW = datetime(2026, 11, 5, 12, tzinfo=UTC)


class FakeEndpoint:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def get_dict(self) -> dict[str, Any]:
        return self._data


def make(calls: list[dict[str, Any]], sleeps: list[float] | None = None) -> NbaStatsClient:
    data = json.loads(FIXTURE.read_text())

    def factory(**kw: Any) -> FakeEndpoint:
        calls.append(kw)
        return FakeEndpoint(data)

    sink = sleeps if sleeps is not None else []
    return NbaStatsClient(endpoint_factory=factory, sleep=sink.append, clock=lambda: NOW)


def test_client_passes_season_and_dates_without_network() -> None:
    calls: list[dict[str, Any]] = []
    fetched = make(calls).player_game_logs("2025-26", date(2026, 11, 12), date(2026, 11, 12))
    assert calls[0]["season"] == "2025-26"
    assert calls[0]["player_or_team_abbreviation"] == "P"
    assert calls[0]["date_from_nullable"] == "11/12/2026"
    assert calls[0]["date_to_nullable"] == "11/12/2026"
    assert fetched.provider == PROVIDER == "nba_stats"
    assert fetched.endpoint == "leaguegamelog"
    assert fetched.params == calls[0]
    assert fetched.payload["resultSets"][0]["name"] == "LeagueGameLog"
    assert fetched.fetched_at == NOW
    assert fetched.quota is None


def test_client_omits_dates_when_not_given() -> None:
    calls: list[dict[str, Any]] = []
    make(calls).player_game_logs("2025-26")
    assert "date_from_nullable" not in calls[0]
    assert "date_to_nullable" not in calls[0]


def test_client_respects_min_interval_between_calls() -> None:
    calls: list[dict[str, Any]] = []
    sleeps: list[float] = []
    client = make(calls, sleeps)
    client.player_game_logs("2025-26")
    assert sleeps == []  # first call never waits
    client.player_game_logs("2025-26")
    assert sleeps == [1.0]
