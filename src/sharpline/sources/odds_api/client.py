"""Quota-aware Odds API client. The API key is sent but never recorded or logged."""

import logging
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

import httpx
from pydantic import SecretStr

from sharpline.sources.fetched import Fetched, QuotaInfo
from sharpline.sources.http import HttpClient, utcnow
from sharpline.sources.odds_api.markets import GAME_MARKETS

PROVIDER = "odds_api"
log = logging.getLogger(__name__)

# httpx logs full request URLs (including the apiKey query param) at INFO.
logging.getLogger("httpx").setLevel(logging.WARNING)


class OddsApiError(RuntimeError):
    """Odds API request failed; the message never contains the API key."""


def _int_header(headers: httpx.Headers, name: str) -> int | None:
    raw = headers.get(name)
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        return None


class OddsApiClient:
    def __init__(
        self,
        api_key: SecretStr,
        http: HttpClient,
        sport_key: str = "basketball_nba",
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._api_key = api_key
        self._http = http
        self._sport_key = sport_key
        self._clock = clock

    @staticmethod
    def estimate_cost(markets: int, regions: int = 1) -> int:
        return markets * regions

    def events(self) -> Fetched:
        return self._fetch(f"sports/{self._sport_key}/events", {})

    def game_odds(self) -> Fetched:
        return self._fetch(f"sports/{self._sport_key}/odds", self._odds_params(GAME_MARKETS))

    def event_odds(self, event_id: str, markets: Sequence[str]) -> Fetched:
        return self._fetch(
            f"sports/{self._sport_key}/events/{event_id}/odds", self._odds_params(markets)
        )

    @staticmethod
    def _odds_params(markets: Sequence[str]) -> dict[str, Any]:
        return {"regions": "us", "oddsFormat": "american", "markets": ",".join(markets)}

    def _fetch(self, endpoint: str, params: dict[str, Any]) -> Fetched:
        request_params = {**params, "apiKey": self._api_key.get_secret_value()}
        try:
            response = self._http.get(endpoint, request_params)
        except httpx.HTTPStatusError as exc:
            # str(exc) embeds the request URL, which carries the key.
            raise OddsApiError(
                f"odds api {endpoint} failed with HTTP {exc.response.status_code}"
            ) from None
        quota = QuotaInfo(
            last=_int_header(response.headers, "x-requests-last"),
            used=_int_header(response.headers, "x-requests-used"),
            remaining=_int_header(response.headers, "x-requests-remaining"),
        )
        log.info("odds api %s ok quota_remaining=%s", endpoint, quota.remaining)
        return Fetched(
            provider=PROVIDER,
            endpoint=endpoint,
            params=dict(params),
            payload=response.json(),
            fetched_at=self._clock(),
            quota=quota,
        )

    def __repr__(self) -> str:
        return f"OddsApiClient(sport_key={self._sport_key!r})"
