"""Runtime settings, loaded from environment variables (and `.env` locally).

Secrets are typed as `SecretStr` so they never appear in reprs or logs.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "test", "staging", "production"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    # Contains the DB password: keep it out of repr()/str() and therefore out of logs.
    database_url: str = Field(repr=False)
    # Read from SHARPLINE_ENV (a bare ENV variable is too generic to trust).
    env: Environment = Field(default="local", validation_alias=AliasChoices("SHARPLINE_ENV", "env"))
    log_level: str = "INFO"
    anthropic_api_key: SecretStr | None = None
    odds_api_key: SecretStr | None = None
    schedule_timezone: str = "America/New_York"
    db_connect_timeout_s: int = 3


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings, read once."""
    return Settings()
