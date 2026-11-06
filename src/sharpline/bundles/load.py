"""Load a slate bundle into any database, then reparse its sources (spec section 6.1).

Loading is idempotent: sources go through `record_source` (content-hash dedupe, original
`fetched_at` kept), observations are only added where the database has fewer identical
ones, and players are matched through their xrefs before any is created. The load is two
units, like an ingest job (spec section 7): the bundle's rows, then the reparse, so a parser
failure leaves the loaded sources in place for a later `reparse`.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select, text, tuple_
from sqlalchemy.orm import Session

from sharpline.bundles.format import read_bundle
from sharpline.db.models import EntityXref, Player, Source, SourceObservation, Team
from sharpline.db.sources import canonical_hash, record_source
from sharpline.ingest.jobs import transaction
from sharpline.ingest.reparse import reparse
from sharpline.ingest.resolve import NBA_STATS, team_scoped_key
from sharpline.ingest.writer import WriteStats

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LoadReport:
    sources_inserted: int
    sources_existing: int
    observations: int
    reparsed: WriteStats
    players_created: int = 0


def _load_sources(session: Session, records: list[dict[str, Any]]) -> tuple[dict[int, int], int]:
    """ref -> source id, and how many sources were new."""
    ids: dict[int, int] = {}
    inserted = 0
    for r in records:
        existing = session.scalar(
            select(Source.id).where(
                Source.provider == r["provider"],
                Source.endpoint == r["endpoint"],
                Source.params_hash == canonical_hash(r["params"]),
                Source.content_hash == canonical_hash(r["payload"]),
            )
        )
        if existing is None:
            inserted += 1
            existing = record_source(
                session,
                provider=r["provider"],
                endpoint=r["endpoint"],
                params=r["params"],
                payload=r["payload"],
                fetched_at=datetime.fromisoformat(r["fetched_at"]),
            )
        ids[r["ref"]] = existing
    return ids, inserted


def _load_observations(session: Session, records: list[dict[str, Any]], ids: dict[int, int]) -> int:
    """Insert each observation the database does not already hold (as a multiset)."""
    wanted: Counter[tuple[int, datetime, str | None]] = Counter(
        (ids[r["source_ref"]], datetime.fromisoformat(r["observed_at"]), r["purpose"])
        for r in records
    )
    have: Counter[tuple[int, datetime, str | None]] = Counter(
        (row.source_id, row.observed_at, row.purpose)
        for row in session.execute(
            select(
                SourceObservation.source_id,
                SourceObservation.observed_at,
                SourceObservation.purpose,
            ).where(SourceObservation.source_id.in_(list(ids.values())))
        )
    )
    missing = list((wanted - have).elements())
    session.add_all(
        SourceObservation(source_id=sid, observed_at=at, purpose=purpose)
        for sid, at, purpose in missing
    )
    session.flush()
    return len(missing)


def _xref_key(x: dict[str, Any], teams: dict[str, int]) -> tuple[str, str] | None:
    if "provider_id" in x:
        return x["provider"], x["provider_id"]
    if x["team"] is None:
        return x["provider"], team_scoped_key(x["name"], None)
    team_id = teams.get(x["team"])
    return None if team_id is None else (x["provider"], team_scoped_key(x["name"], team_id))


def _load_players(session: Session, records: list[dict[str, Any]]) -> int:
    """Match each player by any of its xrefs (nba_stats first), else create it; then add
    the xrefs this database lacks. Existing xrefs are never repointed."""
    created = 0
    teams_by_sport: dict[str, dict[str, int]] = {}
    for r in records:
        if r["sport"] not in teams_by_sport:
            teams_by_sport[r["sport"]] = {
                abbr: tid
                for abbr, tid in session.execute(
                    select(Team.abbreviation, Team.id).where(Team.sport == r["sport"])
                )
            }
        teams = teams_by_sport[r["sport"]]
        team_id = None if r["team"] is None else teams.get(r["team"])
        keys = [k for x in r["xrefs"] if (k := _xref_key(x, teams)) is not None]
        hits: dict[tuple[str, str], int] = {}
        if keys:
            hits = {
                (provider, provider_id): entity_id
                for provider, provider_id, entity_id in session.execute(
                    select(EntityXref.provider, EntityXref.provider_id, EntityXref.entity_id).where(
                        EntityXref.entity_kind == "player",
                        tuple_(EntityXref.provider, EntityXref.provider_id).in_(keys),
                    )
                )
            }
        matches = sorted(hits.items(), key=lambda kv: (kv[0][0] != NBA_STATS, kv[0]))
        if len({entity_id for _, entity_id in matches}) > 1:
            log.warning(
                "bundle player %r matches several players; using the nba_stats match",
                r["full_name"],
            )
        if matches:
            player_id = matches[0][1]
        elif not keys and (
            same := session.scalar(
                select(Player.id).where(
                    Player.sport == r["sport"],
                    Player.full_name == r["full_name"],
                    Player.team_id.is_not_distinct_from(team_id),
                )
            )
        ):
            player_id = same  # no xrefs to match on: reuse the identical player (idempotent)
        else:
            player = Player(sport=r["sport"], full_name=r["full_name"], team_id=team_id)
            session.add(player)
            session.flush()
            player_id = player.id
            created += 1
        session.add_all(
            EntityXref(entity_kind="player", entity_id=player_id, provider=p, provider_id=pid)
            for p, pid in keys
            if (p, pid) not in hits
        )
        session.flush()
    return created


def load_bundle(session: Session, path: Path) -> LoadReport:
    """Verify, load and reparse a bundle; raises BundleIntegrityError before writing."""
    manifest, records = read_bundle(path)
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        by_kind.setdefault(r["kind"], []).append(r)

    # Even the revision read runs inside the unit: a query before it would autobegin a
    # transaction on an idle session and turn both units into uncommitted savepoints.
    with transaction(session):
        revision = session.scalar(text("SELECT version_num FROM alembic_version"))
        if manifest.alembic_revision != revision:  # records are raw payloads: usually harmless
            log.warning(
                "bundle %s was exported at alembic %s, loading into %s",
                path.name,
                manifest.alembic_revision,
                revision,
            )
        ids, inserted = _load_sources(session, by_kind.get("source", []))
        observations = _load_observations(session, by_kind.get("observation", []), ids)
        players = _load_players(session, by_kind.get("player", []))
    with transaction(session):
        stats = reparse(session, source_ids=ids.values())
    return LoadReport(
        sources_inserted=inserted,
        sources_existing=len(ids) - inserted,
        observations=observations,
        reparsed=stats,
        players_created=players,
    )
