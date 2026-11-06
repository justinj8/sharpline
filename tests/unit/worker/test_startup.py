"""Worker startup: orphaned live runs are recovered only in live mode (M2).

With live capture off the worker must not touch `capture_runs` at all: it has no
pre-deploy step, so on a fresh deploy that table may not exist until the api migrates.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

import sharpline_worker.__main__ as worker_main
from sharpline.config import Settings


class _App:
    @asynccontextmanager
    async def open_async(self) -> AsyncIterator[None]:
        yield

    async def run_worker_async(self, **_options: Any) -> None:
        return None


def _start(monkeypatch: pytest.MonkeyPatch, *, live: bool) -> list[Any]:
    recovered: list[Any] = []
    settings = Settings(database_url="postgresql://u:p@h/d", env="test", live_capture=live)
    monkeypatch.setattr(worker_main, "get_settings", lambda: settings)
    monkeypatch.setattr(worker_main, "configure_logging", lambda *_a, **_kw: None)
    monkeypatch.setattr(worker_main, "make_engine", lambda _settings: object())
    monkeypatch.setattr(worker_main, "make_session_factory", lambda _engine: "sessions")
    monkeypatch.setattr(worker_main, "build_app", lambda *_a, **_kw: _App())
    monkeypatch.setattr(worker_main, "recover_orphaned_runs", recovered.append)
    asyncio.run(worker_main._run())
    return recovered


def test_startup_skips_recovery_when_live_capture_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _start(monkeypatch, live=False) == []


def test_startup_recovers_orphaned_runs_in_live_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _start(monkeypatch, live=True) == ["sessions"]
