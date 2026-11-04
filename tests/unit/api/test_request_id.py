from fastapi import FastAPI
from fastapi.testclient import TestClient

from sharpline_api.middleware import RequestIdMiddleware


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/x")
    def x() -> dict[str, str]:
        return {}

    return app


def test_request_id_echoed() -> None:
    c = TestClient(_app())
    assert c.get("/x", headers={"X-Request-ID": "abc"}).headers["X-Request-ID"] == "abc"


def test_request_id_generated_when_absent() -> None:
    c = TestClient(_app())
    rid = c.get("/x").headers["X-Request-ID"]
    assert len(rid) == 32
    int(rid, 16)
