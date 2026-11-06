"""F8: only real provider failures count toward the circuit breaker."""

import httpx
import pytest

from sharpline.capture.runner import trips_breaker
from sharpline.sources.http import ProviderUnavailable
from sharpline.sources.odds_api.client import OddsApiError


def _status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://site.api.espn.com/scoreboard")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


@pytest.mark.parametrize("status", [401, 429, 500, 502, 503, 504])
def test_auth_rate_limit_and_server_errors_count(status: int) -> None:
    assert trips_breaker(OddsApiError(f"HTTP {status}", status=status))
    assert trips_breaker(_status_error(status))


@pytest.mark.parametrize("status", [400, 403, 404, 422])
def test_client_errors_such_as_a_delisted_event_do_not_count(status: int) -> None:
    assert not trips_breaker(OddsApiError(f"HTTP {status}", status=status))
    assert not trips_breaker(_status_error(status))


def test_transport_errors_and_open_circuits_count() -> None:
    assert trips_breaker(httpx.ConnectError("refused"))
    assert trips_breaker(httpx.ReadTimeout("slow"))
    assert trips_breaker(ProviderUnavailable("open"))
    assert trips_breaker(OddsApiError("no status known"))
