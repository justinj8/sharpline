import pytest

from sharpline.ingest.normalize import normalize_name


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Nicolás Claxton", "nicolas claxton"),
        ("Jaren Jackson Jr.", "jaren jackson"),
        ("Gary Trent Jr", "gary trent"),
        ("Marvin Bagley III", "marvin bagley"),
        ("De'Aaron Fox", "deaaron fox"),
        ("  P.J.  Washington ", "pj washington"),
        ("Wendell Carter Jr.", "wendell carter"),
        ("Larry Nance Jr.", "larry nance"),
    ],
)
def test_normalize_name(raw: str, expected: str) -> None:
    assert normalize_name(raw) == expected


def test_suffix_only_removed_as_whole_token() -> None:
    assert normalize_name("Viiktor Sr") == "viiktor"
    assert normalize_name("Ivan Iverson") == "ivan iverson"
