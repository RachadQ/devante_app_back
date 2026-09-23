from typing import Any
from uuid import UUID

from app.database import get_database
from app.models import utcnow


async def write_audit(action: str, actor_id: UUID | None, resource: str, resource_id: Any = None,
                      changes: dict | None = None, metadata: dict | None = None) -> None:
    await get_database().audit_logs.insert_one({
        "action": action, "actor_id": actor_id, "resource": resource,
        "resource_id": str(resource_id) if resource_id is not None else None,
        "changes": changes or {}, "metadata": metadata or {}, "created_at": utcnow(),
    })

