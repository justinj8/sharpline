import pytest

from sharpline.domain.odds import (
    InvalidOddsError,
    american_to_decimal,
    decimal_to_american,
    devig_multiplicative,
    implied_probability,
)


@pytest.mark.parametrize(
    ("price", "dec", "prob"),
    [
        (150, 2.5, 0.4),
        (-200, 1.5, 2 / 3),
        (100, 2.0, 0.5),
        (-100, 2.0, 0.5),
        (-110, 1.909091, 0.523810),
    ],
)
def test_conversions(price: int, dec: float, prob: float) -> None:
    assert american_to_decimal(price) == pytest.approx(dec, abs=1e-6)
    assert implied_probability(price) == pytest.approx(prob, abs=1e-6)


@pytest.mark.parametrize("bad", [0, 50, -99, 99])
def test_invalid_american_odds_raise(bad: int) -> None:
    with pytest.raises(InvalidOddsError):
        implied_probability(bad)


def test_decimal_round_trip() -> None:
    assert decimal_to_american(2.5) == 150
    assert decimal_to_american(1.5) == -200
    with pytest.raises(InvalidOddsError):
        decimal_to_american(1.0)


def test_devig_symmetric_market() -> None:
    assert devig_multiplicative([-110, -110]) == pytest.approx([0.5, 0.5])


def test_devig_asymmetric_market_sums_to_one() -> None:
    probs = devig_multiplicative([-150, 130])
    assert sum(probs) == pytest.approx(1.0)
    assert probs == pytest.approx([0.579832, 0.420168], abs=1e-6)


def test_devig_requires_two_prices() -> None:
    with pytest.raises(ValueError):
        devig_multiplicative([-110])
