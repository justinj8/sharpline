import sharpline
import sharpline_api
import sharpline_worker


def test_packages_import_and_version() -> None:
    assert sharpline.__version__ == "0.1.0"
    assert sharpline_api is not None
    assert sharpline_worker is not None


def test_directly_imported_requests_is_a_declared_dependency() -> None:
    """The CLI imports `requests` itself (nba_api errors), so it is declared and pinned, not
    left to arrive transitively through nba-api."""
    import tomllib
    from pathlib import Path

    pyproject = tomllib.loads((Path(__file__).parents[2] / "pyproject.toml").read_text())
    assert "requests==2.34.2" in pyproject["project"]["dependencies"]
