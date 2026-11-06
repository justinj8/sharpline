"""Capture client factory: the one place the CLI and the live worker build providers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from sharpline.capture import clients
from sharpline.config import Settings
from sharpline.sources.odds_api.client import OddsApiClient


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"database_url": "postgresql://u:p@h/d", "odds_api_key": None}
    return Settings(_env_file=None, **(base | overrides))  # type: ignore[call-arg]


def _refuse(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected network call: {request.url.path}")


def test_build_clients_without_key_has_no_odds_client() -> None:
    built = clients.build_clients(_settings(), espn_transport=httpx.MockTransport(_refuse))
    assert built.odds is None
    assert built.guard.session_cap == 180 and built.guard.floor == 20


def test_build_clients_uses_settings_and_cap_override() -> None:
    settings = _settings(odds_api_key="sk-test", capture_session_cap=150, capture_quota_floor=30)
    built = clients.build_clients(
        settings,
        cap=90,
        espn_transport=httpx.MockTransport(_refuse),
        odds_transport=httpx.MockTransport(_refuse),
    )
    assert isinstance(built.odds, OddsApiClient)
    assert built.guard.session_cap == 90 and built.guard.floor == 30
    assert built.breaker.allow("odds_api")


def test_clients_hit_the_provider_base_urls() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url.copy_remove_param("apiKey")))
        if "the-odds-api" in request.url.host:
            return httpx.Response(200, json=[], headers={"x-requests-last": "0"})
        return httpx.Response(200, json={"events": []})

    built = clients.build_clients(
        _settings(odds_api_key="sk-test"),
        espn_transport=httpx.MockTransport(handler),
        odds_transport=httpx.MockTransport(handler),
    )
    built.espn.injuries()
    assert built.odds is not None
    built.odds.events()
    assert seen == [
        f"{clients.ESPN_BASE_URL}/injuries",
        f"{clients.ODDS_API_BASE_URL}/sports/basketball_nba/events",
    ]


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now
        self.naps: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.naps.append(seconds)
        self.now += timedelta(seconds=seconds)


def test_sleep_until_sleeps_in_small_chunks() -> None:
    clock = _Clock(datetime(2026, 11, 12, 23, 0, tzinfo=UTC))
    clients.sleep_until(clock.now + timedelta(seconds=2.5), clock=clock, sleep=clock.sleep)
    assert clock.naps == [1.0, 1.0, 0.5]


def test_sleep_until_past_target_returns_at_once() -> None:
    clock = _Clock(datetime(2026, 11, 12, 23, 0, tzinfo=UTC))
    clients.sleep_until(clock.now - timedelta(minutes=5), clock=clock, sleep=clock.sleep)
    assert clock.naps == []


def test_sleep_until_lets_keyboard_interrupt_through() -> None:
    clock = _Clock(datetime(2026, 11, 12, 23, 0, tzinfo=UTC))

    def interrupted(_s: float) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        clients.sleep_until(clock.now + timedelta(hours=1), clock=clock, sleep=interrupted)
