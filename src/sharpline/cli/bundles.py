"""`sharpline bundle`: export a capture run, move bundles through the store, load them."""

from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer
from botocore.exceptions import BotoCoreError, ClientError  # type: ignore[import-untyped]

from sharpline.bundles.export import export_bundle
from sharpline.bundles.format import BundleManifest, read_bundle
from sharpline.bundles.load import LoadReport, load_bundle
from sharpline.bundles.store import DEFAULT_LOCAL_ROOT, BundleStore, store_from_settings
from sharpline.cli._common import CliError, cli_command, database, load_settings, stats_line
from sharpline.db.models import CaptureRun

bundle_app = typer.Typer(help="Slate bundles: export, push, pull, list and load.")


def _store() -> BundleStore:
    try:
        return store_from_settings(load_settings(need_database=False))
    except ValueError as exc:  # names the missing variable, never a value
        raise CliError(str(exc)) from None


def _with_store[T](action: Callable[[BundleStore], T]) -> T:
    store = _store()
    try:
        return action(store)
    except (BotoCoreError, ClientError) as exc:
        # boto messages can carry the endpoint and request details: keep it to the kind.
        code = exc.response.get("Error", {}).get("Code") if isinstance(exc, ClientError) else None
        raise CliError(f"bundle store request failed: {code or type(exc).__name__}") from None
    except (FileNotFoundError, ValueError) as exc:
        raise CliError(str(exc)) from None


def _existing(file: Path) -> Path:
    if not file.is_file():
        raise CliError(f"no such file: {file}")
    return file


@bundle_app.command("export")
@cli_command
def export(
    capture_run_id: int,
    out: Annotated[Path, typer.Option("--out", help="Directory to write into.")] = (
        DEFAULT_LOCAL_ROOT
    ),
) -> None:
    """Write a capture run's slate bundle."""
    settings = load_settings()
    with database(settings) as sessions, sessions() as session:
        if session.get(CaptureRun, capture_run_id) is None:
            raise CliError(f"capture run {capture_run_id} not found")
        path = export_bundle(session, capture_run_id, out)
    typer.echo(f"exported {path}")


@bundle_app.command("push")
@cli_command
def push(file: Path) -> None:
    """Upload a bundle file to the bundle store."""
    path = _existing(file)
    name = _with_store(lambda store: store.push(path))
    typer.echo(f"pushed {name}")


@bundle_app.command("pull")
@cli_command
def pull(
    name: str,
    dest: Annotated[Path, typer.Option("--dest", help="Directory to write into.")] = (
        DEFAULT_LOCAL_ROOT
    ),
) -> None:
    """Download a bundle from the bundle store."""
    path = _with_store(lambda store: store.pull(name, dest))
    typer.echo(f"pulled {path}")


@bundle_app.command("list")
@cli_command
def list_bundles() -> None:
    """List the bundles in the bundle store."""
    names = _with_store(lambda store: store.list())
    typer.echo("\n".join(names) if names else "no bundles")


def _report(path: Path, manifest: BundleManifest, report: LoadReport) -> str:
    return (
        f"loaded {path.name} ({manifest.sport} {manifest.slate_date}): "
        f"sources {report.sources_inserted} new / {report.sources_existing} existing, "
        f"observations {report.observations}, players {report.players_created}, "
        f"reparse {stats_line(report.reparsed)}"
    )


@bundle_app.command("load")
@cli_command
def load(files: list[Path]) -> None:
    """Load bundles into the database, oldest slate first.

    Every file is verified before anything is written; a damaged file stops the load.
    """
    # read_bundle verifies the hash and counts: a bad file fails here, before any DB write.
    manifests = [(path, read_bundle(_existing(path))[0]) for path in files]
    manifests.sort(key=lambda pm: (pm[1].slate_date, pm[0].name))
    settings = load_settings()
    with database(settings) as sessions:
        for path, manifest in manifests:
            with sessions() as session:
                report = load_bundle(session, path)
            typer.echo(_report(path, manifest, report))
