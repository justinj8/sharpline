import pytest

from sharpline.sports.nba.teams import NBA_TEAMS


def test_thirty_teams_with_unique_ids() -> None:
    assert len(NBA_TEAMS) == 30
    for field in ("abbr", "espn_id", "odds_api_name", "nba_api_id", "espn_abbr", "name"):
        values = [getattr(t, field) for t in NBA_TEAMS]
        assert len(set(values)) == 30, field


@pytest.mark.parametrize(
    ("espn", "ours"),
    [
        ("GS", "GSW"),
        ("NY", "NYK"),
        ("SA", "SAS"),
        ("NO", "NOP"),
        ("UTAH", "UTA"),
        ("WSH", "WAS"),
        ("LAC", "LAC"),
    ],
)
def test_espn_abbreviation_divergences(espn: str, ours: str) -> None:
    team = next(t for t in NBA_TEAMS if t.abbr == ours)
    assert team.espn_abbr == espn


def test_clippers_names_across_providers() -> None:
    lac = next(t for t in NBA_TEAMS if t.abbr == "LAC")
    assert lac.odds_api_name == "Los Angeles Clippers"
    assert lac.name == "Los Angeles Clippers"
