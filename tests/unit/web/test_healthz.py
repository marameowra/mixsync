from fastapi.testclient import TestClient

from mixsync.app import create_app


def test_healthz() -> None:
    assert TestClient(create_app()).get("/healthz").status_code == 200
