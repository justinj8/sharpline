"""ORM models. Importing this package registers every table on `Base.metadata`."""

from sharpline.db.models.entities import EntityXref, Player, Team
from sharpline.db.models.ingest import (
    CaptureRun,
    EntityReview,
    Injury,
    PlayerGameStat,
    QuotaLedger,
    Result,
    SourceObservation,
)
from sharpline.db.models.prices import OddsSnapshot
from sharpline.db.models.provenance import Source
from sharpline.db.models.slate import Event, Market

__all__ = [
    "CaptureRun",
    "EntityReview",
    "EntityXref",
    "Event",
    "Injury",
    "Market",
    "OddsSnapshot",
    "Player",
    "PlayerGameStat",
    "QuotaLedger",
    "Result",
    "Source",
    "SourceObservation",
    "Team",
]
