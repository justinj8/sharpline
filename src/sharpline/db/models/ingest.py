"""Ingest-side tables: observations, injuries, results, box-score stats, review queue, quota."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sharpline.db.base import Base, in_values
from sharpline.db.models.entities import ENTITY_KINDS

INJURY_STATUSES = ("out", "doubtful", "questionable", "probable", "active")
REVIEW_STATUSES = ("open", "resolved", "rejected")
CAPTURE_MODES = ("manual", "live")
CAPTURE_STATUSES = ("running", "completed", "interrupted", "failed")
OBSERVATION_PURPOSES = ("closing",)


class SourceObservation(Base):
    """When a stored source payload was observed; append-only (DB trigger)."""

    __tablename__ = "source_observations"
    __table_args__ = (
        Index(None, "source_id"),
        CheckConstraint(
            "purpose IS NULL OR " + in_values("purpose", OBSERVATION_PURPOSES), name="purpose"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    purpose: Mapped[str | None] = mapped_column(Text)


class Injury(Base):
    __tablename__ = "injuries"
    __table_args__ = (
        Index(None, "player_id", "reported_at"),
        CheckConstraint(in_values("status", INJURY_STATUSES), name="status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id"))
    status: Mapped[str] = mapped_column(Text)
    detail: Mapped[str | None] = mapped_column(Text)
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))


class Result(Base):
    __tablename__ = "results"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), unique=True)
    home_score: Mapped[int] = mapped_column(Integer)
    away_score: Mapped[int] = mapped_column(Integer)
    final_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))


class PlayerGameStat(Base):
    __tablename__ = "player_game_stats"
    __table_args__ = (UniqueConstraint("event_id", "player_id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))


class EntityReview(Base):
    """Provider entities we could not resolve automatically, awaiting a human decision."""

    __tablename__ = "entity_review"
    __table_args__ = (
        UniqueConstraint("provider", "entity_kind", "provider_id"),
        CheckConstraint(in_values("entity_kind", ENTITY_KINDS), name="entity_kind"),
        CheckConstraint(in_values("status", REVIEW_STATUSES), name="status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    provider: Mapped[str] = mapped_column(Text)
    provider_id: Mapped[str] = mapped_column(Text)
    entity_kind: Mapped[str] = mapped_column(Text)
    raw_name: Mapped[str] = mapped_column(Text)
    context: Mapped[dict[str, Any]] = mapped_column(JSONB)
    candidates: Mapped[list[Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text, server_default="open")
    resolved_entity_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CaptureRun(Base):
    __tablename__ = "capture_runs"
    __table_args__ = (
        CheckConstraint(in_values("mode", CAPTURE_MODES), name="mode"),
        CheckConstraint(in_values("status", CAPTURE_STATUSES), name="status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    sport: Mapped[str] = mapped_column(Text)
    slate_date: Mapped[date] = mapped_column(Date)
    mode: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text)
    credits_cap: Mapped[int] = mapped_column(Integer)
    credits_used: Mapped[int] = mapped_column(Integer, server_default="0")
    plan: Mapped[dict[str, Any]] = mapped_column(JSONB)


class QuotaLedger(Base):
    """Odds API credit usage; insert-only by convention."""

    __tablename__ = "quota_ledger"
    __table_args__ = (Index(None, "provider", "observed_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    provider: Mapped[str] = mapped_column(Text)
    cost: Mapped[int] = mapped_column(Integer)
    used: Mapped[int | None] = mapped_column(Integer)
    remaining: Mapped[int | None] = mapped_column(Integer)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"))
    capture_run_id: Mapped[int | None] = mapped_column(ForeignKey("capture_runs.id"))
