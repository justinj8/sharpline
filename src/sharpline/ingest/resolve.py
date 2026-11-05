"""Cross-provider entity resolution: exact xref, unique name match, else review queue.

The resolver never commits; it flushes as needed and callers own the transaction.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from rapidfuzz import fuzz, process
from sqlalchemy import Row, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from sharpline.db.models import EntityReview, EntityXref, Player
from sharpline.domain.enums import Sport
from sharpline.ingest.normalize import normalize_name

NBA_STATS = "nba_stats"
FUZZY_CUTOFF = 90
MAX_CANDIDATES = 5
TEAM_SCOPE = "team"


def team_scoped_key(raw_name: str, team_id: int | None) -> str:
    """Xref key for a provider that names players without ids or teams (R7a)."""
    return f"{raw_name}|{'' if team_id is None else team_id}"


def _same_name(
    players: Sequence[Row[int, str, int | None]], raw_name: str
) -> list[Row[int, str, int | None]]:
    target = normalize_name(raw_name)
    return [p for p in players if normalize_name(p.full_name) == target]


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
    ) -> Resolution:
        """`team_id` restricts name matches to that team."""
        entity_id = self._xref(provider, "player", provider_id)
        if entity_id is not None:
            return Resolved(entity_id)

        players = self._players(sport)
        exact = [p.id for p in _same_name(players, raw_name) if team_id in (None, p.team_id)]
        if len(exact) == 1:
            self._add_xref("player", exact[0], provider, provider_id)
            return Resolved(exact[0])

        if not exact and provider == NBA_STATS:
            player = Player(sport=sport, full_name=raw_name, team_id=team_id)
            self._session.add(player)
            self._session.flush()
            self._add_xref("player", player.id, provider, provider_id)
            return Resolved(player.id)

        context = {"sport": str(sport), "team_id": team_id}
        return self._review_player(provider, provider_id, raw_name, players, context)

    def player_in_event(
        self, provider: str, raw_name: str, *, sport: Sport, team_ids: Sequence[int]
    ) -> Resolution:
        """Resolve a bare name quoted on an event (Odds API props) without merging namesakes.

        Xrefs are keyed `name|our_team_id` (R7a): the keys for the event's teams are tried
        first, then a unique name match within those teams, then a unique sport-wide match.
        """
        hits = {
            entity_id
            for team_id in team_ids
            if (entity_id := self._xref(provider, "player", team_scoped_key(raw_name, team_id)))
            is not None
        }
        if len(hits) == 1:
            return Resolved(hits.pop())

        players = self._players(sport)
        same_name = _same_name(players, raw_name)
        candidates = [p for p in same_name if p.team_id in team_ids] or same_name
        if len(candidates) == 1:
            player = candidates[0]
            key = team_scoped_key(raw_name, player.team_id)
            if self._xref(provider, "player", key) is None:
                self._add_xref("player", player.id, provider, key)
            return Resolved(player.id)

        review_key = f"{raw_name}|{','.join(str(t) for t in sorted(team_ids))}"
        context = {"sport": str(sport), "team_ids": sorted(team_ids), "scope": TEAM_SCOPE}
        return self._review_player(provider, review_key, raw_name, players, context)

    def resolve_review(self, review_id: int, entity_id: int) -> None:
        review = self._session.get(EntityReview, review_id)
        if review is None:
            raise ValueError(f"entity_review {review_id} not found")
        provider_id = review.provider_id
        if review.context.get("scope") == TEAM_SCOPE:
            team_id = self._session.get_one(Player, entity_id).team_id
            provider_id = team_scoped_key(review.raw_name, team_id)
        if self._xref(review.provider, review.entity_kind, provider_id) is None:
            self._add_xref(review.entity_kind, entity_id, review.provider, provider_id)
        review.status = "resolved"
        review.resolved_entity_id = entity_id
        self._session.flush()

    def _players(self, sport: Sport) -> Sequence[Row[int, str, int | None]]:
        return self._session.execute(
            select(Player.id, Player.full_name, Player.team_id).where(Player.sport == sport)
        ).all()

    def _review_player(
        self,
        provider: str,
        provider_id: str,
        raw_name: str,
        players: Sequence[Row[int, str, int | None]],
        context: dict[str, Any],
    ) -> NeedsReview:
        names = {p.id: normalize_name(p.full_name) for p in players}
        matches = process.extract(
            normalize_name(raw_name),
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
            context=context,
            candidates=candidates,
        )
        return NeedsReview(review_id)

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
