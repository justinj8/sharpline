"""`sharpline` CLI against a private NBA-seeded database and MockTransport providers."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from functools import partial
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from sharpline.bundles.format import write_bundle
from sharpline.capture import clients
from sharpline.capture.quota import QuotaGuard
from sharpline.capture.runner import CaptureRunner
from sharpline.cli import app
from sharpline.cli import capture as cli_capture
from sharpline.config import Settings
from sharpline.db.models import (
    CaptureRun,
    EntityReview,
    EntityXref,
    Event,
    Player,
    QuotaLedger,
    Source,
    Team,
)
from sharpline.ingest import jobs
from sharpline.sources.espn.client import EspnClient
from sharpline.sources.http import CircuitBreaker, HttpClient
from sharpline.sources.nba_stats.client import NbaStatsClient

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parent.parent / "fixtures"
SLATE = date(2026, 11, 12)
TIP1 = datetime(2026, 11, 13, 0, 0, tzinfo=UTC)  # 19:00 ET
TIP2 = datetime(2026, 11, 13, 2, 30, tzinfo=UTC)  # 21:30 ET
NOW = datetime(2026, 11, 12, 22, 0, tzinfo=UTC)  # 17:00 ET
GAMES = {"e1": (TIP1, "BOS", "NY"), "e2": (TIP2, "LAC", "DEN")}

runner = CliRunner()


def _scoreboard(games: dict[str, tuple[datetime, str, str]]) -> dict[str, Any]:
    return {
        "events": [
            {
                "id": key,
                "date": tip.strftime("%Y-%m-%dT%H:%MZ"),
                "status": {"type": {"name": "STATUS_SCHEDULED"}},
                "competitions": [
                    {
                        "competitors": [
                            {"homeAway": "home", "team": {"abbreviation": home}},
                            {"homeAway": "away", "team": {"abbreviation": away}},
                        ]
                    }
                ],
            }
            for key, (tip, home, away) in games.items()
        ]
    }


class Espn:
    """ESPN served from memory; records every path it is asked for."""

    def __init__(self, games: dict[str, tuple[datetime, str, str]]) -> None:
        self.games = games
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path.rsplit("/", 1)[-1])
        if request.url.path.endswith("/injuries"):
            return httpx.Response(200, json={"injuries": []})
        return httpx.Response(200, json=_scoreboard(self.games))

    def client(self, clock: Callable[[], datetime] = lambda: NOW) -> EspnClient:
        http = HttpClient(
            clients.ESPN_BASE_URL,
            transport=httpx.MockTransport(self),
            sleep=lambda _s: None,
            max_retries=0,
        )
        return EspnClient(http, clock=clock)


def _forbidden(name: str) -> Callable[..., Any]:
    def build(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError(f"{name} must not be built here")

    return build


@pytest.fixture
def db(
    db_session_factory: sessionmaker[Session], cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> sessionmaker[Session]:
    """The CLI's DATABASE_URL points at this test's private database."""
    bind = db_session_factory.kw["bind"]
    monkeypatch.setenv("DATABASE_URL", bind.url.render_as_string(hide_password=False))
    monkeypatch.setattr(cli_capture, "clock", lambda: NOW)
    # No provider client may be built unless a test opts in.
    for name in ("build_espn_client", "build_clients", "build_runner", "build_nba_stats_client"):
        monkeypatch.setattr(clients, name, _forbidden(name))
    return db_session_factory


def _seed_slate(db: sessionmaker[Session]) -> None:
    with db() as session:
        jobs.ingest_schedule(session, Espn(GAMES).client(), SLATE)


def _count(db: sessionmaker[Session], model: type[Any]) -> int:
    with db() as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _invoke(*args: str) -> Any:
    result = runner.invoke(app, list(args))
    assert "Traceback" not in result.output
    return result


# -- capture ------------------------------------------------------------------------------


def test_dry_run_offline_plans_stored_events(db: sessionmaker[Session]) -> None:
    _seed_slate(db)
    sources = _count(db, Source)
    result = _invoke("capture", "nba", "--date", "2026-11-12", "--dry-run", "--offline")
    assert result.exit_code == 0, result.output
    out = result.output
    assert "est. credits" in out and "closing" in out
    assert "18:57" in out and "21:27" in out  # T-3 closings in ET
    assert "NYK@BOS" in out and "DEN@LAC" in out
    # start: game lines 3 + props 12; t-90: game lines 3, props 6 + 6; closing 9 + 9;
    # hourly game lines at 18:00, 19:00, 20:00 and 21:00 ET (3 each).
    assert "total est. credits: 60 / cap 180" in out
    assert "dropped: none" in out
    assert _count(db, CaptureRun) == 0 and _count(db, Source) == sources


def test_dry_run_cap_override_lists_dropped_actions(db: sessionmaker[Session]) -> None:
    _seed_slate(db)
    result = _invoke(
        "capture", "nba", "--date", "2026-11-12", "--dry-run", "--offline", "--cap", "40"
    )
    assert result.exit_code == 0, result.output
    assert "/ cap 40" in result.output
    assert "dropped (budget):" in result.output and "hourly" in result.output


def test_dry_run_fetches_only_the_espn_schedule(
    db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    espn = Espn(GAMES)
    monkeypatch.setattr(
        clients,
        "build_espn_client",
        lambda settings: espn.client(),
    )
    result = _invoke("capture", "nba", "--date", "2026-11-12", "--dry-run")
    assert result.exit_code == 0, result.output
    assert espn.calls == ["scoreboard"]  # the schedule, nothing else; no Odds API client
    assert "closing" in result.output and "NYK@BOS" in result.output
    assert _count(db, Event) == 2  # stored through the normal ingest path
    assert _count(db, CaptureRun) == 0


def test_dry_run_with_no_games(db: sessionmaker[Session]) -> None:
    result = _invoke("capture", "nba", "--date", "2026-11-12", "--dry-run", "--offline")
    assert result.exit_code == 0, result.output
    assert "no scheduled games stored for 2026-11-12" in result.output


class FakeClock:
    def __init__(self, now: datetime, *, interrupt: bool = False) -> None:
        self.now = now
        self.interrupt = interrupt

    def __call__(self) -> datetime:
        return self.now

    def sleep_until(self, at: datetime) -> None:
        if self.interrupt:
            raise KeyboardInterrupt
        self.now = max(self.now, at)


def _fake_runner(clock: FakeClock, espn: Espn) -> Callable[..., CaptureRunner]:
    def build(
        sessions: sessionmaker[Session], settings: Settings, *, cap: int | None = None
    ) -> CaptureRunner:
        return CaptureRunner(
            sessions,
            espn=espn.client(clock),
            odds=None,
            guard=QuotaGuard(cap or settings.capture_session_cap, settings.capture_quota_floor),
            breaker=CircuitBreaker(clock=clock),
            clock=clock,
            sleep_until=clock.sleep_until,
        )

    return build


def test_capture_runs_a_manual_session(
    db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(clients, "build_runner", _fake_runner(FakeClock(NOW), Espn(GAMES)))
    result = _invoke("capture", "nba", "--date", "2026-11-12", "--cap", "90")
    assert result.exit_code == 0, result.output
    with db() as session:
        run = session.scalars(select(CaptureRun)).one()
    assert (run.mode, run.status, run.credits_cap) == ("manual", "completed", 90)
    out = result.output
    assert "warning: ODDS_API_KEY is not set: odds actions will be skipped" in out
    assert f"capture_run_id: {run.id}" in out
    assert "status: completed" in out and "credits: 0 / 90" in out
    assert "skipped" in out  # odds actions: no_api_key


def test_interrupted_capture_exports_a_partial_bundle(
    db: sessionmaker[Session], cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(NOW, interrupt=True)
    monkeypatch.setattr(clients, "build_runner", _fake_runner(clock, Espn(GAMES)))
    result = _invoke("capture", "nba", "--date", "2026-11-12")
    assert result.exit_code == 130, result.output
    with db() as session:
        run = session.scalars(select(CaptureRun)).one()
    assert run.status == "interrupted"
    bundles = list((cli_env / "var" / "bundles").glob("nba-2026-11-12-*.jsonl.gz"))
    assert len(bundles) == 1
    assert f"interrupted: capture_run_id {run.id}" in result.output
    assert f"partial bundle: {Path('var/bundles') / bundles[0].name}" in result.output


# -- results and backfill -----------------------------------------------------------------


class NbaStats:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.payload = json.loads((FIXTURES / "nba_stats/league_game_log_players.json").read_text())

    def __call__(self, **params: Any) -> Any:
        self.calls.append(params)
        payload = self.payload

        class _Endpoint:
            def get_dict(self) -> Any:
                return payload

        return _Endpoint()

    def client(self) -> NbaStatsClient:
        return NbaStatsClient(endpoint_factory=self, sleep=lambda _s: None, clock=lambda: NOW)


def test_results_ingests_finals_and_box_scores(
    db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    espn, nba = Espn(GAMES), NbaStats()
    monkeypatch.setattr(clients, "build_espn_client", lambda settings: espn.client())
    monkeypatch.setattr(clients, "build_nba_stats_client", lambda: nba.client())
    result = _invoke("results", "nba", "--date", "2026-11-12")
    assert result.exit_code == 0, result.output
    assert espn.calls == ["scoreboard"]
    assert nba.calls[0]["season"] == "2026-27"
    assert nba.calls[0]["date_from_nullable"] == "11/12/2026"
    assert "results: written" in result.output and "player stats: written" in result.output


def test_backfill_fetches_each_season(
    db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    nba = NbaStats()
    monkeypatch.setattr(clients, "build_nba_stats_client", lambda: nba.client())
    result = _invoke("backfill", "nba-stats", "--seasons", "2024-25", "2025-26")
    assert result.exit_code == 0, result.output
    assert [c["season"] for c in nba.calls] == ["2024-25", "2025-26"]
    assert all("date_from_nullable" not in c for c in nba.calls)
    assert "2024-25: written" in result.output and "2025-26: written" in result.output
    assert _count(db, Source) == 2  # one source per season


def test_reparse_passes_provider_and_source_ids(db: sessionmaker[Session]) -> None:
    _seed_slate(db)
    with db() as session:
        source_id = session.scalars(select(Source.id)).one()
    for args in (["--provider", "espn"], ["--source-id", str(source_id)], []):
        result = _invoke("reparse", *args)
        assert result.exit_code == 0, result.output
        assert result.output.startswith("reparse: written ")
    nothing = _invoke("reparse", "--provider", "odds_api")
    assert nothing.output.strip() == "reparse: written 0, skipped 0, reviews 0"


# -- bundles ------------------------------------------------------------------------------


def _bundle(out: Path, slate: date, tag: str) -> Path:
    source = {
        "kind": "source",
        "ref": 0,
        "provider": "cli_test",  # no parser: reparse skips it
        "endpoint": "/order",
        "params": {},
        "payload": {"tag": tag},
        "fetched_at": "2026-11-04T23:00:00+00:00",
    }
    return write_bundle(
        out,
        sport="nba",
        slate_date=slate,
        capture_run_id=1,
        alembic_revision=None,
        records=[source],
    )


def test_bundle_load_runs_in_slate_date_order(db: sessionmaker[Session], cli_env: Path) -> None:
    late = _bundle(cli_env / "in", date(2026, 11, 6), "late")
    early = _bundle(cli_env / "in", date(2026, 11, 4), "early")
    ties = {_bundle(cli_env / "in", date(2026, 11, 5), tag): tag for tag in ("x", "y")}
    tie_a, tie_b = sorted(ties, key=lambda p: p.name)  # same slate date: by file name
    result = _invoke("bundle", "load", str(late), str(tie_b), str(early), str(tie_a))
    assert result.exit_code == 0, result.output
    loaded = [line.split()[1] for line in result.output.splitlines() if line.startswith("loaded")]
    assert loaded == [early.name, tie_a.name, tie_b.name, late.name]
    assert "sources 1 new / 0 existing" in result.output
    with db() as session:
        tags = session.scalars(
            select(Source.payload["tag"].astext)
            .where(Source.provider == "cli_test")
            .order_by(Source.id)
        ).all()
    assert tags == ["early", ties[tie_a], ties[tie_b], "late"]


def test_bundle_export_writes_to_var_bundles(db: sessionmaker[Session], cli_env: Path) -> None:
    with db.begin() as session:
        run = CaptureRun(
            sport="nba",
            slate_date=SLATE,
            mode="manual",
            started_at=NOW,
            finished_at=NOW,
            status="completed",
            credits_cap=180,
            plan={},
        )
        session.add(run)
        session.flush()
        run_id = run.id
    result = _invoke("bundle", "export", str(run_id))
    assert result.exit_code == 0, result.output
    (path,) = (cli_env / "var" / "bundles").glob("nba-2026-11-12-*.jsonl.gz")
    assert result.output.strip() == f"exported {Path('var/bundles') / path.name}"
    custom = _invoke("bundle", "export", str(run_id), "--out", "elsewhere")
    assert custom.exit_code == 0 and (cli_env / "elsewhere" / path.name).is_file()
    missing = _invoke("bundle", "export", "999999")
    assert (missing.exit_code, missing.output.strip()) == (
        1,
        "error: capture run 999999 not found",
    )


# -- review -------------------------------------------------------------------------------


@pytest.fixture
def reviews(db: sessionmaker[Session]) -> tuple[int, int, int]:
    """(open review id, resolved review id, candidate player id)."""
    with db.begin() as session:
        team_id = session.scalars(select(Team.id).where(Team.abbreviation == "BOS")).one()
        player = Player(sport="nba", full_name="Jaylen Brown", team_id=team_id)
        session.add(player)
        session.flush()
        open_review = EntityReview(
            provider="espn",
            provider_id="2:Jaylen Brwn",
            entity_kind="player",
            raw_name="Jaylen Brwn",
            context={"sport": "nba", "team_id": team_id},
            candidates=[{"id": player.id, "name": "Jaylen Brown", "score": 95.2}],
        )
        done = EntityReview(
            provider="espn",
            provider_id="2:Old Name",
            entity_kind="player",
            raw_name="Old Name",
            context={},
            candidates=[],
            status="resolved",
        )
        session.add_all([open_review, done])
        session.flush()
        ids = open_review.id, done.id, player.id
    return ids


def test_review_list_shows_open_reviews(reviews: tuple[int, int, int]) -> None:
    open_id, _done_id, player_id = reviews
    result = _invoke("review", "list")
    assert result.exit_code == 0, result.output
    assert str(open_id) in result.output and "Jaylen Brwn" in result.output
    assert f"{player_id}:Jaylen Brown (95)" in result.output
    assert "Old Name" not in result.output


def test_review_list_when_empty(db: sessionmaker[Session]) -> None:
    assert _invoke("review", "list").output.strip() == "no open reviews"


def test_review_resolve_creates_the_xref(
    db: sessionmaker[Session], reviews: tuple[int, int, int]
) -> None:
    open_id, _done_id, player_id = reviews
    result = _invoke("review", "resolve", str(open_id), str(player_id))
    assert result.exit_code == 0, result.output
    assert result.output.strip() == f"review {open_id} resolved -> player {player_id}"
    with db() as session:
        review = session.get_one(EntityReview, open_id)
        assert (review.status, review.resolved_entity_id) == ("resolved", player_id)
        assert (
            session.scalar(
                select(EntityXref.entity_id).where(
                    EntityXref.provider == "espn", EntityXref.provider_id == "2:Jaylen Brwn"
                )
            )
            == player_id
        )


def test_review_resolve_rejects_unknown_entity(reviews: tuple[int, int, int]) -> None:
    open_id, _done_id, _player_id = reviews
    result = _invoke("review", "resolve", str(open_id), "999999")
    assert (result.exit_code, result.output.strip()) == (1, "error: player 999999 not found")


def test_review_reject(db: sessionmaker[Session], reviews: tuple[int, int, int]) -> None:
    open_id, done_id, _player_id = reviews
    result = _invoke("review", "reject", str(open_id))
    assert (result.exit_code, result.output.strip()) == (0, f"review {open_id} rejected")
    with db() as session:
        assert session.get_one(EntityReview, open_id).status == "rejected"
    not_open = _invoke("review", "reject", str(done_id))
    assert (not_open.exit_code, not_open.output.strip()) == (
        1,
        f"error: review {done_id} is resolved, not open",
    )
    missing = _invoke("review", "reject", "999999")
    assert (missing.exit_code, missing.output.strip()) == (
        1,
        "error: entity_review 999999 not found",
    )


# -- quota --------------------------------------------------------------------------------


def test_quota_with_no_ledger(db: sessionmaker[Session]) -> None:
    result = _invoke("quota")
    assert (result.exit_code, result.output.strip()) == (0, "no Odds API calls recorded")


def test_quota_shows_remaining_and_credits_per_run(db: sessionmaker[Session]) -> None:
    with db.begin() as session:
        run = CaptureRun(
            sport="nba",
            slate_date=SLATE,
            mode="manual",
            started_at=NOW,
            status="completed",
            credits_cap=180,
            plan={},
        )
        session.add(run)
        session.flush()
        at = partial(datetime, 2026, 11, 12, tzinfo=UTC)
        session.add_all(
            [
                QuotaLedger(
                    provider="odds_api",
                    cost=3,
                    used=3,
                    remaining=497,
                    observed_at=at(22),
                    capture_run_id=run.id,
                ),
                QuotaLedger(
                    provider="odds_api",
                    cost=6,
                    used=9,
                    remaining=491,
                    observed_at=at(23),
                    capture_run_id=run.id,
                ),
                QuotaLedger(
                    provider="odds_api", cost=0, used=9, remaining=None, observed_at=at(23, 30)
                ),
            ]
        )
        run_id = run.id
    result = _invoke("quota")
    assert result.exit_code == 0, result.output
    assert "remaining: 491 (as of 2026-11-12 18:00 ET)" in result.output
    assert f"run {run_id}  nba 2026-11-12  completed  9 credits" in result.output
    assert "no run  0 credits" in result.output
