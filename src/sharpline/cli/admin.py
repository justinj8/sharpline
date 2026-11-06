"""`sharpline review`, `reparse` and `quota`: housekeeping on the stored data."""

from enum import StrEnum
from typing import Annotated

import typer
from sqlalchemy import func, select, update

from sharpline.cli._common import (
    CliError,
    cli_command,
    database,
    eastern,
    load_settings,
    stats_line,
    table,
)
from sharpline.db.base import Base
from sharpline.db.models import CaptureRun, EntityReview, Event, Player, QuotaLedger, Team
from sharpline.ingest.jobs import transaction
from sharpline.ingest.reparse import reparse as reparse_sources
from sharpline.ingest.resolve import EntityResolver
from sharpline.sources.espn import PROVIDER as ESPN
from sharpline.sources.nba_stats import PROVIDER as NBA_STATS
from sharpline.sources.odds_api.client import PROVIDER as ODDS

review_app = typer.Typer(help="The entity review queue: names we could not match.")

ENTITY_MODELS: dict[str, type[Base]] = {"player": Player, "team": Team, "event": Event}


class Provider(StrEnum):
    ODDS_API = ODDS
    ESPN = ESPN
    NBA_STATS = NBA_STATS


class Kind(StrEnum):
    PLAYER = "player"
    TEAM = "team"
    EVENT = "event"


@review_app.command("list")
@cli_command
def review_list() -> None:
    """Open reviews, with their fuzzy candidates as `entity_id:name (score)`."""
    settings = load_settings()
    with database(settings) as sessions, sessions() as session:
        reviews = session.scalars(
            select(EntityReview).where(EntityReview.status == "open").order_by(EntityReview.id)
        ).all()
    if not reviews:
        typer.echo("no open reviews")
        return
    rows = [
        [
            str(r.id),
            r.provider,
            r.entity_kind,
            r.provider_id,
            r.raw_name,
            ", ".join(f"{c['id']}:{c['name']} ({c['score']:.0f})" for c in r.candidates) or "-",
        ]
        for r in reviews
    ]
    typer.echo(table(["id", "provider", "kind", "provider id", "name", "candidates"], rows))


@review_app.command("resolve")
@cli_command
def review_resolve(review_id: int, entity_id: int) -> None:
    """Map a review's provider name to one of our entities (adds the xref)."""
    settings = load_settings()
    with database(settings) as sessions, sessions() as session, transaction(session):
        review = session.get(EntityReview, review_id)
        if review is None:
            raise CliError(f"entity_review {review_id} not found")
        kind = review.entity_kind
        if session.get(ENTITY_MODELS[kind], entity_id) is None:
            raise CliError(f"{kind} {entity_id} not found")
        EntityResolver(session).resolve_review(review_id, entity_id)
    typer.echo(f"review {review_id} resolved -> {kind} {entity_id}")


@review_app.command("reject")
@cli_command
def review_reject(
    review_id: Annotated[int | None, typer.Argument(help="The review to reject.")] = None,
    reject_all: Annotated[
        bool, typer.Option("--all", help="Reject every open review matching the filters.")
    ] = False,
    provider: Annotated[
        Provider | None, typer.Option(help="With --all: only this provider.")
    ] = None,
    kind: Annotated[Kind | None, typer.Option(help="With --all: only this entity kind.")] = None,
    yes: Annotated[bool, typer.Option("--yes", help="With --all: do not ask to confirm.")] = False,
) -> None:
    """Close open reviews without a match (the provider's rows stay skipped): one by id, or
    `--all [--provider P] [--kind K]` after a confirmation (`--yes` skips it)."""
    if (review_id is None) == (not reject_all):
        raise CliError("give a review id, or --all (with optional --provider/--kind)")
    if review_id is None:
        _reject_all(provider, kind, yes=yes)
        return
    if provider is not None or kind is not None:
        raise CliError("--provider/--kind only apply with --all")
    settings = load_settings()
    with database(settings) as sessions, sessions() as session, transaction(session):
        review = session.get(EntityReview, review_id)
        if review is None:
            raise CliError(f"entity_review {review_id} not found")
        if review.status != "open":
            raise CliError(f"review {review_id} is {review.status}, not open")
        review.status = "rejected"
    typer.echo(f"review {review_id} rejected")


def _reject_all(provider: Provider | None, kind: Kind | None, *, yes: bool) -> None:
    where = [EntityReview.status == "open"]
    if provider is not None:
        where.append(EntityReview.provider == provider.value)
    if kind is not None:
        where.append(EntityReview.entity_kind == kind.value)
    settings = load_settings()
    with database(settings) as sessions:
        with sessions() as session:
            count = session.scalar(select(func.count()).select_from(EntityReview).where(*where))
        if not count:
            typer.echo("no open reviews match")
            return
        if not yes:
            filters = ", ".join(
                f"{name} {value}"
                for name, value in (("provider", provider), ("kind", kind))
                if value is not None
            )
            scope = f" ({filters})" if filters else ""
            typer.confirm(f"reject {count} open review{_s(count)}{scope}?", abort=True)
        with sessions() as session, transaction(session):
            rejected = len(
                session.execute(
                    update(EntityReview)
                    .where(*where)
                    .values(status="rejected")
                    .returning(EntityReview.id)
                ).all()
            )
    typer.echo(f"rejected {rejected} review{_s(rejected)}")


def _s(n: int) -> str:
    return "" if n == 1 else "s"


@cli_command
def reparse(
    provider: Annotated[Provider | None, typer.Option(help="Only this provider's sources.")] = None,
    source_id: Annotated[
        list[int] | None, typer.Option("--source-id", help="Only these sources (repeatable).")
    ] = None,
) -> None:
    """Rebuild typed rows from stored sources (additive: never deletes)."""
    settings = load_settings()
    with database(settings) as sessions, sessions() as session, transaction(session):
        stats = reparse_sources(
            session,
            provider=None if provider is None else provider.value,
            source_ids=source_id or None,
        )
    typer.echo(f"reparse: {stats_line(stats)}")


@cli_command
def quota() -> None:
    """Latest Odds API credits remaining, and credits used per capture run."""
    settings = load_settings()
    with database(settings) as sessions, sessions() as session:
        latest = session.execute(
            select(QuotaLedger.remaining, QuotaLedger.observed_at)
            .where(QuotaLedger.provider == ODDS, QuotaLedger.remaining.is_not(None))
            .order_by(QuotaLedger.observed_at.desc(), QuotaLedger.id.desc())
            .limit(1)
        ).first()
        per_run = session.execute(
            select(
                QuotaLedger.capture_run_id,
                CaptureRun.sport,
                CaptureRun.slate_date,
                CaptureRun.status,
                func.sum(QuotaLedger.cost),
            )
            .outerjoin(CaptureRun, CaptureRun.id == QuotaLedger.capture_run_id)
            .where(QuotaLedger.provider == ODDS)
            .group_by(
                QuotaLedger.capture_run_id,
                CaptureRun.sport,
                CaptureRun.slate_date,
                CaptureRun.status,
            )
            .order_by(QuotaLedger.capture_run_id.asc().nulls_last())
        ).all()
    if not per_run:
        typer.echo("no Odds API calls recorded")
        return
    if latest is None:
        typer.echo("remaining: unknown (no call reported it)")
    else:
        typer.echo(f"remaining: {latest.remaining} (as of {eastern(latest.observed_at)})")
    typer.echo("credits used per capture run:")
    for run_id, sport, slate_date, status, used in per_run:
        label = "no run" if run_id is None else f"run {run_id}  {sport} {slate_date}  {status}"
        typer.echo(f"  {label}  {used} credits")
