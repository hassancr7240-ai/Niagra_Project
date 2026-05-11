from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from fastapi import HTTPException, status


class Role(str, Enum):
    SUPERVISOR = "Supervisor"
    TECHNICIAN = "Technician"
    MANAGER = "Manager"
    ENGINEER = "Engineer"


# Role → list of permitted actions
_PERMISSIONS: dict[str, set[str]] = {
    Role.SUPERVISOR: {
        "pm:read",
        "pm:approve",
        "history:read",
        "library:read",
        "machines:read",
        "download:read",
        "dashboard:read",
    },
    Role.TECHNICIAN: {
        "pm:read",
        "pm:generate",
        "pm:complete",
        "history:read",
        "library:read",
        "machines:read",
        "download:read",
        "dashboard:read",
    },
    Role.MANAGER: {
        "pm:read",
        "pm:generate",
        "pm:approve",
        "pm:complete",
        "history:read",
        "history:export",
        "library:read",
        "library:write",
        "machines:read",
        "machines:write",
        "download:read",
        "dashboard:read",
        "audit:read",
        "manual:upload",
        "hours:write",
    },
    Role.ENGINEER: {
        "pm:read",
        "pm:generate",
        "history:read",
        "library:read",
        "library:write",
        "machines:read",
        "machines:write",
        "download:read",
        "dashboard:read",
        "manual:upload",
        "manual:approve",
    },
}


@dataclass
class CurrentUser:
    user_id: str
    email: str
    name: str
    role: str
    ip_address: Optional[str] = None

    def can(self, permission: str) -> bool:
        perms = _PERMISSIONS.get(self.role, set())
        return permission in perms

    def require(self, permission: str) -> None:
        if not self.can(permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{self.role}' is not permitted to perform: {permission}",
            )


def normalize_role(raw_roles: list[str]) -> str:
    """Map Azure AD app role claims to internal roles."""
    priority = [Role.MANAGER, Role.ENGINEER, Role.SUPERVISOR, Role.TECHNICIAN]
    role_set = {r.lower() for r in raw_roles}
    for r in priority:
        if r.lower() in role_set:
            return r
    # Default
    return Role.TECHNICIAN


def require_roles(*roles: str):
    """Dependency factory: raise 403 if current user role is not in allowed list."""
    allowed = {r.lower() for r in roles}

    def _check(user: CurrentUser) -> CurrentUser:
        if user.role.lower() not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This endpoint requires one of: {', '.join(roles)}",
            )
        return user

    return _check
