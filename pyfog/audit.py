"""Small, explicit audit helper that never receives credentials or image content."""

from sqlalchemy.orm import Session

from pyfog.models import AuditEvent

AUDIT_OUTCOMES = frozenset({"success", "failure"})


def record_audit(
    db: Session,
    *,
    action: str,
    resource_type: str,
    resource_id: str = "",
    outcome: str = "success",
    actor_user_id: int | None = None,
    actor_host_id: str | None = None,
    detail: str = "",
) -> AuditEvent:
    """Queue one bounded event; the caller commits it with the business transaction.

    Callers pass only stable internal identifiers and short, human-readable summaries. Tokens,
    passwords, request bodies, manifests and artifact contents deliberately have no parameter here.
    """

    if outcome not in AUDIT_OUTCOMES:
        raise ValueError("El resultado de auditoría no es válido.")
    if not action or len(action) > 64 or not resource_type or len(resource_type) > 32:
        raise ValueError("La acción o el tipo de recurso de auditoría no es válido.")
    if len(resource_id) > 100:
        raise ValueError("El identificador del recurso de auditoría es demasiado largo.")
    event = AuditEvent(
        actor_user_id=actor_user_id,
        actor_host_id=actor_host_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        outcome=outcome,
        detail=detail[:500],
    )
    db.add(event)
    return event

