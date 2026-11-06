"""`sharpline` CLI against a private NBA-seeded database and MockTransport providers."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime
from functools import partial
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from sharpline.bundles.format import read_bundle, write_bundle
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
    PlayerGameStat,
    QuotaLedger,
    Result,
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
    for name in (
        "build_espn_client",
        "build_odds_client",
        "build_clients",
        "build_runner",
        "build_nba_stats_client",
        "OddsApiClient",
    ):
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
    # A key is configured, so only the dry run itself keeps the Odds API out: the `db`
    # fixture makes building any Odds API client (factory or class) raise.
    monkeypatch.setenv("ODDS_API_KEY", "sk-dummy-not-real")
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
    db: sessionmaker[Session], cli_env: Path, monkeypatch: pytest.MonkeyPatch
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
    (bundle,) = (cli_env / "var" / "bundles").glob("nba-2026-11-12-*.jsonl.gz")
    assert read_bundle(bundle)[0].capture_run_id == run.id
    shown = Path("var/bundles") / bundle.name
    assert f"bundle: {shown}" in out
    # M4: the same-day bundle has no finals or box scores yet: never pushed as final
    assert "sharpline bundle push var/bundles" not in out
    assert (
        "next, the morning after the slate:\n"
        "  sharpline results nba --date 2026-11-12\n"
        "  sharpline reparse\n"
        f"  sharpline bundle export {run.id}\n"
        "  sharpline bundle push <the file bundle export writes>\n"
    ) in out


def _older_interrupted_run(db: sessionmaker[Session]) -> int:
    with db.begin() as session:
        older = CaptureRun(
            sport="nba",
            slate_date=SLATE,
            mode="manual",
            started_at=NOW,
            finished_at=NOW,
            status="interrupted",
            credits_cap=180,
            plan={},
        )
        session.add(older)
        session.flush()
        return older.id


def test_interrupted_capture_exports_its_own_partial_bundle(
    db: sessionmaker[Session], cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    older_id = _older_interrupted_run(db)  # same day: must not be the one exported
    clock = FakeClock(NOW, interrupt=True)
    monkeypatch.setattr(clients, "build_runner", _fake_runner(clock, Espn(GAMES)))
    result = _invoke("capture", "nba", "--date", "2026-11-12")
    assert result.exit_code == 130, result.output
    with db() as session:
        run = session.scalars(select(CaptureRun).where(CaptureRun.id != older_id)).one()
    assert run.status == "interrupted"
    bundles = list((cli_env / "var" / "bundles").glob("nba-2026-11-12-*.jsonl.gz"))
    assert len(bundles) == 1
    assert read_bundle(bundles[0])[0].capture_run_id == run.id
    assert f"interrupted: capture_run_id {run.id}" in result.output
    assert f"partial bundle: {Path('var/bundles') / bundles[0].name}" in result.output


def test_interrupt_before_the_run_is_recorded_exports_nothing(
    db: sessionmaker[Session], cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class NotStarted:
        run_id: int | None = None

        def run(self, *_a: Any, **_kw: Any) -> int:
            raise KeyboardInterrupt

    _older_interrupted_run(db)  # an earlier session's run is not this one's to export
    monkeypatch.setattr(clients, "build_runner", lambda *_a, **_kw: NotStarted())
    result = _invoke("capture", "nba", "--date", "2026-11-12")
    assert result.exit_code == 130, result.output
    assert "interrupted before the capture run was recorded" in result.output
    assert not (cli_env / "var" / "bundles").exists()


class FlakySlate:
    """Session factory whose slate reads (`_load_slate`) drop the connection `times` times."""

    def __init__(self, real: sessionmaker[Session], times: int) -> None:
        self.real, self.times = real, times

    def _maybe_fail(self) -> None:
        if self.times and sys._getframe(2).f_code.co_name == "_load_slate":
            self.times -= 1
            raise OperationalError("SELECT 1", {}, Exception("server closed the connection"))

    def __call__(self, **kw: Any) -> Session:
        self._maybe_fail()
        return self.real(**kw)

    def begin(self) -> Any:
        self._maybe_fail()
        return self.real.begin()


def test_failed_capture_prints_its_run_and_exports_its_bundle(
    db: sessionmaker[Session], cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F5: a run that fails (three dropped connections in a row) is exported like an
    interrupted one, then the error is reported in one line."""
    clock, espn = FakeClock(NOW), Espn(GAMES)

    def build(
        sessions: sessionmaker[Session], settings: Settings, *, cap: int | None = None
    ) -> CaptureRunner:
        return CaptureRunner(
            FlakySlate(sessions, 3),  # type: ignore[arg-type]
            espn=espn.client(clock),
            odds=None,
            guard=QuotaGuard(settings.capture_session_cap, settings.capture_quota_floor),
            breaker=CircuitBreaker(clock=clock),
            clock=clock,
            sleep_until=clock.sleep_until,
        )

    monkeypatch.setattr(clients, "build_runner", build)
    result = _invoke("capture", "nba", "--date", "2026-11-12")
    assert result.exit_code == 1, result.output
    with db() as session:
        run = session.scalars(select(CaptureRun)).one()
    assert run.status == "failed"
    assert f"failed: capture_run_id {run.id}" in result.output
    (bundle,) = (cli_env / "var" / "bundles").glob("nba-2026-11-12-*.jsonl.gz")
    assert read_bundle(bundle)[0].capture_run_id == run.id
    assert f"partial bundle: {Path('var/bundles') / bundle.name}" in result.output
    assert result.output.rstrip().endswith("error: database error: OperationalError")


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


