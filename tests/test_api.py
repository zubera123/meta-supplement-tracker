from fastapi.testclient import TestClient

from app.main import app


def test_root_returns_app_information() -> None:
    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.json()["app"] == "meta-supplement-tracker"


def test_health_returns_runtime_information() -> None:
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "app_name": "meta-supplement-tracker",
        "environment": "development",
    }
