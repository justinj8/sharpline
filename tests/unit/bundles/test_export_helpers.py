"""Pure helpers of bundle export/load: the unfinished-run window and portable xrefs."""

from __future__ import annotations

from datetime import UTC, date, datetime

from sharpline.bundles.export import _xref_record, observation_window_end
from sharpline.bundles.load import _xref_key

SLATE = date(2026, 11, 4)
CUTOFF = datetime(2026, 11, 5, 11, 0, tzinfo=UTC)  # 06:00 EST on Nov 5


def test_finished_run_window_ends_at_finished_at() -> None:
    finished = datetime(2026, 11, 5, 2, 30, tzinfo=UTC)
    later = datetime(2026, 11, 9, tzinfo=UTC)
    assert observation_window_end(SLATE, finished, now=later) == finished


def test_unfinished_run_window_ends_at_next_morning_cutoff() -> None:
    """I2: a run killed without finishing must not sweep in later days' fetches."""
    assert observation_window_end(SLATE, None, now=datetime(2026, 11, 9, tzinfo=UTC)) == CUTOFF


def test_unfinished_run_exported_mid_session_ends_now() -> None:
    now = datetime(2026, 11, 5, 1, 0, tzinfo=UTC)
    assert observation_window_end(SLATE, None, now=now) == now


def test_cutoff_follows_daylight_saving() -> None:
    summer = date(2026, 4, 10)  # EDT: 06:00 ET is 10:00 UTC
    far = datetime(2026, 5, 1, tzinfo=UTC)
    assert observation_window_end(summer, None, now=far) == datetime(2026, 4, 11, 10, 0, tzinfo=UTC)


def test_team_scoped_xref_survives_different_team_ids() -> None:
    """D2 / M5a: `name|<team id>` keys are exported by abbreviation and rebuilt with the
    loading database's own team id, which need not match the exporter's."""
    record = _xref_record("odds_api", "Jayson Tatum|17", {17: "BOS", 3: "NYK"})
    assert record == {"provider": "odds_api", "name": "Jayson Tatum", "team": "BOS"}
    assert _xref_key(record, {"BOS": 2, "NYK": 18}) == ("odds_api", "Jayson Tatum|2")
    assert _xref_key(record, {"NYK": 18}) is None  # team unknown here: dropped, not guessed

    teamless = _xref_record("odds_api", "Jayson Tatum|", {17: "BOS"})
    assert _xref_key(teamless, {}) == ("odds_api", "Jayson Tatum|")

    for provider, provider_id in (("nba_stats", "1628369"), ("espn", "2:Jayson Tatum")):
        plain = _xref_record(provider, provider_id, {17: "BOS"})
        assert _xref_key(plain, {}) == (provider, provider_id)
    unknown_team = _xref_record("odds_api", "A|B|99", {17: "BOS"})
    assert _xref_key(unknown_team, {}) == ("odds_api", "A|B|99")
