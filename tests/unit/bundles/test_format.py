"""Slate bundle file format: manifest line + hashed jsonl body, gzipped."""

from __future__ import annotations

import gzip
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from sharpline.bundles.format import (
    BundleIntegrityError,
    BundleManifest,
    read_bundle,
    write_bundle,
)

SLATE = date(2026, 11, 4)
RECORDS: list[dict[str, Any]] = [
    {
        "kind": "source",
        "ref": 0,
        "provider": "espn",
        "endpoint": "scoreboard",
        "params": {"dates": "20261104"},
        "payload": {"events": [{"id": "e1", "name": "Nikola Jokić"}]},
        "fetched_at": "2026-11-04T23:00:00+00:00",
    },
    {"kind": "observation", "source_ref": 0, "observed_at": "2026-11-04T23:00:00+00:00"},
]


def _write(tmp_path: Path) -> Path:
    return write_bundle(
        tmp_path,
        sport="nba",
        slate_date=SLATE,
        capture_run_id=7,
        alembic_revision="0003",
        records=RECORDS,
    )


def _lines(path: Path) -> list[bytes]:
    with gzip.open(path, "rb") as f:
        return f.read().splitlines(keepends=True)


def _rewrite(path: Path, lines: list[bytes]) -> None:
    with gzip.open(path, "wb") as f:
        f.write(b"".join(lines))


def test_roundtrip_name_manifest_and_records(tmp_path: Path) -> None:
    path = _write(tmp_path)
    manifest, records = read_bundle(path)

    assert isinstance(manifest, BundleManifest)
    assert re.fullmatch(r"nba-2026-11-04-[0-9a-f]{8}\.jsonl\.gz", path.name)
    assert path.name == f"nba-2026-11-04-{manifest.content_sha256[:8]}.jsonl.gz"
    assert (manifest.format_version, manifest.sport, manifest.slate_date) == (1, "nba", SLATE)
    assert (manifest.capture_run_id, manifest.alembic_revision) == (7, "0003")
    assert manifest.counts == {"source": 1, "observation": 1}
    assert manifest.sharpline_version
    assert records == RECORDS


def test_export_is_deterministic(tmp_path: Path) -> None:
    first = _write(tmp_path / "a")
    second = _write(tmp_path / "b")
    assert first.name == second.name
    assert first.read_bytes() == second.read_bytes()


def test_manifest_hash_detects_tampering(tmp_path: Path) -> None:
    path = _write(tmp_path)
    lines = _lines(path)
    lines[1] = lines[1].replace(b"e1", b"e9")  # same length, different content
    _rewrite(path, lines)

    with pytest.raises(BundleIntegrityError, match="content_sha256"):
        read_bundle(path)


def test_dropped_line_detected(tmp_path: Path) -> None:
    path = _write(tmp_path)
    _rewrite(path, _lines(path)[:-1])

    with pytest.raises(BundleIntegrityError):
        read_bundle(path)


def test_unknown_format_version_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path)
    lines = _lines(path)
    manifest = json.loads(lines[0])
    manifest["format_version"] = 2
    lines[0] = (json.dumps(manifest) + "\n").encode()
    _rewrite(path, lines)

    with pytest.raises(BundleIntegrityError, match="format_version"):
        read_bundle(path)


def test_not_a_bundle_rejected(tmp_path: Path) -> None:
    path = tmp_path / "empty.jsonl.gz"
    _rewrite(path, [])
    with pytest.raises(BundleIntegrityError):
        read_bundle(path)
