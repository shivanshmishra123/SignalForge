from app.main import create_app
from fastapi.testclient import TestClient


def test_health_reports_process_status() -> None:
    client = TestClient(create_app())

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_readiness_is_not_ready_until_dependencies_are_configured() -> None:
    client = TestClient(create_app())

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["dependencies"]["database"] == "not_configured"


def test_cors_preflight_allows_frontend_origin() -> None:
    client = TestClient(create_app())

    response = client.options(
        "/v1/workspaces/workspace-a/runs",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
