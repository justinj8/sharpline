"""Odds API market keys mapped onto Sharpline's market vocabulary."""

from dataclasses import dataclass

from sharpline.domain.enums import MarketType, Period

GAME_MARKETS = ("h2h", "spreads", "totals")
NBA_PROP_MARKETS = (
    "player_points",
    "player_rebounds",
    "player_assists",
    "player_threes",
    "player_points_rebounds_assists",
    "player_blocks",
)

_GAME_TYPES = {
    "h2h": MarketType.MONEYLINE,
    "spreads": MarketType.SPREAD,
    "totals": MarketType.TOTAL,
}
_PROP_STATS = {
    "player_points": "points",
    "player_rebounds": "rebounds",
    "player_assists": "assists",
    "player_threes": "threes",
    "player_points_rebounds_assists": "points_rebounds_assists",
    "player_blocks": "blocks",
}
_SUFFIX_PERIODS = {"_h1": Period.H1, "_q1": Period.Q1}


class UnsupportedMarket(ValueError):
    """Raised for an Odds API market key Sharpline does not model."""


@dataclass(frozen=True)
class MarketSpec:
    market_type: MarketType
    period: Period
    stat: str | None


def describe_market(key: str) -> MarketSpec:
    base, period = key, Period.FULL
    for suffix, suffix_period in _SUFFIX_PERIODS.items():
        if key.endswith(suffix):
            base, period = key[: -len(suffix)], suffix_period
            break
    if base in _GAME_TYPES:
        return MarketSpec(_GAME_TYPES[base], period, None)
    if base in _PROP_STATS:
        return MarketSpec(MarketType.PLAYER_PROP, period, _PROP_STATS[base])
    raise UnsupportedMarket(key)
