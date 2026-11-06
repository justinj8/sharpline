import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from sharpline.sources.fetched import QuotaInfo
from sharpline.sources.http import HttpClient
from sharpline.sources.odds_api.client import OddsApiClient, OddsApiError
from sharpline.sources.odds_api.markets import NBA_PROP_MARKETS

FIXTURES = Path(__file__).parents[3] / "fixtures" / "odds_api"
KEY = "sk-test-key"
T0 = datetime(2026, 11, 4, 18, 0, tzinfo=UTC)
QUOTA_HEADERS = {"x-requests-last": "6", "x-requests-used": "120", "x-requests-remaining": "380"}


def _client(
    fixture: str, headers: dict[str, str] | None = None, status: int = 200
) -> tuple[OddsApiClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []
    body: Any = json.loads((FIXTURES / fixture).read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=body, headers=headers or {})

    http = HttpClient(
        "https://api.the-odds-api.com/v4",
        transport=httpx.MockTransport(handler),
        sleep=lambda _s: None,
        max_retries=0,
    )
    return OddsApiClient(SecretStr(KEY), http, clock=lambda: T0), seen


def test_event_odds_parses_quota_headers_and_never_leaks_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    client, seen = _client("nba_event_props.json", QUOTA_HEADERS)
    f = client.event_odds("evt1", NBA_PROP_MARKETS)

    req = seen[0]
    assert req.url.path == "/v4/sports/basketball_nba/events/evt1/odds"
    q = dict(req.url.params)
    assert q["apiKey"] == KEY
    assert q["regions"] == "us"
    assert q["oddsFormat"] == "american"
    assert q["markets"] == ",".join(NBA_PROP_MARKETS)

    assert f.provider == "odds_api"
    assert f.endpoint == "sports/basketball_nba/events/evt1/odds"
    assert f.quota == QuotaInfo(last=6, used=120, remaining=380)
    assert f.fetched_at == T0
    assert f.payload["id"] == "evt1"
    assert f.params == {
        "regions": "us",
        "oddsFormat": "american",
        "markets": ",".join(NBA_PROP_MARKETS),
    }
    assert KEY not in caplog.text
    assert KEY not in str(f.params)
    assert KEY not in repr(client)


def test_game_odds_request_shape() -> None:
    client, seen = _client("nba_game_odds.json", QUOTA_HEADERS)
    f = client.game_odds()
    assert seen[0].url.path == "/v4/sports/basketball_nba/odds"
    assert dict(seen[0].url.params)["markets"] == "h2h,spreads,totals"
    assert f.endpoint == "sports/basketball_nba/odds"
    assert "apiKey" not in f.params
    assert len(f.payload) == 2
    assert "eventIds" not in seen[0].url.params


def test_game_odds_can_be_scoped_to_events() -> None:
    client, seen = _client("nba_game_odds.json", QUOTA_HEADERS)
    f = client.game_odds(["evt2", "evt3"])
    assert dict(seen[0].url.params)["eventIds"] == "evt2,evt3"
    assert f.params["eventIds"] == "evt2,evt3"


def test_events_without_quota_headers_gives_all_none_quota() -> None:
    client, seen = _client("nba_events.json")
    f = client.events()
    assert seen[0].url.path == "/v4/sports/basketball_nba/events"
    assert dict(seen[0].url.params) == {"apiKey": KEY}
    assert f.params == {}
    assert f.quota == QuotaInfo(last=None, used=None, remaining=None)


def test_http_error_message_redacts_key() -> None:
    client, _ = _client("nba_events.json", status=401)
    with pytest.raises(Exception) as exc:
        client.events()
    assert KEY not in str(exc.value)
    assert KEY not in repr(exc.value.__cause__)


def test_estimate_cost() -> None:
    assert OddsApiClient.estimate_cost(6) == 6
    assert OddsApiClient.estimate_cost(3, regions=2) == 6


@pytest.mark.parametrize("status", [401, 404, 422, 429, 500])
def test_http_error_carries_the_status(status: int) -> None:
    """F8: the runner decides from the status whether a failure counts toward the breaker."""
    client, _ = _client("nba_events.json", status=status)
    with pytest.raises(OddsApiError) as exc:
        client.events()
    assert exc.value.status == status
    assert f"HTTP {status}" in str(exc.value)
