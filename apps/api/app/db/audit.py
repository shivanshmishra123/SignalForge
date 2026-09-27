import json
from dataclasses import dataclass
from typing import Any

from app.auth.models import AuthPrincipal


@dataclass(frozen=True)
class AuditRecord:
    workspace_id: str
    actor_user_id: str
    action: str
    entity_type: str
    entity_id: str
    metadata_json: str


def build_audit_record(
    principal: AuthPrincipal,
    action: str,
    entity_type: str,
    entity_id: str,
    metadata: dict[str, Any] | None = None,
) -> AuditRecord:
    return AuditRecord(
        workspace_id=principal.workspace_id,
        actor_user_id=principal.user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        metadata_json=json.dumps(metadata or {}, sort_keys=True),
    )
