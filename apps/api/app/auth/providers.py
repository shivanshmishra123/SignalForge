from typing import Protocol

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPBearer

from app.auth.auth0 import Auth0Validator
from app.auth.models import AuthPrincipal, WorkspaceRole
from app.domains.async_utils import maybe_await
from app.settings import Settings, get_settings

settings_dependency = Depends(get_settings)
bearer_scheme = HTTPBearer(auto_error=False)


class AuthenticationProvider(Protocol):
    async def authenticate(
        self,
        request: Request,
        settings: Settings,
        user_id: str | None,
        workspace_id: str | None,
        role: str | None,
    ) -> AuthPrincipal:
        """Authenticate a request and return its tenant-scoped principal."""


class DevelopmentHeaderAuthentication:
    async def authenticate(
        self,
        request: Request,
        settings: Settings,
        user_id: str | None,
        workspace_id: str | None,
        role: str | None,
    ) -> AuthPrincipal:
        if not user_id or not workspace_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={
                    "code": "AUTHENTICATION_REQUIRED",
                    "message": "Development auth headers are required.",
                },
            )

        try:
            principal_role = WorkspaceRole(role or WorkspaceRole.VIEWER)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={
                    "code": "INVALID_ROLE",
                    "message": "The supplied development role is invalid.",
                },
            ) from exc

        return AuthPrincipal(
            user_id=user_id,
            workspace_id=workspace_id,
            role=principal_role,
            auth_source="development-header",
        )


class Auth0Authentication:
    def __init__(self, validator: Auth0Validator):
        self.validator = validator

    async def authenticate(
        self,
        request: Request,
        settings: Settings,
        user_id: str | None,
        workspace_id: str | None,
        role: str | None,
    ) -> AuthPrincipal:
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "AUTHENTICATION_REQUIRED", "message": "Bearer token is required."},
            )
        token = auth_header[7:]
        payload = self.validator.verify_token(token)
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "INVALID_TOKEN", "message": "Token missing sub claim."},
            )

        # Determine the workspace the user is trying to access
        # In a real app, this might come from a path param or header
        if not workspace_id:
            # We must fail if they need workspace context but didn't provide one
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "WORKSPACE_REQUIRED",
                    "message": "X-Workspace-Id header is required.",
                },
            )

        runtime = getattr(request.app.state, "runtime", None)
        if not runtime or not runtime.workspace:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Workspace repository not available.",
            )

        workspace_role = await maybe_await(runtime.workspace.get_role(user_id, workspace_id))
        if not workspace_role:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "WORKSPACE_ACCESS_DENIED",
                    "message": "User is not a member of this workspace.",
                },
            )

        return AuthPrincipal(
            user_id=user_id,
            workspace_id=workspace_id,
            role=workspace_role,
            auth_source="auth0",
        )


_auth0_validator: Auth0Validator | None = None


def get_authentication_provider(settings: Settings) -> AuthenticationProvider:
    global _auth0_validator
    if settings.auth_mode == "development":
        return DevelopmentHeaderAuthentication()
    if settings.auth_mode == "production":
        if _auth0_validator is None:
            _auth0_validator = Auth0Validator(settings)
        return Auth0Authentication(_auth0_validator)
    raise RuntimeError(f"Unsupported AUTH_MODE: {settings.auth_mode}")


async def current_principal(
    request: Request,
    settings: Settings = settings_dependency,
    x_user_id: str | None = Header(default=None),
    x_workspace_id: str | None = Header(default=None),
    x_role: str | None = Header(default=None),
) -> AuthPrincipal:
    provider = get_authentication_provider(settings)
    return await provider.authenticate(request, settings, x_user_id, x_workspace_id, x_role)
