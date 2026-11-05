"""nba_api LeagueGameLog client.

nba_api brings its own HTTP stack, so this client deliberately does not use HttpClient.
`endpoint_factory` is injectable so tests never touch the network.
"""

import time
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from nba_api.stats.endpoints import LeagueGameLog  # type: ignore[import-untyped]

from sharpline.sources.fetched import Fetched
from sharpline.sources.http import utcnow
from sharpline.sources.nba_stats import PROVIDER

ENDPOINT = "leaguegamelog"


def _fmt(d: date) -> str:
    return d.strftime("%m/%d/%Y")


class NbaStatsClient:
    def __init__(
        self,
        endpoint_factory: Callable[..., Any] = LeagueGameLog,
        sleep: Callable[[float], None] = time.sleep,
        min_interval: float = 1.0,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._factory = endpoint_factory
        self._sleep = sleep
        self._min_interval = min_interval
        self._clock = clock
        self._called = False

    def player_game_logs(
        self, season: str, date_from: date | None = None, date_to: date | None = None
    ) -> Fetched:
        params: dict[str, Any] = {"season": season, "player_or_team_abbreviation": "P"}
        if date_from is not None:
            params["date_from_nullable"] = _fmt(date_from)
        if date_to is not None:
            params["date_to_nullable"] = _fmt(date_to)
        # stats.nba.com throttles aggressively: pause between successive calls.
        if self._called:
            self._sleep(self._min_interval)
        self._called = True
        payload = self._factory(**params).get_dict()
        return Fetched(
            provider=PROVIDER,
            endpoint=ENDPOINT,
            params=params,
            payload=payload,
            fetched_at=self._clock(),
        )
