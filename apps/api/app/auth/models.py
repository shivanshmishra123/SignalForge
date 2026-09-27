from dataclasses import dataclass
from enum import StrEnum


class WorkspaceRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    ANALYST = "analyst"
    VIEWER = "viewer"


@dataclass(frozen=True)
class AuthPrincipal:
    user_id: str
    workspace_id: str
    role: WorkspaceRole
    auth_source: str
