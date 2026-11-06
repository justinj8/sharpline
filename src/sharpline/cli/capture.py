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
        _export_partial(sessions, runner.run_id, slate, "interrupted")
        raise typer.Exit(code=INTERRUPTED_EXIT) from None
    except Exception:
        # A failed run (e.g. the database kept dropping) is exported like an interrupted
        # one, then the error is reported by cli_command.
        _export_partial(sessions, runner.run_id, slate, "failed")
        raise
    with sessions() as session:
        run = session.get_one(CaptureRun, run_id)
        typer.echo(f"capture_run_id: {run.id}")
        _summary(run)
        path = export_bundle(session, run.id, DEFAULT_LOCAL_ROOT)  # spec 1.1: every capture
    typer.echo(f"bundle: {path}")
    _next_steps(slate, run_id)


def _summary(run: CaptureRun) -> None:
    statuses = [a["status"] for a in run.plan.get("actions", [])]
    counts = ", ".join(f"{s} {statuses.count(s)}" for s in ("done", "skipped", "failed"))
    typer.echo(f"status: {run.status}")
    typer.echo(f"credits: {run.credits_used} / {run.credits_cap}")
    typer.echo(f"actions: {counts}")
    typer.echo(f"dropped: {len(run.plan.get('dropped', []))}")


def _next_steps(slate: date, run_id: int) -> None:
    """The same-day bundle has no finals or box scores yet: the final bundle is exported
    the morning after, once results are in and reparsed, and that one is pushed (M4)."""
    typer.echo("next, the morning after the slate:")
    typer.echo(f"  sharpline results nba --date {slate.isoformat()}")
    typer.echo("  sharpline reparse")
    typer.echo(f"  sharpline bundle export {run_id}")
    typer.echo("  sharpline bundle push <the file bundle export writes>")


def _export_partial(
    sessions: sessionmaker[Session], run_id: int | None, slate: date, how: str
) -> None:
    """Export what this interrupted or failed session captured, so the day is not lost.

    `run_id` is the runner's own run; None means it stopped before its row committed, so
    there is nothing of this session's to export. The run id is printed before anything
    touches the database, which may be what failed; a failed export is a warning.
    """
    if run_id is None:
        typer.echo(f"{how} before the capture run was recorded", err=True)
        return
    typer.echo(f"{how}: capture_run_id {run_id}", err=True)
    try:
        with sessions() as session:
            run = session.get_one(CaptureRun, run_id)
            _summary(run)
            path = export_bundle(session, run.id, DEFAULT_LOCAL_ROOT)
    except Exception as exc:
        warn(f"could not export capture run {run_id}: {type(exc).__name__}")
        return
    typer.echo(f"partial bundle: {path}")
    _next_steps(slate, run_id)


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

    Box scores attach only to stored events (R1), so each season's game days are first
    ingested from the ESPN scoreboard (events and finals, free), then the box scores are
    written, creating the players. Resumable: rerun after a failure and dates whose
    scoreboard is already stored are skipped.
    """
    bad = [s for s in seasons if not SEASON.fullmatch(s)]
    if bad:
        raise CliError(f"seasons look like 2025-26, got: {', '.join(bad)}")
    settings = load_settings()
    nba = capture_clients.build_nba_stats_client()
    espn = capture_clients.build_espn_client(settings)
    with database(settings) as sessions:
        for season in seasons:
            with sessions() as session:
                done = jobs.backfill_season(session, nba, espn, season)
            typer.echo(
                f"{season}: game dates {done.dates} (fetched {done.fetched}, skipped"
                f" {done.skipped}); player stats: {stats_line(done.stats)}"
            )
