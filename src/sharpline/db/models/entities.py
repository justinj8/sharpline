"""Canonical teams/players plus the cross-provider ID map (`entity_xref`)."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from sharpline.db.base import Base, in_values
from sharpline.domain.enums import Sport

ENTITY_KINDS = ("team", "player", "event")


class Team(Base):
    __tablename__ = "teams"
    __table_args__ = (
        UniqueConstraint("sport", "abbreviation"),
        CheckConstraint(in_values("sport", Sport), name="sport"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    sport: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    abbreviation: Mapped[str] = mapped_column(Text)


class Player(Base):
    __tablename__ = "players"
    __table_args__ = (
        Index(None, "sport", "full_name"),
        CheckConstraint(in_values("sport", Sport), name="sport"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    sport: Mapped[str] = mapped_column(Text)
    full_name: Mapped[str] = mapped_column(Text)
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id"))
    position: Mapped[str | None] = mapped_column(Text)


class EntityXref(Base):
    """Maps one canonical entity to each provider's identifier for it."""

    __tablename__ = "entity_xref"
    __table_args__ = (
        UniqueConstraint("provider", "entity_kind", "provider_id"),
        CheckConstraint(in_values("entity_kind", ENTITY_KINDS), name="entity_kind"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    entity_kind: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[int] = mapped_column(BigInteger)
    provider: Mapped[str] = mapped_column(Text)
    provider_id: Mapped[str] = mapped_column(Text)
