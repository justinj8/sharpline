"""ESPN NBA client. `http` must be based at .../apis/site/v2/sports/basketball/nba."""

from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from sharpline.sources.espn import PROVIDER
from sharpline.sources.fetched import Fetched
from sharpline.sources.http import HttpClient, utcnow

SCOREBOARD = "scoreboard"


def scoreboard_params(slate_date: date) -> dict[str, Any]:
    """The scoreboard request's params, as recorded in `sources.params`."""
    return {"dates": slate_date.strftime("%Y%m%d")}


class EspnClient:
    def __init__(self, http: HttpClient, clock: Callable[[], datetime] = utcnow) -> None:
        self._http = http
        self._clock = clock

    def scoreboard(self, slate_date: date) -> Fetched:
        return self._fetch(SCOREBOARD, scoreboard_params(slate_date))

    def injuries(self) -> Fetched:
        return self._fetch("injuries", {})

    def _fetch(self, endpoint: str, params: dict[str, Any]) -> Fetched:
        response = self._http.get(endpoint, params)
        return Fetched(
            provider=PROVIDER,
            endpoint=endpoint,
            params=params,
            payload=response.json(),
            fetched_at=self._clock(),
        )