# The fixture game log's day (2025-10-28) as ESPN reports it: home, away, ESPN abbreviations.
BACKFILL_DAY = date(2025, 10, 28)
BACKFILL_GAMES = {
    "b1": (datetime(2025, 10, 29, 2, 30, tzinfo=UTC), "GS", "LAC"),
    "b2": (datetime(2025, 10, 28, 23, 30, tzinfo=UTC), "MIA", "CHA"),
    "b3": (datetime(2025, 10, 29, 0, 0, tzinfo=UTC), "MIL", "NY"),
}


class EspnByDate(Espn):
    """ESPN scoreboards by `dates` param (finals); records each date asked for."""

    def __init__(self, days: dict[date, dict[str, tuple[datetime, str, str]]]) -> None:
        super().__init__({})
        self.days = days
        self.dates: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        day = request.url.params["dates"]
        self.dates.append(day)
        games = self.days.get(datetime.strptime(day, "%Y%m%d").date(), {})
        board = _scoreboard(games)
        for event in board["events"]:
            event["status"]["type"]["name"] = "STATUS_FINAL"
            for n, competitor in enumerate(event["competitions"][0]["competitors"]):
                competitor["score"] = str(100 + n)
        return httpx.Response(200, json=board)


def _backfill_providers(monkeypatch: pytest.MonkeyPatch) -> tuple[EspnByDate, NbaStats]:
    espn, nba = EspnByDate({BACKFILL_DAY: BACKFILL_GAMES}), NbaStats()
    monkeypatch.setattr(clients, "build_espn_client", lambda settings: espn.client())
    monkeypatch.setattr(clients, "build_nba_stats_client", lambda: nba.client())
    return espn, nba


