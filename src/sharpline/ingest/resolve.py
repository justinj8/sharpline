"""Cross-provider entity resolution: exact xref, unique name match, else review queue.

The resolver never commits; it flushes as needed and callers own the transaction.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

from rapidfuzz import fuzz, process
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from sharpline.db.models import EntityReview, EntityXref, Player
from sharpline.domain.enums import Sport
from sharpline.ingest.normalize import normalize_name

NBA_STATS = "nba_stats"
FUZZY_CUTOFF = 90
MAX_CANDIDATES = 5


@dataclass(frozen=True)
class Resolved:
    entity_id: int


@dataclass(frozen=True)
class NeedsReview:
    review_id: int


Resolution = Resolved | NeedsReview


class EntityResolver:
    def __init__(self, session: Session) -> None:
        self._session = session

    def team(self, provider: str, provider_id: str) -> Resolution:
        entity_id = self._xref(provider, "team", provider_id)
        if entity_id is not None:
            return Resolved(entity_id)
        review_id = self._upsert_review(
            provider, "team", provider_id, raw_name=provider_id, context={}, candidates=[]
        )
        return NeedsReview(review_id)

    def player(
        self,
        provider: str,
        provider_id: str,
        raw_name: str,
        *,
        sport: Sport,
        team_id: int | None,
        prefer_team_ids: Collection[int] = (),
    ) -> Resolution:
        """`team_id` restricts name matches to that team; `prefer_team_ids` only breaks ties
        (a unique match among those teams wins, else the sport-wide match is used)."""
        entity_id = self._xref(provider, "player", provider_id)
        if entity_id is not None:
            return Resolved(entity_id)

        target = normalize_name(raw_name)
        players = self._session.execute(
            select(Player.id, Player.full_name, Player.team_id).where(Player.sport == sport)
        ).all()
        same_name = [
            p
            for p in players
            if normalize_name(p.full_name) == target and (team_id is None or p.team_id == team_id)
        ]
        preferred = [p for p in same_name if p.team_id in prefer_team_ids]
        exact = [p.id for p in (preferred if len(preferred) == 1 else same_name)]
        if len(exact) == 1:
            self._add_xref("player", exact[0], provider, provider_id)
            return Resolved(exact[0])

        if not exact and provider == NBA_STATS:
            player = Player(sport=sport, full_name=raw_name, team_id=team_id)
            self._session.add(player)
            self._session.flush()
            self._add_xref("player", player.id, provider, provider_id)
            return Resolved(player.id)

        names = {p.id: normalize_name(p.full_name) for p in players}
        matches = process.extract(
            target,
            names,
            scorer=fuzz.token_sort_ratio,
            score_cutoff=FUZZY_CUTOFF,
            limit=MAX_CANDIDATES,
        )
        full_names = {p.id: p.full_name for p in players}
        candidates = [
            {"id": int(pid), "name": full_names[pid], "score": float(score)}
            for _, score, pid in matches
        ]
        review_id = self._upsert_review(
            provider,
            "player",
            provider_id,
            raw_name=raw_name,
            context={"sport": str(sport), "team_id": team_id},
            candidates=candidates,
        )
        return NeedsReview(review_id)

    def resolve_review(self, review_id: int, entity_id: int) -> None:
        review = self._session.get(EntityReview, review_id)
        if review is None:
            raise ValueError(f"entity_review {review_id} not found")
        if self._xref(review.provider, review.entity_kind, review.provider_id) is None:
            self._add_xref(review.entity_kind, entity_id, review.provider, review.provider_id)
        review.status = "resolved"
        review.resolved_entity_id = entity_id
        self._session.flush()

    def _xref(self, provider: str, kind: str, provider_id: str) -> int | None:
        return self._session.scalar(
            select(EntityXref.entity_id).where(
                EntityXref.provider == provider,
                EntityXref.entity_kind == kind,
                EntityXref.provider_id == provider_id,
            )
        )

    def _add_xref(self, kind: str, entity_id: int, provider: str, provider_id: str) -> None:
        self._session.add(
            EntityXref(
                entity_kind=kind, entity_id=entity_id, provider=provider, provider_id=provider_id
            )
        )
        self._session.flush()

    def _upsert_review(
        self,
        provider: str,
        kind: str,
        provider_id: str,
        *,
        raw_name: str,
        context: dict[str, Any],
        candidates: list[dict[str, Any]],
    ) -> int:
        stmt = (
            insert(EntityReview)
            .values(
                provider=provider,
                entity_kind=kind,
                provider_id=provider_id,
                raw_name=raw_name,
                context=context,
                candidates=candidates,
            )
            .on_conflict_do_update(
                index_elements=["provider", "entity_kind", "provider_id"],
                set_={"raw_name": raw_name, "context": context, "candidates": candidates},
            )
            .returning(EntityReview.id)
        )
        review_id = int(self._session.execute(stmt).scalar_one())
        # The Core upsert bypasses the identity map: expire just that review (if loaded)
        # so it reloads, leaving the rest of the caller's session untouched.
        loaded = self._session.identity_map.get(self._session.identity_key(EntityReview, review_id))
        if loaded is not None:
            self._session.expire(loaded)
        return review_id
