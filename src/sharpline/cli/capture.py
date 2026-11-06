"""`sharpline capture`, `results` and `backfill`: the commands that talk to providers."""

import re
import signal
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date, datetime
from types import FrameType
from typing import Annotated, Any

import typer
from sqlalchemy import select
from sqlalchemy.orm import Session, aliased, sessionmaker

from sharpline.bundles.export import export_bundle
from sharpline.bundles.store import DEFAULT_LOCAL_ROOT
from sharpline.capture import clients as capture_clients
from sharpline.capture.plan import Budget, CaptureAction, CapturePlan, plan_captures
from sharpline.capture.runner import SlateEntry, load_slate
from sharpline.cli._common import (
    CliError,
    cli_command,
    database,
    eastern,
    load_settings,
    stats_line,
    table,
    warn,
)
from sharpline.config import Settings
from sharpline.db.models import CaptureRun, Event, Team
from sharpline.domain.enums import Sport
from sharpline.ingest import jobs
from sharpline.sources.http import utcnow

capture_app = typer.Typer(help="Capture a slate's odds, injuries and schedule.")
results_app = typer.Typer(help="Next-morning results and box scores for a slate.")
backfill_app = typer.Typer(help="Historical data backfills.")

# The dry run's "now" (the session would start when the command runs); tests pin it.
clock = utcnow

SlateDate = Annotated[
    datetime, typer.Option("--date", formats=["%Y-%m-%d"], help="Slate date (ET), YYYY-MM-DD.")
]
SEASON = re.compile(r"\d{4}-\d{2}")
MAX_LISTED_GAMES = 3
INTERRUPTED_EXIT = 130  # 128 + SIGINT, the shell convention


@capture_app.command("nba")
@cli_command
def capture_nba(
    slate: SlateDate,
    cap: Annotated[
        int | None, typer.Option(min=0, help="Session credit cap (default CAPTURE_SESSION_CAP).")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the plan; no Odds API calls, no run.")
    ] = False,
    offline: Annotated[
        bool, typer.Option("--offline", help="With --dry-run: plan from stored events only.")
    ] = False,
) -> None:
    """Run a manual capture session for an NBA slate (or plan one with --dry-run)."""
    if offline and not dry_run:
        raise CliError("--offline only applies with --dry-run")
    settings = load_settings()
    with database(settings) as sessions:
        if dry_run:
            _dry_run(sessions, settings, slate.date(), cap, offline=offline)
        else:
            _capture(sessions, settings, slate.date(), cap)


def _dry_run(
    sessions: sessionmaker[Session],
    settings: Settings,
    slate: date,
    cap: int | None,
    *,
    offline: bool,
) -> None:
    """Plan without spending: the ESPN schedule at most, no Odds API, no capture run."""
    if not offline:
        espn = capture_clients.build_espn_client(settings)
        with sessions() as session:
            typer.echo(f"schedule: {stats_line(jobs.ingest_schedule(session, espn, slate))}")
    with sessions() as session:
        entries = load_slate(session, Sport.NBA, slate)
        labels = _matchups(session, entries)
    now = clock()
    budget = Budget(settings.capture_session_cap if cap is None else cap)
    plan = plan_captures([e.game for e in entries], now, budget)
    source = "stored events" if offline else "ESPN schedule"
    typer.echo(f"capture plan nba {slate} (dry run, {source}, session start {eastern(now)})")
    if not entries:
        typer.echo(f"no scheduled games stored for {slate}")
    _print_plan(plan, labels, budget.session_cap)


def _matchups(session: Session, entries: Sequence[SlateEntry]) -> dict[str, str]:
    """Plan key -> "AWAY@HOME" (team abbreviations)."""
    home, away = aliased(Team), aliased(Team)
    rows = session.execute(
        select(Event.id, away.abbreviation, home.abbreviation)
        .join(home, home.id == Event.home_team_id)
        .join(away, away.id == Event.away_team_id)
        .where(Event.id.in_([e.event_id for e in entries]))
    ).all()
    names = {event_id: f"{a}@{h}" for event_id, a, h in rows}
    return {e.game.provider_event_id: names[e.event_id] for e in entries}


def _row(a: CaptureAction, labels: dict[str, str]) -> list[str]:
    if not a.event_ids:
        games = "-"
    elif len(a.event_ids) > MAX_LISTED_GAMES:
        games = f"{len(a.event_ids)} games"
    else:
        games = f"{len(a.event_ids)} ({', '.join(labels.get(i, i) for i in a.event_ids)})"
    return [eastern(a.at, "%m-%d %H:%M"), a.kind, games, str(a.est_cost), a.reason]


