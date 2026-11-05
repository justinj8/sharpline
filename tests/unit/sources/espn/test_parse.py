import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from sharpline.domain.enums import Sport
from sharpline.sources.espn.parse import parse_injuries, parse_scoreboard

FIXTURES = Path(__file__).parents[3] / "fixtures" / "espn"


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def test_scoreboard_events_use_abbreviations_and_utc() -> None:
    events, _ = parse_scoreboard(_load("nba_scoreboard.json"))
    assert len(events) == 2
    first = events[0]
    assert first.provider == "espn"
    assert first.provider_event_id == "401909922"
    assert first.sport is Sport.NBA
    assert (first.home_team, first.away_team) == ("DET", "PHI")
    assert first.commence_time == datetime(2026, 11, 5, 0, 0, tzinfo=UTC)
    assert first.commence_time.utcoffset() is not None
    assert first.commence_time.utcoffset() == timedelta(0)


def test_scoreboard_results_only_for_finals() -> None:
    _, scheduled = parse_scoreboard(_load("nba_scoreboard.json"))
    assert scheduled == []
    events, results = parse_scoreboard(_load("nba_scoreboard_final.json"))
    assert len(events) == 2
    assert len(results) == 2
    first = results[0]
    assert first.provider_event_id == "401909907"
    assert (first.home_score, first.away_score) == (112, 117)  # CLE home, OKC away
    assert first.final_at is None  # writer stamps it from fetched_at (R4)


def test_injuries_map_status_and_team() -> None:
    rows = parse_injuries(_load("nba_injuries.json"))
    by_name = {r.player_name: r for r in rows}
    landale = by_name["Jock Landale"]
    assert landale.team_provider_id == "1"
    assert landale.status == "out"
    assert landale.reported_at == datetime(2026, 11, 3, 22, 8, tzinfo=UTC)
    assert landale.detail is not None and landale.detail.startswith("Landale is out")
    assert by_name["Jalen Johnson"].status == "questionable"  # Day-To-Day
    assert by_name["Jayson Tatum"].team_provider_id == "2"


def test_injury_detail_falls_back_to_long_comment_then_none() -> None:
    payload = {
        "injuries": [
            {
                "id": "9",
                "injuries": [
                    {
                        "status": "Out",
                        "date": "2026-11-03T22:08Z",
                        "longComment": "long",
                        "athlete": {"displayName": "A"},
                    },
                    {"status": "Probable", "athlete": {"displayName": "B"}},
                ],
            }
        ]
    }
    a, b = parse_injuries(payload)
    assert a.detail == "long"
    assert b.detail is None and b.reported_at is None and b.status == "probable"


def test_unknown_injury_status_logged_and_dropped(caplog: pytest.LogCaptureFixture) -> None:
    payload = {
        "injuries": [
            {
                "id": "9",
                "injuries": [
                    {"status": "Suspension", "athlete": {"displayName": "A"}},
                    {"status": "DOUBTFUL", "athlete": {"displayName": "B"}},
                ],
            }
        ]
    }
    with caplog.at_level(logging.WARNING):
        rows = parse_injuries(payload)
    assert [r.player_name for r in rows] == ["B"]
    assert rows[0].status == "doubtful"
    assert "Suspension" in caplog.text


def test_malformed_event_is_skipped_not_fatal(caplog: pytest.LogCaptureFixture) -> None:
    payload = _load("nba_scoreboard_final.json")
    good_id = payload["events"][1]["id"]
    bad = payload["events"][0]
    bad["competitions"][0]["competitors"][0]["score"] = ""  # final with empty score
    with caplog.at_level(logging.WARNING):
        events, results = parse_scoreboard(payload)
    assert [e.provider_event_id for e in events] == [good_id]
    assert [r.provider_event_id for r in results] == [good_id]
    assert "malformed" in caplog.text and "ValueError" in caplog.text


def test_event_without_competitions_is_skipped(caplog: pytest.LogCaptureFixture) -> None:
    payload = _load("nba_scoreboard.json")
    del payload["events"][0]["competitions"]
    with caplog.at_level(logging.WARNING):
        events, _ = parse_scoreboard(payload)
    assert len(events) == 1
    assert "KeyError" in caplog.text


def test_injury_entry_without_athlete_is_skipped(caplog: pytest.LogCaptureFixture) -> None:
    payload = _load("nba_injuries.json")
    total = sum(len(t["injuries"]) for t in payload["injuries"])
    del payload["injuries"][0]["injuries"][0]["athlete"]
    with caplog.at_level(logging.WARNING):
        rows = parse_injuries(payload)
    assert len(rows) == total - 1
    assert "malformed" in caplog.text and "KeyError" in caplog.text


def test_team_with_null_injuries_is_skipped(caplog: pytest.LogCaptureFixture) -> None:
    payload = _load("nba_injuries.json")
    kept = len(payload["injuries"][1]["injuries"])
    payload["injuries"][0]["injuries"] = None
    with caplog.at_level(logging.WARNING):
        rows = parse_injuries(payload)
    assert len(rows) == kept
    assert {r.team_provider_id for r in rows} == {"2"}
    assert "team 1 malformed" in caplog.text


def test_non_dict_injury_entry_is_skipped(caplog: pytest.LogCaptureFixture) -> None:
    payload = _load("nba_injuries.json")
    total = sum(len(t["injuries"]) for t in payload["injuries"])
    payload["injuries"][0]["injuries"][0] = "garbage"
    with caplog.at_level(logging.WARNING):
        rows = parse_injuries(payload)
    assert len(rows) == total - 1
    assert "AttributeError" in caplog.text
