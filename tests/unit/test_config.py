import pytest
from pydantic import ValidationError

from sharpline.config import Settings


def test_settings_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@h/d")
    monkeypatch.setenv("ODDS_API_KEY", "super-secret-value")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.odds_api_key is not None
    assert s.odds_api_key.get_secret_value() == "super-secret-value"
    assert "super-secret-value" not in repr(s)


def test_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@h/d")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.env == "local"
    assert s.schedule_timezone == "America/New_York"
    assert s.db_connect_timeout_s == 3


def test_settings_requires_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]
