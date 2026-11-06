"""Shared HTTP plumbing for provider clients: retries, spacing and a circuit breaker."""

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def utcnow() -> datetime:
    return datetime.now(UTC)


class ProviderUnavailable(RuntimeError):
    """Raised when a provider's circuit is open or it keeps failing."""


class CircuitBreaker:
    """Per-provider breaker: opens after `threshold` consecutive failures for `cooldown`."""

    def __init__(
        self,
        threshold: int = 3,
        cooldown: timedelta = timedelta(minutes=10),
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._threshold = threshold
        self._cooldown = cooldown
        self._clock = clock
        self._failures: dict[str, int] = {}
        self._opened_at: dict[str, datetime] = {}

    def allow(self, provider: str) -> bool:
        opened = self._opened_at.get(provider)
        if opened is None:
            return True
        if self._clock() - opened >= self._cooldown:
            # Cooldown elapsed: close the circuit and give the provider a fresh start.
            del self._opened_at[provider]
            self._failures[provider] = 0
            return True
        return False

    def success(self, provider: str) -> None:
        self._failures[provider] = 0
        self._opened_at.pop(provider, None)

    def failure(self, provider: str) -> None:
        count = self._failures.get(provider, 0) + 1
        self._failures[provider] = count
        if count >= self._threshold:
            self._opened_at[provider] = self._clock()


class HttpClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 15.0,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        min_interval: float = 0.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._client = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._min_interval = min_interval
        self._sleep = sleep
        self._clock = clock
        self._last_request_at: datetime | None = None

    def _space_requests(self) -> None:
        if self._min_interval > 0 and self._last_request_at is not None:
            elapsed = (self._clock() - self._last_request_at).total_seconds()
            if elapsed < self._min_interval:
                self._sleep(self._min_interval - elapsed)
        self._last_request_at = self._clock()

    def _delay(self, response: httpx.Response | None, attempt: int) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after is not None:
                try:
                    return float(int(retry_after))
                except ValueError:
                    pass  # HTTP-date or junk: fall back to exponential backoff
        return float(self._backoff_base * 2**attempt)

    def get(self, path: str, params: dict[str, Any]) -> httpx.Response:
        for attempt in range(self._max_retries + 1):
            self._space_requests()
            response: httpx.Response | None = None
            try:
                response = self._client.get(path, params=params)
            except httpx.TransportError:
                if attempt == self._max_retries:
                    raise
            else:
                if response.status_code not in RETRYABLE_STATUS:
                    response.raise_for_status()
                    return response
                if attempt == self._max_retries:
                    response.raise_for_status()
            self._sleep(self._delay(response, attempt))
        raise AssertionError("unreachable")  # pragma: no cover
