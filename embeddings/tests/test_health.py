"""Liveness must not depend on the model.

This is the distinction that keeps a slow model load from becoming a restart loop:
/health answers "the process is up" and says nothing about readiness to serve.
"""

from fastapi.testclient import TestClient

from app.main import app


def test_health_does_not_require_the_model() -> None:
    # No lifespan: the model is deliberately not loaded here.
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_is_503_until_the_model_is_loaded() -> None:
    client = TestClient(app)

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "loading"
