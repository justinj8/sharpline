"""Slate bundle file format (spec section 6.1): `<sport>-<slate_date>-<sha8>.jsonl.gz`.

Line 1 is the manifest. Every later line is one record (`{"kind": ...}`), serialized with
sorted keys and no whitespace so the same records always produce the same bytes. The
manifest's `content_sha256` covers the exact bytes of every line after it (newlines
included), and `sha8` is its first 8 hex digits.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from sharpline import __version__

FORMAT_VERSION = 1


class BundleIntegrityError(Exception):
    """The file is not a bundle this code can trust: bad hash, counts or format version."""


class BundleManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    format_version: Literal[1]
    sport: str
    slate_date: date
    capture_run_id: int
    alembic_revision: str | None
    sharpline_version: str
    counts: dict[str, int]
    content_sha256: str


def _line(obj: Mapping[str, Any]) -> bytes:
    text = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return (text + "\n").encode("utf-8")


def write_bundle(
    out_dir: Path,
    *,
    sport: str,
    slate_date: date,
    capture_run_id: int,
    alembic_revision: str | None,
    records: Iterable[Mapping[str, Any]],
) -> Path:
    """Write the records under a manifest; returns the bundle's path in `out_dir`."""
    records = list(records)
    body = [_line(r) for r in records]
    digest = hashlib.sha256(b"".join(body)).hexdigest()
    manifest = BundleManifest(
        format_version=FORMAT_VERSION,
        sport=sport,
        slate_date=slate_date,
        capture_run_id=capture_run_id,
        alembic_revision=alembic_revision,
        sharpline_version=__version__,
        counts=dict(Counter(str(r["kind"]) for r in records)),
        content_sha256=digest,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{sport}-{slate_date.isoformat()}-{digest[:8]}.jsonl.gz"
    with path.open("wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as gz:
        gz.write(_line(manifest.model_dump(mode="json")))
        gz.writelines(body)
    return path


def read_bundle(path: Path) -> tuple[BundleManifest, list[dict[str, Any]]]:
    """Read and verify a bundle; raises BundleIntegrityError before returning anything."""
    try:
        with gzip.open(path, "rb") as f:
            lines = f.read().splitlines(keepends=True)
        head = json.loads(lines[0]) if lines else None
    except (OSError, EOFError, ValueError) as exc:
        raise BundleIntegrityError(f"{path.name}: not a readable bundle ({exc})") from exc
    if not isinstance(head, dict):
        raise BundleIntegrityError(f"{path.name}: missing manifest line")
    if head.get("format_version") != FORMAT_VERSION:
        raise BundleIntegrityError(
            f"{path.name}: unknown format_version {head.get('format_version')!r}"
        )
    try:
        manifest = BundleManifest.model_validate(head)
    except ValidationError as exc:
        raise BundleIntegrityError(f"{path.name}: invalid manifest ({exc})") from exc

    body = lines[1:]
    if hashlib.sha256(b"".join(body)).hexdigest() != manifest.content_sha256:
        raise BundleIntegrityError(f"{path.name}: content_sha256 mismatch")
    records: list[dict[str, Any]] = [json.loads(line) for line in body]
    if dict(Counter(str(r.get("kind")) for r in records)) != manifest.counts:
        raise BundleIntegrityError(f"{path.name}: record counts do not match the manifest")
    return manifest, records
