"""Normalized provider rows: the DTOs every source client parses into."""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, field_validator

from sharpline.domain.enums import EventStatus, MarketType, Period, Side, Sport
from sharpline.domain.time import ensure_utc


class _Row(BaseModel):
    model_config = ConfigDict(frozen=True)


class EventRow(_Row):
    provider: str
    provider_event_id: str
    sport: Sport
    home_team: str
    away_team: str
    commence_time: datetime
    # What the provider says about the game; None when it says nothing (the Odds API).
    status: EventStatus | None = None

    @field_validator("commence_time")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return ensure_utc(v)


class OddsRow(_Row):
    provider_event_id: str
    market_key: str
    market_type: MarketType
    period: Period
    stat: str | None
    player_name: str | None
    book: str
    side: Side
    price: int
    line: Decimal | None
    captured_at: datetime

    @field_validator("captured_at")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return ensure_utc(v)


class InjuryRow(_Row):
    team_provider_id: str
    player_name: str
    status: str
    detail: str | None
    reported_at: datetime | None

    @field_validator("reported_at")
    @classmethod
    def _utc(cls, v: datetime | None) -> datetime | None:
        return None if v is None else ensure_utc(v)


class ResultRow(_Row):
    provider_event_id: str
    home_score: int
    away_score: int
    final_at: datetime | None

    @field_validator("final_at")
    @classmethod
    def _utc(cls, v: datetime | None) -> datetime | None:
        return None if v is None else ensure_utc(v)


class PlayerGameStatRow(_Row):
    provider_game_id: str
    provider_player_id: str
    player_name: str
    team_abbr: str
    game_date: date
    matchup: str
    stats: dict[str, float]
