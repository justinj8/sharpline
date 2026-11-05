"""The 30 NBA franchises with each provider's identifier, plus an idempotent DB seed.

Literal data: ids/abbreviations from nba_api's bundled team list and ESPN's public
teams endpoint (fetched once). The Odds API identifies teams by full franchise name.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from sharpline.db.models import EntityXref, Team
from sharpline.domain.enums import Sport

ESPN = "espn"
ODDS_API = "odds_api"
NBA_STATS = "nba_stats"


@dataclass(frozen=True)
class NbaTeam:
    abbr: str  # ours == nba_api abbreviation
    name: str  # nba_api full name
    espn_abbr: str
    espn_id: str
    odds_api_name: str
    nba_api_id: int


NBA_TEAMS: tuple[NbaTeam, ...] = (
    NbaTeam("ATL", "Atlanta Hawks", "ATL", "1", "Atlanta Hawks", 1610612737),
    NbaTeam("BKN", "Brooklyn Nets", "BKN", "17", "Brooklyn Nets", 1610612751),
    NbaTeam("BOS", "Boston Celtics", "BOS", "2", "Boston Celtics", 1610612738),
    NbaTeam("CHA", "Charlotte Hornets", "CHA", "30", "Charlotte Hornets", 1610612766),
    NbaTeam("CHI", "Chicago Bulls", "CHI", "4", "Chicago Bulls", 1610612741),
    NbaTeam("CLE", "Cleveland Cavaliers", "CLE", "5", "Cleveland Cavaliers", 1610612739),
    NbaTeam("DAL", "Dallas Mavericks", "DAL", "6", "Dallas Mavericks", 1610612742),
    NbaTeam("DEN", "Denver Nuggets", "DEN", "7", "Denver Nuggets", 1610612743),
    NbaTeam("DET", "Detroit Pistons", "DET", "8", "Detroit Pistons", 1610612765),
    NbaTeam("GSW", "Golden State Warriors", "GS", "9", "Golden State Warriors", 1610612744),
    NbaTeam("HOU", "Houston Rockets", "HOU", "10", "Houston Rockets", 1610612745),
    NbaTeam("IND", "Indiana Pacers", "IND", "11", "Indiana Pacers", 1610612754),
    NbaTeam("LAC", "Los Angeles Clippers", "LAC", "12", "Los Angeles Clippers", 1610612746),
    NbaTeam("LAL", "Los Angeles Lakers", "LAL", "13", "Los Angeles Lakers", 1610612747),
    NbaTeam("MEM", "Memphis Grizzlies", "MEM", "29", "Memphis Grizzlies", 1610612763),
    NbaTeam("MIA", "Miami Heat", "MIA", "14", "Miami Heat", 1610612748),
    NbaTeam("MIL", "Milwaukee Bucks", "MIL", "15", "Milwaukee Bucks", 1610612749),
    NbaTeam("MIN", "Minnesota Timberwolves", "MIN", "16", "Minnesota Timberwolves", 1610612750),
    NbaTeam("NOP", "New Orleans Pelicans", "NO", "3", "New Orleans Pelicans", 1610612740),
    NbaTeam("NYK", "New York Knicks", "NY", "18", "New York Knicks", 1610612752),
    NbaTeam("OKC", "Oklahoma City Thunder", "OKC", "25", "Oklahoma City Thunder", 1610612760),
    NbaTeam("ORL", "Orlando Magic", "ORL", "19", "Orlando Magic", 1610612753),
    NbaTeam("PHI", "Philadelphia 76ers", "PHI", "20", "Philadelphia 76ers", 1610612755),
    NbaTeam("PHX", "Phoenix Suns", "PHX", "21", "Phoenix Suns", 1610612756),
    NbaTeam("POR", "Portland Trail Blazers", "POR", "22", "Portland Trail Blazers", 1610612757),
    NbaTeam("SAC", "Sacramento Kings", "SAC", "23", "Sacramento Kings", 1610612758),
    NbaTeam("SAS", "San Antonio Spurs", "SA", "24", "San Antonio Spurs", 1610612759),
    NbaTeam("TOR", "Toronto Raptors", "TOR", "28", "Toronto Raptors", 1610612761),
    NbaTeam("UTA", "Utah Jazz", "UTAH", "26", "Utah Jazz", 1610612762),
    NbaTeam("WAS", "Washington Wizards", "WSH", "27", "Washington Wizards", 1610612764),
)


def seed_nba_teams(session: Session) -> int:
    """Upsert the teams and their provider xrefs; idempotent. Flushes, never commits."""
    for t in NBA_TEAMS:
        team = session.scalar(
            select(Team).where(Team.sport == Sport.NBA, Team.abbreviation == t.abbr)
        )
        if team is None:
            team = Team(sport=Sport.NBA, name=t.name, abbreviation=t.abbr)
            session.add(team)
        else:
            team.name = t.name
        session.flush()
        for provider, provider_id in (
            (ESPN, t.espn_abbr),
            (ESPN, t.espn_id),
            (ODDS_API, t.odds_api_name),
            (NBA_STATS, str(t.nba_api_id)),
        ):
            xref = session.scalar(
                select(EntityXref).where(
                    EntityXref.provider == provider,
                    EntityXref.entity_kind == "team",
                    EntityXref.provider_id == provider_id,
                )
            )
            if xref is None:
                session.add(
                    EntityXref(
                        entity_kind="team",
                        entity_id=team.id,
                        provider=provider,
                        provider_id=provider_id,
                    )
                )
            else:
                xref.entity_id = team.id
    session.flush()
    return len(NBA_TEAMS)
