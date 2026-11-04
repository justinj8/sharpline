import time

from fastapi.testclient import TestClient

from sharpline.config import Settings
from sharpline_api.app import create_app


def test_health_degraded_fast_when_db_unreachable() -> None:
    # 10.255.255.1 is a non-routable address: connects hang until the timeout fires.
    s = Settings(
        database_url="postgresql://u:p@10.255.255.1:5432/x",
        env="test",
        db_connect_timeout_s=1,
    )
    client = TestClient(create_app(s))
    t0 = time.monotonic()
    r = client.get("/v1/health")
    assert r.status_code == 503
    assert r.json() == {"status": "degraded", "db": "unreachable", "version": "0.1.0"}
    assert time.monotonic() - t0 < 3
