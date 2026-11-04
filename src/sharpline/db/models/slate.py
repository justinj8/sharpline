"""Events (games) and the markets offered on them."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from sharpline.db.base import Base, in_values
from sharpline.domain.enums import MarketType, Period, Sport


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("sport", "home_team_id", "away_team_id", "commence_time"),
        Index(None, "sport", "commence_time"),
        CheckConstraint(in_values("sport", Sport), name="sport"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    sport: Mapped[str] = mapped_column(Text)
    season: Mapped[int] = mapped_column(Integer)
    home_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    away_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    commence_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, server_default="scheduled")


class Market(Base):
    """One priceable proposition, e.g. NBA game total 221.5 or Tatum points 27.5."""

    __tablename__ = "markets"
    __table_args__ = (
        # A missing player or line is a real value here ("no player"), so two NULLs
        # must collide instead of slipping past the unique constraint.
        UniqueConstraint(
            "event_id",
            "provider_key",
            "period",
            "player_id",
            "line",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(in_values("market_type", MarketType), name="market_type"),
        CheckConstraint(in_values("period", Period), name="period"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    market_type: Mapped[str] = mapped_column(Text)
    period: Mapped[str] = mapped_column(Text, server_default="FULL")
    provider_key: Mapped[str] = mapped_column(Text)
    stat: Mapped[str | None] = mapped_column(Text)
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"))
    line: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
