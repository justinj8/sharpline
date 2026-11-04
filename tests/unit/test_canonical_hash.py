from sharpline.db.sources import canonical_hash


def test_hash_ignores_key_order() -> None:
    assert canonical_hash({"a": 1, "b": [1, 2]}) == canonical_hash({"b": [1, 2], "a": 1})


def test_hash_detects_value_change() -> None:
    assert canonical_hash({"a": 1}) != canonical_hash({"a": 2})


def test_hash_is_sha256_hex() -> None:
    digest = canonical_hash({"a": 1})
    assert len(digest) == 64
    int(digest, 16)
