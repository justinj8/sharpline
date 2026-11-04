"""Odds arithmetic: American/decimal conversion, implied probability and de-vigging.

American odds are integers with |price| >= 100: +150 pays 150 per 100 staked,
-200 requires staking 200 to win 100. Values strictly between -100 and +100 are
not valid prices and are rejected rather than silently producing probabilities
outside [0, 1].
"""

from __future__ import annotations

from collections.abc import Sequence


class InvalidOddsError(ValueError):
    """Raised for prices that cannot exist in American or decimal notation."""


def validate_american(price: int) -> int:
    if -100 < price < 100:
        raise InvalidOddsError(f"American odds must satisfy |price| >= 100, got {price}")
    return price


def american_to_decimal(price: int) -> float:
    """Stake-inclusive decimal odds: +150 -> 2.5, -200 -> 1.5."""
    validate_american(price)
    return 1 + price / 100 if price > 0 else 1 + 100 / -price


def decimal_to_american(decimal_odds: float) -> int:
    """Inverse of `american_to_decimal`, rounded to the nearest integer price."""
    if decimal_odds <= 1.0:
        raise InvalidOddsError(f"decimal odds must be > 1.0, got {decimal_odds}")
    if decimal_odds >= 2.0:
        return round((decimal_odds - 1.0) * 100)
    return round(-100 / (decimal_odds - 1.0))


def implied_probability(price: int) -> float:
    """Break-even probability of a price, vig included: -110 -> 0.5238."""
    return 1 / american_to_decimal(price)


def devig_multiplicative(prices: Sequence[int]) -> list[float]:
    """Remove the bookmaker's margin from a complete set of outcome prices.

    `prices` are the American odds for every outcome of one market (both sides
    of a spread/total, or all outcomes of a 3-way market). The returned
    probabilities are in the same order and sum to 1.0.

    Multiplicative method: scale each implied probability by the same factor
    (their sum, the "overround").
    """
    if len(prices) < 2:
        raise ValueError("de-vigging needs every outcome of the market (at least 2 prices)")
    implied = [implied_probability(p) for p in prices]
    overround = sum(implied)
    return [p / overround for p in implied]
