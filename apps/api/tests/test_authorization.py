from app.main import create_app
from fastapi.testclient import TestClient


def test_workspace_member_can_access_own_workspace() -> None:
    client = TestClient(create_app())

    response = client.get(
        "/v1/workspaces/workspace-a/access-check",
        headers={"X-User-Id": "user-a", "X-Workspace-Id": "workspace-a", "X-Role": "analyst"},
    )

    assert response.status_code == 200
    assert response.json()["role"] == "analyst"


def test_cross_workspace_access_is_denied() -> None:
    client = TestClient(create_app())

    response = client.get(
        "/v1/workspaces/workspace-b/access-check",
        headers={"X-User-Id": "user-a", "X-Workspace-Id": "workspace-a", "X-Role": "analyst"},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "WORKSPACE_ACCESS_DENIED"


def test_viewer_cannot_use_admin_action() -> None:
    client = TestClient(create_app())

    response = client.post(
        "/v1/workspaces/workspace-a/admin-check",
        headers={"X-User-Id": "user-a", "X-Workspace-Id": "workspace-a", "X-Role": "viewer"},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "ROLE_REQUIRED"
