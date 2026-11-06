"""Shared CLI plumbing: settings, database sessions, output and one-line errors.

Every command runs under `cli_command`, which turns the failures an operator can act on
into `error: <message>` on stderr and exit code 1, never a traceback. Messages name
environment variables, never their values: no DB URL, API key or bucket credential is
ever printed.
"""

import functools
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from typing import NoReturn
from zoneinfo import ZoneInfo

import httpx
import typer
from pydantic import ValidationError
from sqlalchemy import exc as sa_exc
from sqlalchemy.orm import Session, sessionmaker

from sharpline.bundles.format import BundleIntegrityError
from sharpline.capture.plan import ClosingExceedsBudget
from sharpline.capture.runner import PROVIDER_ERRORS
from sharpline.config import Settings
from sharpline.db.session import make_engine, make_session_factory
from sharpline.ingest.writer import WriteStats

EASTERN = ZoneInfo("America/New_York")
# Store-only commands (bundle push/pull/list) need no database; Settings still requires a
# URL, so they get this placeholder, which is never connected to.
_NO_DATABASE = "postgresql://not-configured/none"


class CliError(Exception):
    """A failure the operator can fix; printed as one line."""


def fail(message: str) -> NoReturn:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(code=1)


def warn(message: str) -> None:
    typer.echo(f"warning: {message}", err=True)


def cli_command[**P, R](func: Callable[P, R]) -> Callable[P, R]:
    """Run a command, mapping expected failures to `error: ...` and exit code 1."""

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return func(*args, **kwargs)
        except (CliError, BundleIntegrityError, ClosingExceedsBudget) as exc:
            fail(str(exc))
        except (sa_exc.OperationalError, sa_exc.InterfaceError):
            # The driver's message names the host; the operator only needs the variable.
            fail("could not connect to the database (check DATABASE_URL and that it is up)")
        except httpx.HTTPStatusError as exc:
            # str(exc) carries the full request URL, query string included: status + host only.
            status, host = exc.response.status_code, exc.request.url.host
            fail(f"provider request failed: HTTP {status} from {host}")
        except PROVIDER_ERRORS as exc:
            fail(f"provider request failed: {type(exc).__name__}: {exc}")

    return wrapper


def load_settings(*, need_database: bool = True) -> Settings:
    """Settings from the environment and `.env`; a missing or bad value is one line."""
    try:
        return Settings()
    except ValidationError as exc:
        errors = exc.errors()
        fields = sorted({str(err["loc"][0]) for err in errors if err["loc"]})
        no_db = any(e["loc"] == ("database_url",) and e["type"] == "missing" for e in errors)
        if no_db and fields == ["database_url"] and not need_database:
            return Settings(database_url=_NO_DATABASE)
        if no_db:
            raise CliError("DATABASE_URL is not set") from None
        # Name the settings only: pydantic's message would echo the offending values.
        raise CliError(f"invalid settings: {', '.join(fields)}") from None


@contextmanager
def database(settings: Settings) -> Iterator[sessionmaker[Session]]:
    try:
        engine = make_engine(settings)
    except ValueError:
        raise CliError("DATABASE_URL is not a Postgres URL") from None
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


def stats_line(stats: WriteStats) -> str:
    return f"written {stats.written}, skipped {stats.skipped}, reviews {stats.reviews}"


def eastern(at: datetime, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return f"{at.astimezone(EASTERN).strftime(fmt)} ET"


def table(headers: Sequence[str], rows: Sequence[Sequence[str]], indent: str = "") -> str:
    """Left-aligned columns, two spaces apart."""
    cells = [list(headers), *(list(r) for r in rows)] if headers else [list(r) for r in rows]
    widths = [max(len(row[i]) for row in cells) for i in range(len(cells[0]))]
    return "\n".join(
        indent + "  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)).rstrip()
        for row in cells
    )
