"""Provider clients for a capture session, built from Settings in one place.

The CLI (`sharpline capture`, `results`, `backfill`) and the live worker both build their
clients here, so a manual session and a live one talk to the providers the same way. The
Odds API client exists only when `ODDS_API_KEY` is set; without it the runner skips odds
actions with reason `no_api_key` (spec section 7).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import httpx
from sqlalchemy.orm import Session, sessionmaker

from sharpline.capture.quota import QuotaGuard
from sharpline.capture.runner import CaptureRunner
from sharpline.config import Settings
from sharpline.sources.espn.client import EspnClient
from sharpline.sources.http import CircuitBreaker, HttpClient, utcnow
from sharpline.sources.nba_stats.client import NbaStatsClient
from sharpline.sources.odds_api.client import OddsApiClient

ESPN_BASE_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
ODDS_API_BASE_URL = "https://api.the-odds-api.com/v4"
ESPN_MIN_INTERVAL = 0.5  # seconds between ESPN requests (keyless, be polite)
ODDS_MIN_INTERVAL = 1.0
SLEEP_CHUNK = 1.0  # seconds; short naps keep Ctrl-C and clock changes responsive

Clock = Callable[[], datetime]


@dataclass(frozen=True)
class CaptureClients:
    espn: EspnClient
    odds: OddsApiClient | None
    guard: QuotaGuard
    breaker: CircuitBreaker


def build_espn_client(
    settings: Settings, *, clock: Clock = utcnow, transport: httpx.BaseTransport | None = None
) -> EspnClient:
    http = HttpClient(
        ESPN_BASE_URL, min_interval=ESPN_MIN_INTERVAL, transport=transport, clock=clock
    )
    return EspnClient(http, clock=clock)


def build_odds_client(
    settings: Settings, *, clock: Clock = utcnow, transport: httpx.BaseTransport | None = None
) -> OddsApiClient | None:
    if settings.odds_api_key is None:
        return None
    http = HttpClient(
        ODDS_API_BASE_URL, min_interval=ODDS_MIN_INTERVAL, transport=transport, clock=clock
    )
    return OddsApiClient(settings.odds_api_key, http, clock=clock)


def build_nba_stats_client() -> NbaStatsClient:
    return NbaStatsClient()


def build_clients(
    settings: Settings,
    *,
    cap: int | None = None,
    clock: Clock = utcnow,
    espn_transport: httpx.BaseTransport | None = None,
    odds_transport: httpx.BaseTransport | None = None,
) -> CaptureClients:
    """Clients, quota guard and breaker for one session; `cap` overrides the session cap."""
    return CaptureClients(
        espn=build_espn_client(settings, clock=clock, transport=espn_transport),
        odds=build_odds_client(settings, clock=clock, transport=odds_transport),
        guard=QuotaGuard(
            session_cap=settings.capture_session_cap if cap is None else cap,
            floor=settings.capture_quota_floor,
        ),
        breaker=CircuitBreaker(clock=clock),
    )


def sleep_until(
    target: datetime,
    *,
    clock: Clock = utcnow,
    sleep: Callable[[float], None] = time.sleep,
    chunk: float = SLEEP_CHUNK,
) -> None:
    """Sleep until `target` in naps of at most `chunk` seconds, re-reading the clock each
    time: a laptop that suspends wakes up on schedule and Ctrl-C lands within a second."""
    while (left := (target - clock()).total_seconds()) > 0:
        sleep(min(left, chunk))


def build_runner(
    sessions: sessionmaker[Session], settings: Settings, *, cap: int | None = None
) -> CaptureRunner:
    """A capture runner on the real clock, with clients from Settings."""
    built = build_clients(settings, cap=cap)
    return CaptureRunner(
        sessions,
        espn=built.espn,
        odds=built.odds,
        guard=built.guard,
        breaker=built.breaker,
        clock=utcnow,
        sleep_until=sleep_until,
    )
