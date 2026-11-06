import json
from collections.abc import Iterator

import pytest
import structlog
from pydantic import SecretStr

from sharpline.observability.logging import configure_logging


@pytest.fixture(autouse=True)
def _restore_structlog() -> Iterator[None]:
    """configure_logging is process-global: put back what was there, so a level filter set
    here cannot silence later tests' `structlog.testing.capture_logs`."""
    saved = structlog.get_config()
    yield
    structlog.reset_defaults()
    structlog.configure(**saved)


def test_json_logs_do_not_leak_secrets(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", json_output=True)
    structlog.get_logger().info("boot", odds_api_key=SecretStr("abc123"))
    out = capsys.readouterr().out
    record = json.loads(out.strip().splitlines()[-1])
    assert record["event"] == "boot"
    assert record["level"] == "info"
    assert "abc123" not in out


def test_log_level_filters_below_threshold(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("WARNING", json_output=True)
    structlog.get_logger().info("quiet")
    assert capsys.readouterr().out == ""
