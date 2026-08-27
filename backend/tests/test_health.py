"""Liveness must not depend on anything.

If ``/health`` ever starts touching Postgres, a database blip becomes a restart loop
across every replica. This test is what stops that happening by accident.
"""

from fastapi.testclient import TestClient


def test_health_is_ok_without_any_dependency(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_returns_a_request_id(client: TestClient) -> None:
    response = client.get("/health")

    assert response.headers["X-Request-ID"]


def test_metrics_are_exposed(client: TestClient) -> None:
    response = client.get("/metrics")

    assert response.status_code == 200
    assert "kitaably_http_requests_total" in response.text
