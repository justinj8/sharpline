"""Migration 0002 applies procrastinate's schema vendored at one exact version.

If the installed procrastinate ever differs from that version, the worker would call
SQL functions the migration never created, while `alembic current` still says head.
These tests make that drift a CI failure instead of a production one.
"""

import tomllib
from importlib.metadata import version
from pathlib import Path

import procrastinate

ROOT = Path(__file__).resolve().parents[3]
VENDORED = sorted((ROOT / "db" / "migrations" / "sql").glob("procrastinate_*_schema.sql"))


def _vendored_version() -> str:
    assert len(VENDORED) == 1, f"expected one vendored schema, found {VENDORED}"
    return VENDORED[0].name.removeprefix("procrastinate_").removesuffix("_schema.sql")


def test_worker_pins_procrastinate_to_vendored_version() -> None:
    deps = tomllib.loads((ROOT / "apps" / "worker" / "pyproject.toml").read_text())["project"][
        "dependencies"
    ]
    assert f"procrastinate=={_vendored_version()}" in deps


def test_installed_procrastinate_matches_vendored_schema() -> None:
    assert version("procrastinate") == _vendored_version()
    installed = (Path(procrastinate.__file__).parent / "sql" / "schema.sql").read_text()
    vendored_body = VENDORED[0].read_text().split("\n", 1)[1]
    assert vendored_body == installed
