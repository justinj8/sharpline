import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx

from sharpline.sources.espn import PROVIDER
from sharpline.sources.espn.client import EspnClient
from sharpline.sources.http import HttpClient

FIXTURES = Path(__file__).parents[3] / "fixtures" / "espn"
BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
T0 = datetime(2026, 11, 4, 18, 0, tzinfo=UTC)


def _client(fixture: str) -> tuple[EspnClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []
    body: Any = json.loads((FIXTURES / fixture).read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=body)

    http = HttpClient(
        BASE, transport=httpx.MockTransport(handler), sleep=lambda _s: None, max_retries=0
    )
    return EspnClient(http, clock=lambda: T0), seen


def test_client_formats_date_param() -> None:
    client, seen = _client("nba_scoreboard.json")
    fetched = client.scoreboard(date(2026, 11, 12))
    assert seen[0].url.params["dates"] == "20261112"
    assert seen[0].url.path.endswith("/basketball/nba/scoreboard")
    assert fetched.provider == PROVIDER == "espn"
    assert fetched.endpoint == "scoreboard"
    assert fetched.params == {"dates": "20261112"}
    assert fetched.fetched_at == T0
    assert fetched.quota is None
    assert len(fetched.payload["events"]) == 2


def test_client_injuries() -> None:
    client, seen = _client("nba_injuries.json")
    fetched = client.injuries()
    assert seen[0].url.path.endswith("/basketball/nba/injuries")
    assert fetched.endpoint == "injuries"
    assert fetched.params == {}
    assert len(fetched.payload["injuries"]) == 2
