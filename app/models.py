from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, EmailStr, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UserCreate(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=200)
    is_active: bool = True
    is_super_admin: bool = False
    role_ids: list[UUID] = Field(default_factory=list)


class UserUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    is_active: bool | None = None
    is_super_admin: bool | None = None
    role_ids: list[UUID] | None = None


class RoleCreate(BaseModel):
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    name: str = Field(min_length=1, max_length=100)
    permissions: list[str] = Field(default_factory=list)


class RoleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    permissions: list[str] | None = None
    is_active: bool | None = None


def user_document(data: UserCreate, actor_id: UUID | None = None) -> dict[str, Any]:
    now = utcnow()
    return {
        "_id": uuid4(),
        "email": str(data.email).lower(),
        "full_name": data.full_name.strip(),
        "is_active": data.is_active,
        "is_super_admin": data.is_super_admin,
        "role_ids": data.role_ids,
        "auth_provider": "microsoft",
        "created_at": now,
        "updated_at": now,
        "created_by": actor_id,
        "deleted_at": None,
    }


def serialize(document: dict[str, Any] | None) -> dict[str, Any] | None:
    if document is None:
        return None
    result = dict(document)
    result["id"] = str(result.pop("_id"))
    for key, value in list(result.items()):
        if isinstance(value, UUID):
            result[key] = str(value)
        elif isinstance(value, list):
            result[key] = [str(item) if isinstance(item, UUID) else item for item in value]
    return result

