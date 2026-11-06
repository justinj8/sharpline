"""Export one capture session as a slate bundle (spec section 6.1).

A bundle holds three record kinds:
- `source`: every payload the session fetched, plus the slate date's next-morning results
  (ESPN scoreboard for that date) and box scores (nba_stats game log for exactly that
  date) whenever they exist, so results and player stats travel with their slate;
- `observation`: every observation of those sources (with `purpose`, so closing flags
  survive without `capture_runs`);
- `player`: the players the slate's typed rows refer to, with their provider xrefs.

Players travel because sources alone cannot recreate them: only nba_stats box scores
create players, and a player who is injured "out" has no box score on the slate, so a fresh
database could not resolve their injury. Their xrefs carry the resolutions made in the
capturing database (including human review decisions). Team-scoped xref keys (R7a,
"name|<team id>") embed a surrogate id, so they are exported by team abbreviation.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import and_, or_, select, text
from sqlalchemy.orm import Session

from sharpline.bundles.format import write_bundle
from sharpline.db.models import (
    CaptureRun,
    EntityXref,
    Injury,
    Market,
    OddsSnapshot,
    Player,
    PlayerGameStat,
    QuotaLedger,
    Source,
    SourceObservation,
    Team,
)
from sharpline.sources.espn import PROVIDER as ESPN
from sharpline.sources.nba_stats import PROVIDER as NBA_STATS
from sharpline.sources.nba_stats.client import ENDPOINT as NBA_GAME_LOG

ESPN_SCOREBOARD = "scoreboard"


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _source_ids(session: Session, run: CaptureRun) -> list[int]:
    window = SourceObservation.observed_at >= run.started_at
    if run.finished_at is not None:
        window = and_(window, SourceObservation.observed_at <= run.finished_at)
    day = run.slate_date
    nba_day = day.strftime("%m/%d/%Y")  # NbaStatsClient's date format
    next_morning = or_(
        and_(
            Source.provider == ESPN,
            Source.endpoint == ESPN_SCOREBOARD,
            Source.params.op("->>")("dates") == day.strftime("%Y%m%d"),
        ),
        and_(
            Source.provider == NBA_STATS,
            Source.endpoint == NBA_GAME_LOG,
            Source.params.op("->>")("date_from_nullable") == nba_day,
            Source.params.op("->>")("date_to_nullable") == nba_day,
        ),
    )
    queries = (
        select(QuotaLedger.source_id).where(QuotaLedger.capture_run_id == run.id),
        select(SourceObservation.source_id).where(window),
        select(Source.id).where(next_morning),
    )
    ids = {sid for query in queries for sid in session.scalars(query) if sid is not None}
    return sorted(ids)


def _referenced_players(session: Session, source_ids: list[int]) -> set[int]:
    """Players the typed rows derived from these sources point at."""
    queries = (
        select(Injury.player_id).where(Injury.source_id.in_(source_ids)),
        select(PlayerGameStat.player_id).where(PlayerGameStat.source_id.in_(source_ids)),
        select(Market.player_id)
        .join(OddsSnapshot, OddsSnapshot.market_id == Market.id)
        .where(OddsSnapshot.source_id.in_(source_ids)),
    )
    return {pid for query in queries for pid in session.scalars(query) if pid is not None}


def _xref_record(provider: str, provider_id: str, teams: dict[int, str]) -> dict[str, Any]:
    name, sep, suffix = provider_id.rpartition("|")
    if sep and (suffix == "" or (suffix.isdigit() and int(suffix) in teams)):
        return {"provider": provider, "name": name, "team": teams[int(suffix)] if suffix else None}
    return {"provider": provider, "provider_id": provider_id}


def _player_records(session: Session, source_ids: list[int]) -> list[dict[str, Any]]:
    teams = {tid: abbr for tid, abbr in session.execute(select(Team.id, Team.abbreviation))}
    players = session.scalars(
        select(Player).where(Player.id.in_(_referenced_players(session, source_ids)))
    ).all()
    xrefs: dict[int, list[dict[str, Any]]] = {}
    for entity_id, provider, provider_id in session.execute(
        select(EntityXref.entity_id, EntityXref.provider, EntityXref.provider_id).where(
            EntityXref.entity_kind == "player",
            EntityXref.entity_id.in_([p.id for p in players]),
        )
    ):
        xrefs.setdefault(entity_id, []).append(_xref_record(provider, provider_id, teams))
    records = [
        {
            "kind": "player",
            "sport": p.sport,
            "full_name": p.full_name,
            "team": None if p.team_id is None else teams[p.team_id],
            "xrefs": sorted(xrefs.get(p.id, []), key=lambda x: sorted(x.items(), key=str)),
        }
        for p in players
    ]
    return sorted(records, key=lambda r: (r["sport"], r["full_name"], str(r["team"])))


def export_bundle(session: Session, capture_run_id: int, out_dir: Path) -> Path:
    """Write the capture run's bundle into `out_dir` and return its path."""
    run = session.get_one(CaptureRun, capture_run_id)
    ids = _source_ids(session, run)
    sources = session.scalars(
        select(Source).where(Source.id.in_(ids)).order_by(Source.fetched_at, Source.id)
    ).all()
    refs = {s.id: ref for ref, s in enumerate(sources)}
    records: list[dict[str, Any]] = [
        {
            "kind": "source",
            "ref": refs[s.id],
            "provider": s.provider,
            "endpoint": s.endpoint,
            "params": s.params,
            "payload": s.payload,
            "fetched_at": _iso(s.fetched_at),
        }
        for s in sources
    ]
    observations = session.execute(
        select(
            SourceObservation.source_id,
            SourceObservation.observed_at,
            SourceObservation.purpose,
        ).where(SourceObservation.source_id.in_(ids))
    ).all()
    records += sorted(
        (
            {
                "kind": "observation",
                "source_ref": refs[source_id],
                "observed_at": _iso(observed_at),
                "purpose": purpose,
            }
            for source_id, observed_at, purpose in observations
        ),
        key=lambda r: (r["source_ref"], r["observed_at"], r["purpose"] or ""),
    )
    records += _player_records(session, ids)
    revision = session.scalar(text("SELECT version_num FROM alembic_version"))
    return write_bundle(
        out_dir,
        sport=run.sport,
        slate_date=run.slate_date,
        capture_run_id=run.id,
        alembic_revision=revision,
        records=records,
    )
