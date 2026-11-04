from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from sharpline.sources.http import CircuitBreaker, HttpClient


def _client(
    responses: list[httpx.Response], slept: list[float], **kwargs: float
) -> tuple[HttpClient, list[httpx.Request]]:
    calls: list[httpx.Request] = []
    script = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return next(script)

    client = HttpClient(
        "https://example.test",
        transport=httpx.MockTransport(handler),
        sleep=slept.append,
        **kwargs,  # type: ignore[arg-type]
    )
    return client, calls


def test_retries_503_then_succeeds() -> None:
    slept: list[float] = []
    client, calls = _client(
        [httpx.Response(503), httpx.Response(503), httpx.Response(200, json={"ok": 1})], slept
    )
    resp = client.get("/x", {"a": "b"})
    assert resp.status_code == 200
    assert len(calls) == 3
    assert slept == [0.5, 1.0]


def test_honors_retry_after_header() -> None:
    slept: list[float] = []
    client, _ = _client(
        [httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200)], slept
    )
    assert client.get("/x", {}).status_code == 200
    assert slept == [7]


def test_non_integer_retry_after_falls_back_to_backoff() -> None:
    slept: list[float] = []
    client, _ = _client(
        [
            httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
            httpx.Response(200),
        ],
        slept,
    )
    client.get("/x", {})
    assert slept == [0.5]


def test_does_not_retry_401() -> None:
    slept: list[float] = []
    client, calls = _client([httpx.Response(401)], slept)
    with pytest.raises(httpx.HTTPStatusError):
        client.get("/x", {})
    assert len(calls) == 1
    assert slept == []


def test_gives_up_after_max_retries() -> None:
    slept: list[float] = []
    client, calls = _client([httpx.Response(503)] * 4, slept)
    with pytest.raises(httpx.HTTPStatusError):
        client.get("/x", {})
    assert len(calls) == 4
    assert slept == [0.5, 1.0, 2.0]


def test_retries_transport_errors() -> None:
    slept: list[float] = []
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200)

    client = HttpClient(
        "https://example.test", transport=httpx.MockTransport(handler), sleep=slept.append
    )
    assert client.get("/x", {}).status_code == 200
    assert slept == [0.5]


def test_min_interval_spaces_requests() -> None:
    slept: list[float] = []
    now = datetime(2026, 1, 1, tzinfo=UTC)
    client = HttpClient(
        "https://example.test",
        min_interval=1.0,
        transport=httpx.MockTransport(lambda r: httpx.Response(200)),
        sleep=slept.append,
        clock=lambda: now,
    )
    client.get("/x", {})
    assert slept == []
    client.get("/x", {})
    assert len(slept) == 1
    assert slept[0] == pytest.approx(1.0)


def _breaker() -> tuple[CircuitBreaker, Callable[[timedelta], None]]:
    state = {"now": datetime(2026, 1, 1, tzinfo=UTC)}

    def advance(delta: timedelta) -> None:
        state["now"] += delta

    return CircuitBreaker(clock=lambda: state["now"]), advance


def test_circuit_opens_after_three_failures_and_recovers() -> None:
    breaker, advance = _breaker()
    assert breaker.allow("odds")
    for _ in range(3):
        breaker.failure("odds")
    assert not breaker.allow("odds")
    assert breaker.allow("espn")  # per-provider
    advance(timedelta(minutes=10, seconds=1))
    assert breaker.allow("odds")


def test_success_resets_failure_count() -> None:
    breaker, _ = _breaker()
    breaker.failure("odds")
    breaker.failure("odds")
    breaker.success("odds")
    breaker.failure("odds")
    breaker.failure("odds")
    assert breaker.allow("odds")
