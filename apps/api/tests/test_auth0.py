import pytest
from app.auth.models import WorkspaceRole
from app.auth.providers import Auth0Authentication
from app.settings import Settings
from fastapi import HTTPException


class MockAuth0Validator:
    def __init__(self, mock_payload):
        self.mock_payload = mock_payload

    def verify_token(self, token: str) -> dict:
        if token == "bad":
            raise HTTPException(
                status_code=401,
                detail={"code": "INVALID_TOKEN", "message": "The token is invalid."},
            )
        return self.mock_payload


class MockWorkspaceRepository:
    def __init__(self, role):
        self.role = role

    async def get_role(self, user_id: str, workspace_id: str):
        if user_id == "good-user" and workspace_id == "workspace-a":
            return self.role
        return None


class MockRuntime:
    def __init__(self, role):
        self.workspace = MockWorkspaceRepository(role)


class MockApp:
    def __init__(self, role):
        self.state = type("State", (), {"runtime": MockRuntime(role)})


class MockRequest:
    def __init__(self, headers, app_role=WorkspaceRole.ADMIN):
        self.headers = headers
        self.app = MockApp(app_role)


@pytest.mark.anyio
async def test_auth0_authentication_success():
    validator = MockAuth0Validator({"sub": "good-user"})
    auth = Auth0Authentication(validator)

    request = MockRequest({"Authorization": "Bearer good-token"})
    settings = Settings(auth_mode="production", auth0_domain="test", auth0_audience="test")

    principal = await auth.authenticate(request, settings, None, "workspace-a", None)
    assert principal.user_id == "good-user"
    assert principal.workspace_id == "workspace-a"
    assert principal.role == WorkspaceRole.ADMIN
    assert principal.auth_source == "auth0"


@pytest.mark.anyio
async def test_auth0_authentication_missing_bearer():
    validator = MockAuth0Validator({"sub": "good-user"})
    auth = Auth0Authentication(validator)

    request = MockRequest({})
    settings = Settings(auth_mode="production", auth0_domain="test", auth0_audience="test")

    with pytest.raises(HTTPException) as exc:
        await auth.authenticate(request, settings, None, "workspace-a", None)

    assert exc.value.status_code == 401
    assert exc.value.detail["code"] == "AUTHENTICATION_REQUIRED"


@pytest.mark.anyio
async def test_auth0_authentication_invalid_workspace():
    validator = MockAuth0Validator({"sub": "bad-user"})
    auth = Auth0Authentication(validator)

    request = MockRequest({"Authorization": "Bearer good-token"})
    settings = Settings(auth_mode="production", auth0_domain="test", auth0_audience="test")

    with pytest.raises(HTTPException) as exc:
        await auth.authenticate(request, settings, None, "workspace-a", None)

    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "WORKSPACE_ACCESS_DENIED"
