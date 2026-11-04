"""Closed vocabularies shared by every sport module and stored as text in Postgres."""

from enum import StrEnum


class Sport(StrEnum):
    NBA = "nba"
    NFL = "nfl"
    MLB = "mlb"


class MarketType(StrEnum):
    MONEYLINE = "moneyline"
    SPREAD = "spread"
    TOTAL = "total"
    PLAYER_PROP = "player_prop"
    SPECIAL = "special"


class Period(StrEnum):
    FULL = "FULL"
    H1 = "H1"  # first half (NFL/NBA)
    Q1 = "Q1"  # first quarter
    F5 = "F5"  # MLB first five innings
    I1 = "I1"  # MLB first inning (NRFI/YRFI)


class Side(StrEnum):
    HOME = "HOME"
    AWAY = "AWAY"
    OVER = "OVER"
    UNDER = "UNDER"
    YES = "YES"
    NO = "NO"
