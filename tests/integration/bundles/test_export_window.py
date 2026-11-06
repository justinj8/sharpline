"""Which sources an export picks up for a run (DB reads only: the shared rolled-back DB)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from sharpline.bundles.export import export_bundle
from sharpline.bundles.format import read_bundle
from sharpline.db.models import CaptureRun
from sharpline.ingest.observations import record_fetch
from sharpline.sources.fetched import Fetched

pytestmark = pytest.mark.integration

SLATE = date(2026, 11, 4)
STARTED = datetime(2026, 11, 4, 23, 0, tzinfo=UTC)
NEXT_EVENING = datetime(2026, 11, 5, 23, 30, tzinfo=UTC)  # the Nov 5 slate's capture


def _fetch(
    session: Session, at: datetime, endpoint: str, params: dict[str, Any], payload: Any
) -> None:
    provider = "nba_stats" if endpoint == "leaguegamelog" else "espn"
    record_fetch(session, Fetched(provider, endpoint, params, payload, at))


def test_unfinished_run_excludes_later_slates_and_unrelated_jobs(
    db_session: Session, tmp_path: Path
) -> None:
    """I2: a run killed without reaching `finally` keeps finished_at NULL; its window ends at
    06:00 ET the next morning, not at export time. M2: only the run's own free fetches
    (ESPN injuries) come from the window; a concurrent backfill does not ride along."""
    run = CaptureRun(
        sport="nba",
        slate_date=SLATE,
        mode="manual",
        started_at=STARTED,
        status="running",
        credits_cap=180,
        plan={},
    )
    db_session.add(run)
    db_session.flush()

    at = datetime(2026, 11, 4, 23, 30, tzinfo=UTC)
    _fetch(db_session, STARTED, "scoreboard", {"dates": "20261104"}, {"events": ["slate"]})
    _fetch(db_session, at, "injuries", {}, {"injuries": ["during the run"]})
    backfill = {"season": "2025-26", "player_or_team_abbreviation": "P"}
    _fetch(db_session, at, "leaguegamelog", backfill, {"resultSets": ["a whole season"]})
    _fetch(db_session, NEXT_EVENING, "injuries", {}, {"injuries": ["next day"]})
    _fetch(db_session, NEXT_EVENING, "scoreboard", {"dates": "20261105"}, {"events": ["next"]})

    path = export_bundle(db_session, run.id, tmp_path, now=datetime(2026, 11, 12, tzinfo=UTC))
    _, records = read_bundle(path)
    payloads = [r["payload"] for r in records if r["kind"] == "source"]
    assert payloads == [{"events": ["slate"]}, {"injuries": ["during the run"]}]
