"""Slate bundle file format: manifest line + hashed jsonl body, gzipped."""

from __future__ import annotations

import gzip
import hashlib
import json
import re
from collections import Counter
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


def test_byte_flip_sweep_raises_only_integrity_errors(tmp_path: Path) -> None:
    """I1: any single corrupted byte either still decodes to the same records (gzip header
    fields such as mtime/OS are not covered by the CRC) or raises BundleIntegrityError."""
    original = _write(tmp_path)
    data = original.read_bytes()
    _, expected = read_bundle(original)
    outcomes: Counter[str] = Counter()
    for i in range(len(data)):
        for mask in (0x01, 0x80, 0xFF):
            corrupt = tmp_path / "corrupt.jsonl.gz"
            corrupt.write_bytes(data[:i] + bytes([data[i] ^ mask]) + data[i + 1 :])
            try:
                _, records = read_bundle(corrupt)
            except BundleIntegrityError:
                outcomes["integrity_error"] += 1
            else:
                assert records == expected, (i, mask)
                outcomes["identical"] += 1
    assert outcomes["integrity_error"] > 0  # the sweep really exercised corruption


def test_truncated_gzip_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path)
    data = path.read_bytes()
    for cut in (1, 10, len(data) // 2, len(data) - 1):
        path.write_bytes(data[:cut])
        with pytest.raises(BundleIntegrityError):
            read_bundle(path)


def test_not_gzip_rejected(tmp_path: Path) -> None:
    path = tmp_path / "plain.jsonl.gz"
    path.write_bytes(b'{"format_version": 1}\n')
    with pytest.raises(BundleIntegrityError):
        read_bundle(path)


@pytest.mark.parametrize("body", [b"not json\n", b"[1, 2]\n", b'"text"\n'])
def test_hashed_body_that_is_not_records_rejected(tmp_path: Path, body: bytes) -> None:
    """A crafted file with a valid hash over a body that is not JSON objects."""
    path = _write(tmp_path)
    manifest = json.loads(_lines(path)[0])
    manifest["content_sha256"] = hashlib.sha256(body).hexdigest()
    manifest["counts"] = {}
    _rewrite(path, [(json.dumps(manifest) + "\n").encode(), body])
    with pytest.raises(BundleIntegrityError):
        read_bundle(path)
