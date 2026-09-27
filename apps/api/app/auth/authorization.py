from collections.abc import Callable

from fastapi import Depends, HTTPException, Path, status

from app.auth.models import AuthPrincipal, WorkspaceRole
from app.auth.providers import current_principal

principal_dependency = Depends(current_principal)


def require_workspace_access(
    workspace_id: str = Path(...),
    principal: AuthPrincipal = principal_dependency,
) -> AuthPrincipal:
    if principal.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "WORKSPACE_ACCESS_DENIED",
                "message": "The user is not a member of this workspace.",
            },
        )
    return principal


def require_roles(*allowed_roles: WorkspaceRole) -> Callable:
    workspace_access_dependency = Depends(require_workspace_access)

    def dependency(
        principal: AuthPrincipal = workspace_access_dependency,
    ) -> AuthPrincipal:
        if principal.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "ROLE_REQUIRED",
                    "message": "The user's role cannot perform this action.",
                },
            )
        return principal

    return dependency


def workspace_auth_dependency() -> Callable:
    def dependency(
        workspace_id: str = Path(...),
        principal: AuthPrincipal = principal_dependency,
    ) -> AuthPrincipal:
        if principal.workspace_id != workspace_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "WORKSPACE_ACCESS_DENIED",
                    "message": "The user is not a member of this workspace.",
                },
            )
        return principal

    return dependency
