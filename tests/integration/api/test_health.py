import pytest
from fastapi.testclient import TestClient

from sharpline.config import Settings
from sharpline_api.app import create_app

pytestmark = pytest.mark.integration


def test_health_ok(pg_url: str) -> None:
    client = TestClient(create_app(Settings(database_url=pg_url, env="test")))
    r = client.get("/v1/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "db": "ok", "version": "0.1.0"}
    assert len(r.headers["X-Request-ID"]) == 32
