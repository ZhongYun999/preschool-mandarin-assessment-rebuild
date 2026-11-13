from fastapi.testclient import TestClient

from app.main import app


def test_health_on_started_application():
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
