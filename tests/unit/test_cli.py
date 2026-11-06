"""`sharpline` CLI without a database: help, and clean one-line errors (never a secret)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sharpline.bundles.format import write_bundle
from sharpline.cli import app

runner = CliRunner()
# Port 1 on loopback refuses at once: a DATABASE_URL that can never be reached.
UNREACHABLE_DB = "postgresql://sharpline:hunter2-db-pass@127.0.0.1:1/sharpline"


def _commands(*args: str) -> str:
    result = runner.invoke(app, [*args, "--help"])
    assert result.exit_code == 0, result.output
    return result.output


def test_help_lists_every_command() -> None:
    top = _commands()
    for name in ("capture", "results", "backfill", "bundle", "reparse", "review", "quota"):
        assert name in top
    assert "nba" in _commands("capture")
    assert "nba" in _commands("results")
    assert "nba-stats" in _commands("backfill")
    bundle = _commands("bundle")
    for name in ("export", "push", "pull", "load", "list"):
        assert name in bundle
    review = _commands("review")
    for name in ("list", "resolve", "reject"):
        assert name in review
    capture = _commands("capture", "nba")
    for option in ("--date", "--cap", "--dry-run", "--offline"):
        assert option in capture


def _assert_clean(output: str) -> None:
    assert "Traceback" not in output
    assert "hunter2" not in output and "127.0.0.1" not in output


@pytest.mark.usefixtures("cli_env")
def test_missing_database_url_is_one_clean_line() -> None:
    result = runner.invoke(app, ["quota"])
    assert result.exit_code == 1
    assert result.output.strip() == "error: DATABASE_URL is not set"


def test_unreachable_database_never_echoes_the_url(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", UNREACHABLE_DB)
    result = runner.invoke(app, ["quota"])
    assert result.exit_code == 1
    assert result.output.startswith("error: could not connect to the database")
    _assert_clean(result.output)


def test_missing_bundle_credentials_is_one_clean_line(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BUNDLE_BUCKET", "sharpline-bundles")
    monkeypatch.setenv("BUNDLE_ENDPOINT_URL", "https://example.invalid")
    monkeypatch.setenv("BUNDLE_ACCESS_KEY_ID", "AKIA-very-secret-id")
    result = runner.invoke(app, ["bundle", "list"])
    assert result.exit_code == 1
    assert result.output.strip() == (
        "error: BUNDLE_BUCKET is set but BUNDLE_SECRET_ACCESS_KEY is missing"
    )
    assert "AKIA" not in result.output


def test_corrupt_bundle_fails_before_touching_the_database(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", UNREACHABLE_DB)  # any DB access would fail loudly
    good = write_bundle(
        cli_env,
        sport="nba",
        slate_date=date(2026, 11, 4),
        capture_run_id=1,
        alembic_revision=None,
        records=[],
    )
    bad = cli_env / "nba-2026-11-05-deadbeef.jsonl.gz"
    bad.write_bytes(b"not a gzip stream")
    result = runner.invoke(app, ["bundle", "load", str(good), str(bad)])
    assert result.exit_code == 1
    assert result.output.startswith(
        "error: nba-2026-11-05-deadbeef.jsonl.gz: not a readable bundle"
    )
    assert len(result.output.strip().splitlines()) == 1
    _assert_clean(result.output)


@pytest.mark.usefixtures("cli_env")
def test_bundle_push_of_missing_file_is_clean() -> None:
    result = runner.invoke(app, ["bundle", "push", "nope.jsonl.gz"])
    assert result.exit_code == 1
    assert result.output.strip() == "error: no such file: nope.jsonl.gz"


@pytest.mark.usefixtures("cli_env")
def test_local_bundle_store_push_list_pull(tmp_path: Path) -> None:
    path = write_bundle(
        tmp_path / "out",
        sport="nba",
        slate_date=date(2026, 11, 4),
        capture_run_id=1,
        alembic_revision=None,
        records=[],
    )
    assert runner.invoke(app, ["bundle", "list"]).output.strip() == "no bundles"
    pushed = runner.invoke(app, ["bundle", "push", str(path)])
    assert pushed.exit_code == 0 and pushed.output.strip() == f"pushed {path.name}"
    assert runner.invoke(app, ["bundle", "list"]).output.strip() == path.name
    # The default destination is the local store itself: pulling must not fail on the copy.
    pulled = runner.invoke(app, ["bundle", "pull", path.name])
    assert pulled.exit_code == 0, pulled.output
    assert pulled.output.strip() == f"pulled {Path('var/bundles') / path.name}"
    elsewhere = runner.invoke(app, ["bundle", "pull", path.name, "--dest", "dl"])
    assert elsewhere.exit_code == 0 and (tmp_path / "dl" / path.name).is_file()


def test_provider_http_error_names_status_and_host_only(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    from sharpline.capture import clients
    from sharpline.sources.espn.client import EspnClient
    from sharpline.sources.http import HttpClient

    monkeypatch.setenv("DATABASE_URL", UNREACHABLE_DB)  # the fetch fails before any query

    def espn(_settings: object) -> EspnClient:
        http = HttpClient(
            clients.ESPN_BASE_URL,
            transport=httpx.MockTransport(lambda _r: httpx.Response(503)),
            sleep=lambda _s: None,
            max_retries=0,
        )
        return EspnClient(http)

    monkeypatch.setattr(clients, "build_espn_client", espn)
    monkeypatch.setattr(clients, "build_nba_stats_client", lambda: None)
    result = runner.invoke(app, ["results", "nba", "--date", "2026-11-12"])
    assert result.exit_code == 1
    assert result.output.strip() == (
        "error: provider request failed: HTTP 503 from site.api.espn.com"
    )
    _assert_clean(result.output)