def _print_plan(plan: CapturePlan, labels: dict[str, str], cap: int) -> None:
    headers = ["time (ET)", "kind", "games", "est. credits", "reason"]
    typer.echo(table(headers, [_row(a, labels) for a in plan.actions]))
    typer.echo(f"total est. credits: {plan.est_cost} / cap {cap}")
    if not plan.dropped:
        typer.echo("dropped: none")
        return
    by_reason: dict[str, list[list[str]]] = {}
    for action, why in plan.dropped:
        by_reason.setdefault(why, []).append(_row(action, labels))
    for why, rows in by_reason.items():
        typer.echo(f"dropped ({why}):")
        typer.echo(table([], rows, indent="  "))


@contextmanager
def _sigterm_interrupts() -> Iterator[None]:
    """SIGTERM ends a session like Ctrl-C (spec section 7): marked interrupted, exported."""

    def handler(_signum: int, _frame: FrameType | None) -> None:
        raise KeyboardInterrupt

    try:
        previous: Any = signal.signal(signal.SIGTERM, handler)
    except ValueError:  # not the main thread: leave signals alone
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _capture(
    sessions: sessionmaker[Session], settings: Settings, slate: date, cap: int | None
) -> None:
    if settings.odds_api_key is None:
        warn("ODDS_API_KEY is not set: odds actions will be skipped")
    runner = capture_clients.build_runner(sessions, settings, cap=cap)
    try:
        with _sigterm_interrupts():
            run_id = runner.run(Sport.NBA, slate, mode="manual")
    except KeyboardInterrupt:
        _export_interrupted(sessions, slate)
        raise typer.Exit(code=INTERRUPTED_EXIT) from None
    with sessions() as session:
        run = session.get_one(CaptureRun, run_id)
    typer.echo(f"capture_run_id: {run.id}")
    _summary(run)


def _summary(run: CaptureRun) -> None:
    statuses = [a["status"] for a in run.plan.get("actions", [])]
    counts = ", ".join(f"{s} {statuses.count(s)}" for s in ("done", "skipped", "failed"))
    typer.echo(f"status: {run.status}")
    typer.echo(f"credits: {run.credits_used} / {run.credits_cap}")
    typer.echo(f"actions: {counts}")
    typer.echo(f"dropped: {len(run.plan.get('dropped', []))}")


def _export_interrupted(sessions: sessionmaker[Session], slate: date) -> None:
    """Export what an interrupted session captured, so the day is not lost."""
    with sessions() as session:
        run = session.scalars(
            select(CaptureRun)
            .where(
                CaptureRun.sport == Sport.NBA.value,
                CaptureRun.slate_date == slate,
                CaptureRun.mode == "manual",
                CaptureRun.status == "interrupted",
            )
            .order_by(CaptureRun.id.desc())
            .limit(1)
        ).first()
        if run is None:
            typer.echo("interrupted before the capture run was recorded", err=True)
            return
        typer.echo(f"interrupted: capture_run_id {run.id}", err=True)
        _summary(run)
        path = export_bundle(session, run.id, DEFAULT_LOCAL_ROOT)
    typer.echo(f"partial bundle: {path}")


@results_app.command("nba")
@cli_command
def results_nba(slate: SlateDate) -> None:
    """Fetch a slate's finals (ESPN) and box scores (nba_stats), the morning after."""
    settings = load_settings()
    espn = capture_clients.build_espn_client(settings)
    nba = capture_clients.build_nba_stats_client()
    day = slate.date()
    with database(settings) as sessions:
        with sessions() as session:
            typer.echo(f"results: {stats_line(jobs.ingest_results(session, espn, day))}")
        with sessions() as session:
            stats = jobs.ingest_player_stats(session, nba, day)
        typer.echo(f"player stats: {stats_line(stats)}")


@backfill_app.command("nba-stats")
@cli_command
def backfill_nba_stats(
    seasons: Annotated[list[str], typer.Argument(metavar="SEASON...", help="e.g. 2025-26")],
    _seasons_flag: Annotated[
        bool, typer.Option("--seasons", help="Optional marker before the season list.")
    ] = False,
) -> None:
    """Fetch whole seasons of NBA box scores: `backfill nba-stats --seasons 2024-25 2025-26`.

    Box scores attach only to stored events (R1); rows without one are counted as skipped
    and their source is kept, so a later `reparse` can attach them.
    """
    bad = [s for s in seasons if not SEASON.fullmatch(s)]
    if bad:
        raise CliError(f"seasons look like 2025-26, got: {', '.join(bad)}")
    settings = load_settings()
    nba = capture_clients.build_nba_stats_client()
    with database(settings) as sessions:
        for season in seasons:
            with sessions() as session:
                stats = jobs.ingest_season_player_stats(session, nba, season)
            typer.echo(f"{season}: {stats_line(stats)}")
