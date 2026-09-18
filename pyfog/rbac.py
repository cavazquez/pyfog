"""Role-based access control for human administrative actions.

The agent API remains authenticated by host-scoped credentials.  This module
describes the permissions for the web/admin surface and deliberately fails
closed for unknown roles and permissions.
"""

from collections.abc import Iterable
from typing import Any

ROLES = ("admin", "operator", "auditor")
ROLE_LABELS = {
    "admin": "Administrador",
    "operator": "Operador",
    "auditor": "Auditor",
}

PERMISSIONS = frozenset(
    {
        "hosts.read",
        "hosts.create",
        "hosts.update",
        "inventory.read",
        "inventory.import",
        "inventory.token",
        "pairing.read",
        "pairing.manage",
        "images.read",
        "images.create",
        "images.update",
        "images.capture",
        "images.delete",
        "restore.execute",
        "clone.execute",
        "tasks.read",
        "tasks.cancel",
        "tasks.reconcile",
        "status.read",
        "audit.read",
        "users.manage",
        "backup.read",
        "backup.create",
        "backup.restore",
    }
)

ROLE_PERMISSIONS = {
    "admin": PERMISSIONS,
    "operator": frozenset(
        {
            "hosts.read",
            "hosts.create",
            "hosts.update",
            "inventory.read",
            "inventory.import",
            "inventory.token",
            "pairing.read",
            "pairing.manage",
            "images.read",
            "images.create",
            "images.update",
            "images.capture",
            "restore.execute",
            "clone.execute",
            "tasks.read",
            "tasks.cancel",
            "tasks.reconcile",
            "status.read",
            "backup.read",
            "backup.create",
        }
    ),
    "auditor": frozenset(
        {
            "hosts.read",
            "inventory.read",
            "pairing.read",
            "images.read",
            "tasks.read",
            "status.read",
            "audit.read",
            "backup.read",
        }
    ),
}

NAVIGATION_PERMISSIONS = {
    "hosts": "hosts.read",
    "pairing": "pairing.read",
    "images": "images.read",
    "tasks": "tasks.read",
    "status": "status.read",
    "audit": "audit.read",
    "users": "users.manage",
}


def role_of(user_or_role: Any) -> str | None:
    """Return a normalized role from a User-like object or a role string."""

    if user_or_role is None:
        return None
    value = getattr(user_or_role, "role", user_or_role)
    return value if isinstance(value, str) else None


def permissions_for(user_or_role: Any) -> frozenset[str]:
    """Return permissions, or an empty set for an unknown role."""

    role = role_of(user_or_role)
    return ROLE_PERMISSIONS.get(role, frozenset()) if role else frozenset()


def has_permission(user_or_role: Any, permission: str) -> bool:
    """Check one permission with deny-by-default semantics."""

    return permission in PERMISSIONS and permission in permissions_for(user_or_role)


def allowed_permissions(role: str) -> frozenset[str]:
    """Expose a stable copy for documentation, tests and administrative UI."""

    return permissions_for(role)


def is_role(value: object) -> bool:
    return isinstance(value, str) and value in ROLES


def missing_permissions(user_or_role: Any, permissions: Iterable[str]) -> tuple[str, ...]:
    """Return requested permissions not granted to the principal."""

    granted = permissions_for(user_or_role)
    return tuple(permission for permission in permissions if permission not in granted)
