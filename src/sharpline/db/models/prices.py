"""`odds_snapshots`: one row per (market, book, side) price observation."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    Text,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column

from sharpline.db.base import Base, in_values
from sharpline.domain.enums import Side


class OddsSnapshot(Base):
    __tablename__ = "odds_snapshots"
    __table_args__ = (
        # Mirrors domain.odds.validate_american: no price strictly between -100 and +100.
        CheckConstraint("price <= -100 OR price >= 100", name="american_price"),
        CheckConstraint(in_values("side", Side), name="side"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"))
    book: Mapped[str] = mapped_column(Text)
    side: Mapped[str] = mapped_column(Text)
    price: Mapped[int] = mapped_column(Integer)
    line: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    is_closing: Mapped[bool] = mapped_column(Boolean, server_default=false())


# "Latest price for this market/book/side" is the hot query (CLV, staleness checks).
Index(
    "ix_odds_snapshots_lookup",
    OddsSnapshot.market_id,
    OddsSnapshot.book,
    OddsSnapshot.side,
    OddsSnapshot.captured_at.desc(),
)
