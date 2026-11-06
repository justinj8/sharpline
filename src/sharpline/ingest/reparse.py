"""Rebuild typed rows from stored `sources` (spec section 3.1: parse on write, pure parsers).

`PARSERS` maps (provider, endpoint pattern) to the same `Parser` the ingest jobs use, so a
reparse writes exactly what the original fetch wrote, through the same Writer semantics
(R4 final_at, R7a team-scoped xrefs, R9 tip threshold, closing dedupe, newest score wins).

Order matters because entities are created as a side effect of writing:
1. Events from every source, oldest fetch first: the tip only moves on a > 10 min change
   (R9), so replaying event rows in their original order reproduces the original tip.
2. Everything else by provider, nba_stats -> espn -> odds_api, oldest fetch first within
   each. Only nba_stats box scores create players, so in a fresh database injuries and
   props resolve only once the box scores are in.

An odds source is written once per observation purpose: as regular snapshots if it was
ever fetched for a regular refresh, and as closing snapshots if it was ever fetched for a
closing (`source_observations.purpose`), each captured at the first such fetch. That is
what the original jobs wrote, including when a closing payload deduped onto an earlier one.

Reparse never deletes: rows only get added or updated (the writer's upserts), and nothing
here commits. Sources with no registered parser are skipped.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import Row, func, select
from sqlalchemy.orm import Session

from sharpline.db.models import Source, SourceObservation
from sharpline.ingest import jobs
from sharpline.ingest.jobs import CLOSING, Parser
from sharpline.ingest.resolve import EntityResolver
from sharpline.ingest.writer import Writer, WriteStats
from sharpline.sources.espn import PROVIDER as ESPN
from sharpline.sources.nba_stats import PROVIDER as NBA_STATS
from sharpline.sources.nba_stats.client import ENDPOINT as NBA_GAME_LOG
from sharpline.sources.odds_api.client import PROVIDER as ODDS

log = logging.getLogger(__name__)

# (provider, endpoint regex, matched in full) -> the ingest job's parser and writer methods.
# ESPN scoreboards always write results: a final is a final whichever job fetched it (R4).
PARSERS: dict[tuple[str, str], Parser] = {
    (ESPN, r"scoreboard"): jobs.RESULTS,
    (ESPN, r"injuries"): jobs.INJURIES,
    (NBA_STATS, re.escape(NBA_GAME_LOG)): jobs.PLAYER_STATS,
    (ODDS, r"sports/[^/]+/events"): jobs.ODDS_EVENTS,
    (ODDS, r"sports/[^/]+/odds"): jobs.ODDS,
    (ODDS, r"sports/[^/]+/events/[^/]+/odds"): jobs.ODDS,
}
PROVIDER_ORDER = (NBA_STATS, ESPN, ODDS)

_SourceRow = Row[int, str, str, datetime]


def parser_for(provider: str, endpoint: str) -> Parser | None:
    for (p, pattern), parser in PARSERS.items():
        if p == provider and re.fullmatch(pattern, endpoint):
            return parser
    return None


def _first_observed(
    session: Session, source_ids: list[int]
) -> dict[int, dict[str | None, datetime]]:
    """source_id -> purpose -> first observation with that purpose."""
    rows = session.execute(
        select(
            SourceObservation.source_id,
            SourceObservation.purpose,
            func.min(SourceObservation.observed_at),
        )
        .where(SourceObservation.source_id.in_(source_ids))
        .group_by(SourceObservation.source_id, SourceObservation.purpose)
    ).all()
    out: dict[int, dict[str | None, datetime]] = {}
    for source_id, purpose, first in rows:
        out.setdefault(source_id, {})[purpose] = first
    return out


def _provider_rank(provider: str) -> int:
    return PROVIDER_ORDER.index(provider) if provider in PROVIDER_ORDER else len(PROVIDER_ORDER)


def reparse(
    session: Session,
    *,
    provider: str | None = None,
    source_ids: Iterable[int] | None = None,
) -> WriteStats:
    """Re-derive typed rows from stored sources (all, one provider's, or the given ids)."""
    query = select(Source.id, Source.provider, Source.endpoint, Source.fetched_at)
    if provider is not None:
        query = query.where(Source.provider == provider)
    if source_ids is not None:
        query = query.where(Source.id.in_(list(source_ids)))

    work: list[tuple[_SourceRow, Parser]] = []
    for src in session.execute(query).all():
        parser = parser_for(src.provider, src.endpoint)
        if parser is None:
            log.warning(
                "reparse: no parser for %s %s (source %s)", src.provider, src.endpoint, src.id
            )
            continue
        work.append((src, parser))
    observed = _first_observed(session, [src.id for src, _ in work])

    def payload(source_id: int) -> object:
        return session.get_one(Source, source_id).payload

    writer = Writer(session, EntityResolver(session))
    stats = WriteStats()
    for src, parser in sorted(work, key=lambda w: (w[0].fetched_at, w[0].id)):
        if parser.events is not None:
            stats += writer.events(parser.events(payload(src.id)), src.id)
    rows_order = sorted(
        work, key=lambda w: (_provider_rank(w[0].provider), w[0].fetched_at, w[0].id)
    )
    for src, parser in rows_order:
        if parser.rows is None:
            continue
        purposes = observed.get(src.id) or {None: src.fetched_at}
        for purpose, at in sorted(purposes.items(), key=lambda kv: kv[1]):
            stats += parser.rows(writer, payload(src.id), src.id, at, purpose == CLOSING)
    return stats
