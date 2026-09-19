"""Small, explicit audit helper that never receives credentials or image content."""

from sqlalchemy.orm import Session

from pyfog.models import AuditEvent

AUDIT_OUTCOMES = frozenset({"success", "failure"})
AUDIT_DECISIONS = frozenset({"allow", "deny"})
MAX_AUDIT_ACTION_LENGTH = 64
MAX_AUDIT_RESOURCE_ID_LENGTH = 100
MAX_AUDIT_RESOURCE_TYPE_LENGTH = 32


def record_audit(
    db: Session,
    *,
    action: str,
    resource_type: str,
    resource_id: str = "",
    outcome: str = "success",
    decision: str = "allow",
    reason: str = "",
    actor_user_id: int | None = None,
    actor_host_id: str | None = None,
    detail: str = "",
) -> AuditEvent:
    """Queue one bounded event; the caller commits it with the business transaction.

    Callers pass only stable internal identifiers and short, human-readable summaries. Tokens,
    passwords, request bodies, manifests and artifact contents deliberately have no parameter here.
    """

    if outcome not in AUDIT_OUTCOMES:
        msg = "El resultado de auditoría no es válido."
        raise ValueError(msg)
    if decision not in AUDIT_DECISIONS:
        msg = "La decisión de auditoría no es válida."
        raise ValueError(msg)
    if (
        not action
        or len(action) > MAX_AUDIT_ACTION_LENGTH
        or not resource_type
        or len(resource_type) > MAX_AUDIT_RESOURCE_TYPE_LENGTH
    ):
        msg = "La acción o el tipo de recurso de auditoría no es válido."
        raise ValueError(msg)
    if len(resource_id) > MAX_AUDIT_RESOURCE_ID_LENGTH:
        msg = "El identificador del recurso de auditoría es demasiado largo."
        raise ValueError(msg)
    event = AuditEvent(
        actor_user_id=actor_user_id,
        actor_host_id=actor_host_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        outcome=outcome,
        decision=decision,
        reason=(reason or detail)[:500],
        detail=detail[:500],
    )
    db.add(event)
    return event
