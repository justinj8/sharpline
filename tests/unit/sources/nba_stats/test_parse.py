import copy
import json
import logging
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from sharpline.sources.nba_stats.parse import parse_player_game_logs

FIXTURE = Path(__file__).parents[3] / "fixtures" / "nba_stats" / "league_game_log_players.json"


def load() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text())
    return data


def test_fixture_is_trimmed() -> None:
    assert len(load()["resultSets"][0]["rowSet"]) <= 5


def test_parse_maps_headers_and_derives_pra() -> None:
    rows = parse_player_game_logs(load())
    assert len(rows) == 5
    first = rows[0]
    assert first.provider_game_id == "0022500127"
    assert first.provider_player_id == "1627741"
    assert first.player_name == "Buddy Hield"
    assert first.team_abbr == "GSW"
    assert first.game_date == date(2025, 10, 28)
    assert first.matchup == "GSW vs. LAC"
    assert first.stats == {
        "points": 2.0,
        "rebounds": 3.0,
        "assists": 0.0,
        "threes": 0.0,
        "blocks": 0.0,
        "steals": 0.0,
        "turnovers": 0.0,
        "minutes": 10.0,
        "points_rebounds_assists": 5.0,
    }
    adebayo = rows[2]
    assert adebayo.stats["points_rebounds_assists"] == 26 + 6 + 2
    assert adebayo.stats["threes"] == 3.0
    assert all(isinstance(v, float) for v in adebayo.stats.values())


def test_parse_minutes_string_form() -> None:
    payload = load()
    idx = payload["resultSets"][0]["headers"].index("MIN")
    payload["resultSets"][0]["rowSet"][0][idx] = "34:12"
    rows = parse_player_game_logs(payload)
    assert rows[0].stats["minutes"] == pytest.approx(34 + 12 / 60)


def test_parse_ignores_column_order() -> None:
    payload = load()
    rs = payload["resultSets"][0]
    order = list(reversed(range(len(rs["headers"]))))
    rs["headers"] = [rs["headers"][i] for i in order]
    rs["rowSet"] = [[row[i] for i in order] for row in rs["rowSet"]]
    assert parse_player_game_logs(payload) == parse_player_game_logs(load())


def test_malformed_row_is_skipped_and_others_survive(caplog: pytest.LogCaptureFixture) -> None:
    payload = copy.deepcopy(load())
    rs = payload["resultSets"][0]
    pts = rs["headers"].index("PTS")
    rs["rowSet"][1][pts] = None  # float(None) -> TypeError
    del rs["rowSet"][3][5:]  # short row -> IndexError
    with caplog.at_level(logging.WARNING):
        rows = parse_player_game_logs(payload)
    assert [r.player_name for r in rows] == ["Buddy Hield", "Bam Adebayo", "Landry Shamet"]
    assert "TypeError" in caplog.text
    assert "IndexError" in caplog.text


def test_empty_or_missing_result_set() -> None:
    assert parse_player_game_logs({"resultSets": []}) == []
    payload = load()
    payload["resultSets"][0]["rowSet"] = []
    assert parse_player_game_logs(payload) == []
