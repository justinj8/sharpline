"""Where slate bundles live: a local directory or a private S3-compatible bucket.

Bundle names embed a content hash (`<sport>-<date>-<sha8>.jsonl.gz`), so pushing a name
that already exists overwrites it with identical bytes. Credentials are only unwrapped at
the boto3 client construction site and never appear in a `repr`.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import boto3  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

if TYPE_CHECKING:
    from pydantic import SecretStr

    from sharpline.config import Settings

PREFIX = "bundles/"
SUFFIX = ".jsonl.gz"
DEFAULT_LOCAL_ROOT = Path("var/bundles")
_MISSING_CODES = frozenset({"NoSuchKey", "404", "NotFound"})


@runtime_checkable
class BundleStore(Protocol):
    def push(self, path: Path) -> str:
        """Store the bundle file and return its stored name (the file's basename)."""
        ...

    def pull(self, name: str, dest: Path) -> Path:
        """Write the named bundle into directory `dest` and return the file path."""
        ...

    def list(self) -> list[str]:
        """Stored bundle names, sorted."""
        ...


def _check_name(name: str) -> str:
    if "/" in name or "\\" in name or ".." in name or not name.endswith(SUFFIX):
        raise ValueError(f"invalid bundle name: {name!r}")
    return name


class LocalBundleStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def __repr__(self) -> str:
        return f"LocalBundleStore(root={str(self.root)!r})"

    def push(self, path: Path) -> str:
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / path.name
        if target.resolve() != path.resolve():
            shutil.copyfile(path, target)
        return path.name

    def pull(self, name: str, dest: Path) -> Path:
        _check_name(name)
        src = self.root / name
        if not src.is_file():
            raise FileNotFoundError(f"bundle {name!r} not found in {self.root}")
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / name
        if out.resolve() != src.resolve():  # pulling into the store's own directory
            shutil.copyfile(src, out)
        return out

    def list(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(
            p.name for p in self.root.iterdir() if p.is_file() and p.name.endswith(SUFFIX)
        )


class S3BundleStore:
    def __init__(
        self,
        bucket: str,
        endpoint_url: str,
        region: str,
        access_key_id: SecretStr,
        secret_access_key: SecretStr,
        client: Any = None,
    ) -> None:
        self.bucket = bucket
        self.endpoint_url = endpoint_url
        self.region = region
        if client is None:
            client = boto3.client(
                "s3",
                region_name=region,
                endpoint_url=endpoint_url,
                aws_access_key_id=access_key_id.get_secret_value(),
                aws_secret_access_key=secret_access_key.get_secret_value(),
            )
        self._client = client

    def __repr__(self) -> str:
        return f"S3BundleStore(bucket={self.bucket!r}, endpoint_url={self.endpoint_url!r})"

    def push(self, path: Path) -> str:
        self._client.put_object(
            Bucket=self.bucket, Key=f"{PREFIX}{path.name}", Body=path.read_bytes()
        )
        return path.name

    def pull(self, name: str, dest: Path) -> Path:
        _check_name(name)
        try:
            resp = self._client.get_object(Bucket=self.bucket, Key=f"{PREFIX}{name}")
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in _MISSING_CODES:
                raise FileNotFoundError(f"bundle {name!r} not found in bucket") from exc
            raise
        data: bytes = resp["Body"].read()
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / name
        out.write_bytes(data)
        return out

    def list(self) -> list[str]:
        names: list[str] = []
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": self.bucket, "Prefix": PREFIX}
            if token:
                kwargs["ContinuationToken"] = token
            resp = self._client.list_objects_v2(**kwargs)
            for obj in resp.get("Contents", []):
                name = str(obj["Key"]).removeprefix(PREFIX)
                if name.endswith(SUFFIX) and "/" not in name:
                    names.append(name)
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
            if not token:
                break
        return sorted(names)


def store_from_settings(settings: Settings) -> BundleStore:
    """S3 when `BUNDLE_BUCKET` is set (all its companions required), else `var/bundles`."""
    if not settings.bundle_bucket:
        return LocalBundleStore(DEFAULT_LOCAL_ROOT)
    for env_name, value in (
        ("BUNDLE_ENDPOINT_URL", settings.bundle_endpoint_url),
        ("BUNDLE_ACCESS_KEY_ID", settings.bundle_access_key_id),
        ("BUNDLE_SECRET_ACCESS_KEY", settings.bundle_secret_access_key),
    ):
        if not value:
            raise ValueError(f"BUNDLE_BUCKET is set but {env_name} is missing")
    assert settings.bundle_endpoint_url and settings.bundle_access_key_id
    assert settings.bundle_secret_access_key
    return S3BundleStore(
        bucket=settings.bundle_bucket,
        endpoint_url=settings.bundle_endpoint_url,
        region=settings.bundle_region,
        access_key_id=settings.bundle_access_key_id,
        secret_access_key=settings.bundle_secret_access_key,
    )