def test_backfill_fetches_each_season(
    db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    espn, nba = _backfill_providers(monkeypatch)
    result = _invoke("backfill", "nba-stats", "--seasons", "2024-25", "2025-26")
    assert result.exit_code == 0, result.output
    assert [c["season"] for c in nba.calls] == ["2024-25", "2025-26"]
    assert all("date_from_nullable" not in c for c in nba.calls)
    assert espn.dates == ["20251028"]  # both (fixture) seasons play that day: fetched once
    assert "2024-25: game dates 1 (fetched 1, skipped 0); player stats: written 5" in result.output
    assert "2025-26: game dates 1 (fetched 0, skipped 1); player stats: written 5" in result.output
    assert _count(db, Source) == 3  # one game log per season + one scoreboard


def test_backfill_stores_events_before_box_scores_and_resumes(
    db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """F4b: on a fresh database the season's game days are ingested from ESPN first (events
    and results), so every box score finds its event and creates its player."""
    espn, _nba = _backfill_providers(monkeypatch)
    result = _invoke("backfill", "nba-stats", "--seasons", "2025-26")
    assert result.exit_code == 0, result.output
    assert espn.dates == ["20251028"]
    assert "2025-26: game dates 1 (fetched 1, skipped 0)" in result.output
    assert "player stats: written 5, skipped 0, reviews 0" in result.output
    assert (_count(db, Event), _count(db, Result), _count(db, Player)) == (3, 3, 5)
    assert _count(db, PlayerGameStat) == 5

    again = _invoke("backfill", "nba-stats", "--seasons", "2025-26")  # resumable: no refetch
    assert again.exit_code == 0, again.output
    assert espn.dates == ["20251028"]
    assert "2025-26: game dates 1 (fetched 0, skipped 1)" in again.output
    assert (_count(db, Event), _count(db, PlayerGameStat)) == (3, 5)


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


def _open_reviews(db: sessionmaker[Session]) -> dict[tuple[str, str], int]:
    """Three open reviews: (provider, kind) -> id."""
    with db.begin() as session:
        rows = {
            ("odds_api", "player"): EntityReview(
                provider="odds_api",
                provider_id="A|1",
                entity_kind="player",
                raw_name="A",
                context={},
                candidates=[],
            ),
            ("espn", "player"): EntityReview(
                provider="espn",
                provider_id="2:B",
                entity_kind="player",
                raw_name="B",
                context={},
                candidates=[],
            ),
            ("odds_api", "team"): EntityReview(
                provider="odds_api",
                provider_id="Seattle",
                entity_kind="team",
                raw_name="Seattle",
                context={},
                candidates=[],
            ),
        }
        session.add_all(rows.values())
        session.flush()
        return {key: review.id for key, review in rows.items()}


def _statuses(
    db: sessionmaker[Session], ids: dict[tuple[str, str], int]
) -> dict[tuple[str, str], str]:
    with db() as session:
        return {key: session.get_one(EntityReview, rid).status for key, rid in ids.items()}


def test_review_reject_all_matching_the_filters(db: sessionmaker[Session]) -> None:
    """F4c: bulk reject, behind a confirmation that --yes skips."""
    ids = _open_reviews(db)
    args = ("review", "reject", "--all", "--provider", "odds_api", "--kind", "player")
    declined = runner.invoke(app, list(args), input="n\n")
    assert declined.exit_code == 1 and "reject 1 open review" in declined.output
    assert set(_statuses(db, ids).values()) == {"open"}

    confirmed = runner.invoke(app, list(args), input="y\n")
    assert confirmed.exit_code == 0, confirmed.output
    assert "rejected 1 review" in confirmed.output
    assert _statuses(db, ids) == {
        ("odds_api", "player"): "rejected",
        ("espn", "player"): "open",
        ("odds_api", "team"): "open",
    }

    rest = _invoke("review", "reject", "--all", "--kind", "player", "--yes")
    assert (rest.exit_code, rest.output.strip()) == (0, "rejected 1 review")
    assert _statuses(db, ids)[("espn", "player")] == "rejected"
    none_left = _invoke("review", "reject", "--all", "--kind", "player", "--yes")
    assert (none_left.exit_code, none_left.output.strip()) == (0, "no open reviews match")
    assert _statuses(db, ids)[("odds_api", "team")] == "open"


def test_review_reject_needs_an_id_or_all(db: sessionmaker[Session]) -> None:
    neither = _invoke("review", "reject")
    assert (neither.exit_code, neither.output.strip()) == (
        1,
        "error: give a review id, or --all (with optional --provider/--kind)",
    )
    both = _invoke("review", "reject", "7", "--all")
    assert (both.exit_code, both.output.strip()) == (
        1,
        "error: give a review id, or --all (with optional --provider/--kind)",
    )
    filters = _invoke("review", "reject", "7", "--provider", "espn")
    assert filters.exit_code == 1 and "--provider/--kind only apply with --all" in filters.output


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
