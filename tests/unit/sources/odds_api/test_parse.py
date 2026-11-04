import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from sharpline.domain.enums import MarketType, Period, Side, Sport
from sharpline.sources.odds_api.parse import parse_events, parse_odds

FIXTURES = Path(__file__).parents[3] / "fixtures" / "odds_api"
T0 = datetime(2026, 11, 4, 18, 0, tzinfo=UTC)


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def test_parse_events() -> None:
    rows = parse_events(load("nba_events.json"))
    assert [r.provider_event_id for r in rows] == ["evt1", "evt2"]
    r = rows[0]
    assert r.provider == "odds_api"
    assert r.sport == Sport.NBA
    assert (r.home_team, r.away_team) == ("Boston Celtics", "New York Knicks")
    assert r.commence_time == datetime(2026, 11, 5, 0, 0, tzinfo=UTC)


def test_parse_props_keeps_per_book_lines() -> None:
    rows = parse_odds(load("nba_event_props.json"), captured_at=T0)
    tatum = [r for r in rows if r.player_name == "Jayson Tatum" and r.side == Side.OVER]
    assert {(r.book, r.line, r.price) for r in tatum} == {
        ("draftkings", Decimal("27.5"), -115),
        ("fanduel", Decimal("28.5"), 100),
    }
    assert len(rows) == 4
    r = tatum[0]
    assert r.market_type == MarketType.PLAYER_PROP
    assert r.period == Period.FULL
    assert r.stat == "points"
    assert r.market_key == "player_points"
    assert r.provider_event_id == "evt1"
    assert r.captured_at == T0


def test_parse_game_odds_sides() -> None:
    rows = parse_odds(load("nba_game_odds.json"), captured_at=T0)
    evt1 = [r for r in rows if r.provider_event_id == "evt1" and r.book == "draftkings"]

    ml = {r.side: r for r in evt1 if r.market_type == MarketType.MONEYLINE}
    assert ml[Side.HOME].price == -230 and ml[Side.HOME].line is None
    assert ml[Side.AWAY].price == 190
    assert ml[Side.HOME].player_name is None and ml[Side.HOME].stat is None

    sp = {r.side: r for r in evt1 if r.market_type == MarketType.SPREAD}
    assert sp[Side.HOME].line == Decimal("-5.5")
    assert sp[Side.AWAY].line == Decimal("5.5")

    tot = {r.side: r for r in evt1 if r.market_type == MarketType.TOTAL}
    assert set(tot) == {Side.OVER, Side.UNDER}
    assert tot[Side.OVER].line == Decimal("224.5")

    # evt2: home/away are mapped per event, not globally
    evt2_ml = {
        r.side: r.price
        for r in rows
        if r.provider_event_id == "evt2" and r.book == "draftkings" and r.market_key == "h2h"
    }
    assert evt2_ml == {Side.HOME: 120, Side.AWAY: -142}


def test_invalid_price_dropped(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    rows = parse_odds(load("nba_game_odds.json"), captured_at=T0)
    assert not [r for r in rows if r.price == 50]
    # each event: 2 books x (2 + 2 + 2) = 12 outcomes, minus the one invalid price
    assert len([r for r in rows if r.provider_event_id == "evt1"]) == 11
    assert len([r for r in rows if r.provider_event_id == "evt2"]) == 11
    assert len(rows) == 22
    assert "50" in caplog.text


def test_unknown_team_outcome_dropped(caplog: pytest.LogCaptureFixture) -> None:
    payload = load("nba_game_odds.json")[0]
    payload["bookmakers"][0]["markets"][0]["outcomes"][0]["name"] = "Draw"
    caplog.set_level(logging.WARNING)
    rows = parse_odds(payload, captured_at=T0)
    dk_ml = [r for r in rows if r.book == "draftkings" and r.market_key == "h2h"]
    assert [r.side for r in dk_ml] == [Side.AWAY]
    assert "Draw" in caplog.text


def test_unsupported_market_skipped(caplog: pytest.LogCaptureFixture) -> None:
    payload = load("nba_event_props.json")
    payload["bookmakers"][0]["markets"][0]["key"] = "player_foo"
    caplog.set_level(logging.WARNING)
    rows = parse_odds(payload, captured_at=T0)
    assert {r.book for r in rows} == {"fanduel"}
    assert "player_foo" in caplog.text


def test_yes_no_outcomes() -> None:
    payload = load("nba_event_props.json")
    payload["bookmakers"] = [
        {
            "key": "draftkings",
            "title": "DraftKings",
            "last_update": "2026-11-04T18:00:00Z",
            "markets": [
                {
                    "key": "player_blocks",
                    "last_update": "2026-11-04T18:00:00Z",
                    "outcomes": [
                        {"name": "Yes", "description": "Kristaps Porzingis", "price": 150},
                        {"name": "No", "description": "Kristaps Porzingis", "price": -180},
                    ],
                }
            ],
        }
    ]
    rows = parse_odds(payload, captured_at=T0)
    assert {(r.side, r.line) for r in rows} == {(Side.YES, None), (Side.NO, None)}


def test_naive_captured_at_rejected() -> None:
    with pytest.raises(ValueError, match="naive"):
        parse_odds(load("nba_event_props.json"), captured_at=datetime(2026, 11, 4, 18, 0))


def test_rows_are_frozen() -> None:
    row = parse_events(load("nba_events.json"))[0]
    with pytest.raises(ValueError):
        row.home_team = "x"  # type: ignore[misc]
