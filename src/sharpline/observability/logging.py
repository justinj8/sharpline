"""structlog configuration: JSON in deployed environments, console output locally."""

from __future__ import annotations

import logging
from collections.abc import MutableMapping
from typing import Any

import structlog
from pydantic import SecretStr

_REDACTED = "**********"


def _redact_secrets(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Replace any SecretStr value with a fixed mask before rendering."""
    for key, value in event_dict.items():
        if isinstance(value, SecretStr):
            event_dict[key] = _REDACTED
    return event_dict


def configure_logging(level: str, json_output: bool) -> None:
    """Configure structlog process-wide. Safe to call more than once."""
    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact_secrets,
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )
