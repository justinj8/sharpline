"""Bundle stores: local directory and private S3-compatible bucket."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.stub import Stubber
from pydantic import SecretStr

from sharpline.bundles.store import (
    BundleStore,
    LocalBundleStore,
    S3BundleStore,
    store_from_settings,
)
from sharpline.config import Settings

NAME = "nba-2026-11-04-ab12cd34.jsonl.gz"
BUCKET = "sharpline-bundles"
ENDPOINT = "https://example.invalid"


def _bundle(tmp_path: Path, name: str = NAME, data: bytes = b"bundle-bytes") -> Path:
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    path = src / name
    path.write_bytes(data)
    return path


def _client() -> Any:
    return boto3.client(
        "s3",
        region_name="auto",
        endpoint_url=ENDPOINT,
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


def _s3(client: Any) -> S3BundleStore:
    return S3BundleStore(
        bucket=BUCKET,
        endpoint_url=ENDPOINT,
        region="auto",
        access_key_id=SecretStr("test"),
        secret_access_key=SecretStr("test"),
        client=client,
    )


# --- local ---------------------------------------------------------------------------


def test_local_round_trip(tmp_path: Path) -> None:
    store = LocalBundleStore(tmp_path / "root" / "bundles")
    assert store.list() == []
    assert store.push(_bundle(tmp_path)) == NAME
    assert store.list() == [NAME]
    out = store.pull(NAME, tmp_path / "out")
    assert out == tmp_path / "out" / NAME
    assert out.read_bytes() == b"bundle-bytes"


def test_local_list_sorted_and_ignores_other_files(tmp_path: Path) -> None:
    store = LocalBundleStore(tmp_path / "root")
    store.push(_bundle(tmp_path, "nba-2026-11-05-bbbbbbbb.jsonl.gz"))
    store.push(_bundle(tmp_path, "nba-2026-11-04-aaaaaaaa.jsonl.gz"))
    (tmp_path / "root" / "notes.txt").write_text("x")
    assert store.list() == [
        "nba-2026-11-04-aaaaaaaa.jsonl.gz",
        "nba-2026-11-05-bbbbbbbb.jsonl.gz",
    ]


def test_local_push_same_name_is_idempotent(tmp_path: Path) -> None:
    store = LocalBundleStore(tmp_path / "root")
    path = _bundle(tmp_path)
    store.push(path)
    store.push(path)
    assert store.list() == [NAME]


def test_local_pull_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        LocalBundleStore(tmp_path / "root").pull(NAME, tmp_path / "out")


@pytest.mark.parametrize(
    "bad", ["../x.jsonl.gz", "a/b.jsonl.gz", "..", "bundle.txt", "x..y/../z.jsonl.gz", ""]
)
def test_pull_rejects_unsafe_names(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError):
        LocalBundleStore(tmp_path / "root").pull(bad, tmp_path / "out")
    stub_client = _client()
    with Stubber(stub_client) as stubber, pytest.raises(ValueError):
        _s3(stub_client).pull(bad, tmp_path / "out")
    stubber.assert_no_pending_responses()


# --- S3 ------------------------------------------------------------------------------


def test_s3_push_puts_under_prefix(tmp_path: Path) -> None:
    client = _client()
    with Stubber(client) as stubber:
        stubber.add_response(
            "put_object",
            {},
            {"Bucket": BUCKET, "Key": f"bundles/{NAME}", "Body": b"bundle-bytes"},
        )
        assert _s3(client).push(_bundle(tmp_path)) == NAME
        stubber.assert_no_pending_responses()


def test_s3_pull_writes_dest(tmp_path: Path) -> None:
    client = _client()
    with Stubber(client) as stubber:
        stubber.add_response(
            "get_object",
            {"Body": _streaming(b"bundle-bytes")},
            {"Bucket": BUCKET, "Key": f"bundles/{NAME}"},
        )
        out = _s3(client).pull(NAME, tmp_path / "out")
    assert out == tmp_path / "out" / NAME
    assert out.read_bytes() == b"bundle-bytes"


def test_s3_pull_missing_raises_file_not_found(tmp_path: Path) -> None:
    client = _client()
    with Stubber(client) as stubber:
        stubber.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404)
        with pytest.raises(FileNotFoundError):
            _s3(client).pull(NAME, tmp_path / "out")
    assert not (tmp_path / "out" / NAME).exists()


def test_s3_pull_other_errors_propagate(tmp_path: Path) -> None:
    from botocore.exceptions import ClientError

    client = _client()
    with Stubber(client) as stubber:
        stubber.add_client_error(
            "get_object", service_error_code="AccessDenied", http_status_code=403
        )
        with pytest.raises(ClientError):
            _s3(client).pull(NAME, tmp_path / "out")


def test_s3_list_uses_prefix_and_strips_it() -> None:
    client = _client()
    with Stubber(client) as stubber:
        stubber.add_response(
            "list_objects_v2",
            {
                "Contents": [
                    {"Key": "bundles/"},
                    {"Key": "bundles/nba-2026-11-05-bbbbbbbb.jsonl.gz"},
                    {"Key": "bundles/nba-2026-11-04-aaaaaaaa.jsonl.gz"},
                ]
            },
            {"Bucket": BUCKET, "Prefix": "bundles/"},
        )
        assert _s3(client).list() == [
            "nba-2026-11-04-aaaaaaaa.jsonl.gz",
            "nba-2026-11-05-bbbbbbbb.jsonl.gz",
        ]


def test_s3_list_paginates() -> None:
    client = _client()
    with Stubber(client) as stubber:
        stubber.add_response(
            "list_objects_v2",
            {
                "IsTruncated": True,
                "NextContinuationToken": "tok",
                "Contents": [{"Key": "bundles/nba-2026-11-05-bbbbbbbb.jsonl.gz"}],
            },
            {"Bucket": BUCKET, "Prefix": "bundles/"},
        )
        stubber.add_response(
            "list_objects_v2",
            {
                "IsTruncated": False,
                "Contents": [{"Key": "bundles/nba-2026-11-04-aaaaaaaa.jsonl.gz"}],
            },
            {"Bucket": BUCKET, "Prefix": "bundles/", "ContinuationToken": "tok"},
        )
        assert _s3(client).list() == [
            "nba-2026-11-04-aaaaaaaa.jsonl.gz",
            "nba-2026-11-05-bbbbbbbb.jsonl.gz",
        ]
        stubber.assert_no_pending_responses()


def test_s3_list_empty_bucket() -> None:
    client = _client()
    with Stubber(client) as stubber:
        stubber.add_response("list_objects_v2", {}, {"Bucket": BUCKET, "Prefix": "bundles/"})
        assert _s3(client).list() == []


def test_s3_builds_client_without_leaking_secrets() -> None:
    store = S3BundleStore(
        bucket=BUCKET,
        endpoint_url=ENDPOINT,
        region="auto",
        access_key_id=SecretStr("AKIA-visible-test"),
        secret_access_key=SecretStr("shh-secret-test"),
    )
    assert "shh-secret-test" not in repr(store)
    assert "AKIA-visible-test" not in repr(store)


# --- store_from_settings ---------------------------------------------------------------


def _settings(**kw: Any) -> Settings:
    return Settings(_env_file=None, **kw)


def test_settings_without_bucket_is_local() -> None:
    store = store_from_settings(_settings())
    assert isinstance(store, LocalBundleStore)
    assert store.root == Path("var/bundles")


def test_settings_with_bucket_is_s3() -> None:
    store = store_from_settings(
        _settings(
            bundle_bucket=BUCKET,
            bundle_endpoint_url=ENDPOINT,
            bundle_access_key_id="k",
            bundle_secret_access_key="s",
        )
    )
    assert isinstance(store, S3BundleStore)
    assert isinstance(store, BundleStore)


@pytest.mark.parametrize(
    ("missing", "env_name"),
    [
        ("bundle_endpoint_url", "BUNDLE_ENDPOINT_URL"),
        ("bundle_access_key_id", "BUNDLE_ACCESS_KEY_ID"),
        ("bundle_secret_access_key", "BUNDLE_SECRET_ACCESS_KEY"),
    ],
)
def test_settings_bucket_with_missing_piece_raises(missing: str, env_name: str) -> None:
    kw: dict[str, Any] = {
        "bundle_bucket": BUCKET,
        "bundle_endpoint_url": ENDPOINT,
        "bundle_access_key_id": "k",
        "bundle_secret_access_key": "s",
    }
    del kw[missing]
    with pytest.raises(ValueError, match=env_name):
        store_from_settings(_settings(**kw))


def _streaming(data: bytes) -> Any:
    from botocore.response import StreamingBody

    return StreamingBody(io.BytesIO(data), len(data))
