import pytest

from sharpline.domain.enums import MarketType, Period
from sharpline.sources.odds_api.markets import (
    GAME_MARKETS,
    NBA_PROP_MARKETS,
    MarketSpec,
    UnsupportedMarket,
    describe_market,
)


def test_constants() -> None:
    assert GAME_MARKETS == ("h2h", "spreads", "totals")
    assert NBA_PROP_MARKETS == (
        "player_points",
        "player_rebounds",
        "player_assists",
        "player_threes",
        "player_points_rebounds_assists",
        "player_blocks",
    )


def test_describe_prop_market() -> None:
    assert describe_market("player_points") == MarketSpec(
        MarketType.PLAYER_PROP, Period.FULL, "points"
    )


def test_describe_every_default_prop() -> None:
    stats = [describe_market(k).stat for k in NBA_PROP_MARKETS]
    assert stats == [
        "points",
        "rebounds",
        "assists",
        "threes",
        "points_rebounds_assists",
        "blocks",
    ]


def test_describe_game_markets() -> None:
    assert describe_market("h2h") == MarketSpec(MarketType.MONEYLINE, Period.FULL, None)
    assert describe_market("totals") == MarketSpec(MarketType.TOTAL, Period.FULL, None)


def test_describe_first_half_spread() -> None:
    assert describe_market("spreads_h1") == MarketSpec(MarketType.SPREAD, Period.H1, None)


def test_describe_first_quarter_prop() -> None:
    assert describe_market("player_points_q1") == MarketSpec(
        MarketType.PLAYER_PROP, Period.Q1, "points"
    )


@pytest.mark.parametrize("key", ["player_foo", "", "spreads_h3", "_h1"])
def test_unknown_market_raises(key: str) -> None:
    with pytest.raises(UnsupportedMarket):
        describe_market(key)
