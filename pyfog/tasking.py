"""State and lease management for durable image tasks."""

import secrets
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pyfog.config import Settings
from pyfog.models import Image, Task, TaskAttempt, TaskEvent, now
from pyfog.security import digest

TASK_STATES = (
    "draft",
    "approved",
    "assigned",
    "running",
    "verifying",
    "succeeded",
    "failed",
    "cancelled",
    "intervention_required",
)
ACTIVE_TASK_STATES = frozenset(
    {"approved", "assigned", "running", "verifying", "intervention_required"}
)
TERMINAL_TASK_STATES = frozenset({"succeeded", "failed", "cancelled"})
CLAIM_CAPABILITIES = frozenset({"gpt", "partclone.ext4", "partclone.fat"})
TASK_PHASES = frozenset(
    {
        "queued",
        "assigned",
        "running",
        "inspecting",
        "capturing",
        "uploading",
        "downloading",
        "restoring",
        "personalizing",
        "finalizing",
        "cancelling",
        "verifying",
        "completed",
        "failed",
    }
)
PHASE_ORDER = {
    "queued": 0,
    "assigned": 1,
    "running": 2,
    "inspecting": 3,
    "capturing": 4,
    "uploading": 5,
    "downloading": 5,
    "restoring": 6,
    "personalizing": 7,
    "finalizing": 8,
    "cancelling": 9,
    "verifying": 10,
    "completed": 11,
    "failed": 12,
}
TASK_LABELS = {
    "draft": "Borrador",
    "approved": "En cola",
    "assigned": "Agente asignado",
    "running": "En curso",
    "verifying": "Verificando",
    "succeeded": "Completada",
    "failed": "Fallida",
    "cancelled": "Cancelada",
    "intervention_required": "Requiere intervención",
}
TASK_OPERATION_LABELS = {"capture": "Captura", "restore": "Restauración", "clone": "Clonación"}

ALLOWED_TRANSITIONS = {
    "draft": frozenset({"approved", "cancelled"}),
    "approved": frozenset({"assigned", "cancelled", "intervention_required"}),
    "assigned": frozenset({"running", "failed", "cancelled", "intervention_required"}),
    "running": frozenset({"verifying", "failed", "cancelled", "intervention_required"}),
    "verifying": frozenset({"succeeded", "failed", "cancelled", "intervention_required"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
    "intervention_required": frozenset({"approved", "cancelled"}),
}

REQUIRED_CAPABILITIES = {
    "capture": frozenset({"gpt", "partclone.ext4", "partclone.fat"}),
    "restore": frozenset({"gpt", "partclone.ext4", "partclone.fat", "restore"}),
    "clone": frozenset(
        {"gpt", "partclone.ext4", "partclone.fat", "restore", "clone", "identity"}
    ),
}


def required_capabilities(operation: str) -> frozenset[str]:
    try:
        return REQUIRED_CAPABILITIES[operation]
    except KeyError:
        raise TaskError("La operación de la tarea no es válida.") from None


class TaskError(ValueError):
    """A task cannot accept the requested operation."""


@dataclass(frozen=True)
class ClaimedTask:
    task: Task
    attempt: TaskAttempt
    token: str


def transition_task(
    db: Session,
    task: Task,
    status: str,
    *,
    phase: str | None = None,
    message: str | None = None,
    failure_reason: str | None = None,
) -> None:
    if status not in TASK_STATES:
        raise TaskError("El estado de la tarea no es válido.")
    if status != task.status and status not in ALLOWED_TRANSITIONS.get(task.status, frozenset()):
        raise TaskError(f"No se puede pasar una tarea de {task.status} a {status}.")
    current = now()
    task.status = status
    task.updated_at = current
    if phase is not None:
        if phase not in TASK_PHASES:
            raise TaskError("La fase de la tarea no es válida.")
        task.phase = phase
    if message is not None:
        task.message = message[:500]
    if failure_reason is not None:
        task.failure_reason = failure_reason[:500]
    if status == "assigned":
        task.assigned_at = task.assigned_at or current
    if status == "running":
        task.assigned_at = task.assigned_at or current
        task.started_at = task.started_at or current
    if status == "verifying":
        task.started_at = task.started_at or current
    if status in TERMINAL_TASK_STATES:
        task.completed_at = task.completed_at or current
        task.reservation_key = None
        task.transfer_slot = None
    db.flush()


def add_event(
    db: Session,
    task: Task,
    *,
    event_type: str,
    attempt: TaskAttempt | None = None,
    sequence: int | None = None,
    phase: str | None = None,
    bytes_processed: int = 0,
    total_bytes: int | None = None,
    message: str = "",
) -> TaskEvent:
    event = TaskEvent(
        task_id=task.id,
        attempt_id=attempt.id if attempt else None,
        sequence=sequence,
        event_type=event_type,
        phase=phase or task.phase,
        bytes_processed=bytes_processed,
        total_bytes=total_bytes,
        message=message[:500],
    )
    db.add(event)
    return event


def expire_stale_tasks(db: Session) -> int:
    """Mark expired leases for intervention without making a new writer eligible."""

    current = now()
    stale = db.scalars(
        select(TaskAttempt)
        .join(Task, Task.id == TaskAttempt.task_id)
        .where(
            Task.status.in_({"assigned", "running", "verifying"}),
            TaskAttempt.lease_expires_at <= current,
        )
    ).all()
    for attempt in stale:
        task = db.get(Task, attempt.task_id)
        if task is None or task.status in TERMINAL_TASK_STATES:
            continue
        reason = "La concesión del agente venció; verificá que el agente anterior esté detenido."
        attempt.finished_at = current
        attempt.failure_reason = reason
        attempt.phase = "failed"
        transition_task(
            db,
            task,
            "intervention_required",
            phase="failed",
            message=reason,
            failure_reason=reason,
        )
        # A different host may use the single transfer slot, while this host remains reserved.
        task.transfer_slot = None
        image = db.get(Image, task.image_id)
        if image is not None and image.status == "capturing":
            image.status = "failed"
            image.failure_reason = reason
        add_event(
            db,
            task,
            event_type="lease_expired",
            attempt=attempt,
            sequence=attempt.last_sequence + 1,
            phase="failed",
            bytes_processed=attempt.bytes_processed,
            total_bytes=attempt.total_bytes,
            message=reason,
        )
        attempt.last_sequence += 1
    db.flush()
    return len(stale)


def request_task_cancellation(db: Session, task: Task, *, reason: str) -> bool:
    """Request cooperative cancellation, or cancel a task that has not been claimed yet."""

    if task.status in TERMINAL_TASK_STATES:
        return False
    if task.status == "intervention_required":
        raise TaskError("La tarea requiere reconciliación antes de cancelarse.")
    current = now()
    if task.cancel_requested_at is not None:
        return False
    task.cancel_requested_at = current
    task.message = reason[:500]
    image = db.get(Image, task.image_id)
    if task.operation == "capture" and image is not None and image.status == "capturing":
        image.status = "failed"
        image.failure_reason = "La captura fue cancelada y no se publicó ningún artefacto."
    if task.status == "approved":
        transition_task(db, task, "cancelled", phase=task.phase, message=reason)
        task.cancel_acknowledged_at = current
        add_event(db, task, event_type="cancelled", phase=task.phase, message=reason)
        return True
    transition_task(db, task, task.status, phase="cancelling", message=reason)
    add_event(db, task, event_type="cancellation_requested", phase="cancelling", message=reason)
    db.flush()
    return True


def acknowledge_task_cancellation(
    db: Session, task: Task, attempt: TaskAttempt, *, reason: str
) -> None:
    """Commit an agent's cooperative stop and retain the target reservation history."""

    if task.status == "cancelled":
        return
    if task.cancel_requested_at is None:
        raise TaskError("La tarea no tiene una cancelación solicitada.")
    if task.operation in {"restore", "clone"} and attempt.started_at is not None:
        reason = f"{reason[:420]} El destino puede haber quedado incompleto; requiere diagnóstico."
    current = now()
    attempt.finished_at = current
    attempt.phase = "failed"
    attempt.failure_reason = reason[:500]
    task.cancel_acknowledged_at = current
    image = db.get(Image, task.image_id)
    if task.operation == "capture" and image is not None and image.status == "capturing":
        image.status = "failed"
        image.failure_reason = "La captura fue cancelada y no se publicó ningún artefacto."
    transition_task(
        db,
        task,
        "cancelled",
        phase="failed",
        message=reason,
        failure_reason=reason if task.operation in {"restore", "clone"} else None,
    )
    sequence = attempt.last_sequence + 1
    attempt.last_sequence = sequence
    add_event(
        db,
        task,
        event_type="cancelled",
        attempt=attempt,
        sequence=sequence,
        phase="failed",
        bytes_processed=attempt.bytes_processed,
        total_bytes=attempt.total_bytes,
        message=reason,
    )
    db.flush()


def reconcile_task(db: Session, task: Task) -> None:
    """Make an intervention-required task explicitly eligible for a new attempt."""

    if task.status != "intervention_required":
        raise TaskError("Sólo se pueden reconciliar tareas que requieren intervención.")
    current = now()
    task.cancel_requested_at = None
    task.cancel_acknowledged_at = None
    task.failure_reason = ""
    task.transfer_slot = None
    task.assigned_at = None
    task.started_at = None
    task.completed_at = None
    task.bytes_processed = 0
    transition_task(
        db,
        task,
        "approved",
        phase="queued",
        message="Tarea reconciliada; esperando un intento nuevo y explícito.",
    )
    add_event(
        db,
        task,
        event_type="reconciled",
        phase="queued",
        message="El operador confirmó que el agente anterior está detenido.",
    )
    task.updated_at = current
    db.flush()


def claim_task(
    db: Session,
    host_id: str,
    session_id: str,
    capabilities: set[str],
    settings: Settings,
) -> ClaimedTask | None:
    """Atomically assign the oldest compatible task for one host."""

    expired = expire_stale_tasks(db)
    if expired:
        # Expiry is a durable safety decision. Do not lose it when the claim finds no work or
        # another task currently owns the single transfer slot.
        db.commit()
    existing_session = db.scalar(
        select(TaskAttempt).where(TaskAttempt.agent_session_id == session_id)
    )
    if existing_session is not None:
        raise TaskError("La sesión de arranque ya fue utilizada.")
    if db.scalar(select(Task.id).where(Task.transfer_slot == 1)) is not None:
        db.rollback()
        return None
    task = db.scalar(
        select(Task)
        .where(Task.host_id == host_id, Task.status == "approved")
        .order_by(Task.created_at.asc())
        .limit(1)
        .with_for_update()
    )
    if task is None:
        db.rollback()
        return None
    required = required_capabilities(task.operation)
    if not required.issubset(capabilities):
        db.rollback()
        raise TaskError("El agente no anuncia todas las capacidades requeridas para la operación.")
    attempt_number = (
        db.scalar(
            select(TaskAttempt.attempt_number)
            .where(TaskAttempt.task_id == task.id)
            .order_by(TaskAttempt.attempt_number.desc())
            .limit(1)
        )
        or 0
    ) + 1
    token = secrets.token_urlsafe(32)
    current = now()
    attempt = TaskAttempt(
        task_id=task.id,
        attempt_number=attempt_number,
        agent_session_id=session_id,
        capability_hash=digest(token),
        lease_expires_at=current + timedelta(seconds=settings.task_lease_seconds),
        last_heartbeat_at=current,
        last_sequence=0,
        phase="assigned",
        bytes_processed=0,
        total_bytes=task.total_bytes,
    )
    task.transfer_slot = 1
    transition_task(
        db,
        task,
        "assigned",
        phase="assigned",
        message="Agente asignado; esperando el análisis del disco.",
    )
    db.add(attempt)
    db.flush()
    add_event(
        db,
        task,
        event_type="assigned",
        attempt=attempt,
        sequence=0,
        phase="assigned",
        total_bytes=task.total_bytes,
        message=task.message,
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    db.refresh(task)
    db.refresh(attempt)
    return ClaimedTask(task=task, attempt=attempt, token=token)


def touch_attempt(
    db: Session,
    task: Task,
    attempt: TaskAttempt,
    settings: Settings,
    *,
    phase: str,
    bytes_processed: int,
    total_bytes: int | None,
    message: str,
) -> None:
    if phase not in TASK_PHASES or phase in {"queued", "completed", "failed"}:
        raise TaskError("La fase enviada por el agente no es válida.")
    if PHASE_ORDER[phase] < PHASE_ORDER.get(attempt.phase, 0):
        raise TaskError("La fase de la tarea no puede retroceder.")
    if bytes_processed < attempt.bytes_processed:
        raise TaskError("El progreso de la tarea no puede retroceder.")
    if (
        total_bytes is not None
        and attempt.total_bytes is not None
        and total_bytes < attempt.total_bytes
    ):
        raise TaskError("El tamaño total de la tarea no puede retroceder.")
    if total_bytes is not None and bytes_processed > total_bytes:
        raise TaskError("El progreso supera el tamaño total declarado.")
    current = now()
    attempt.last_heartbeat_at = current
    attempt.lease_expires_at = current + timedelta(seconds=settings.task_lease_seconds)
    attempt.phase = phase
    attempt.bytes_processed = bytes_processed
    if total_bytes is not None:
        attempt.total_bytes = total_bytes
    if task.cancel_requested_at is not None:
        task.phase = "cancelling"
        task.message = "Cancelación solicitada; esperando el reconocimiento del agente."
        task.bytes_processed = bytes_processed
        task.total_bytes = total_bytes if total_bytes is not None else task.total_bytes
        task.updated_at = current
    elif task.status == "assigned":
        transition_task(db, task, "running", phase=phase, message=message)
    else:
        task.phase = phase
        task.message = message[:500]
        task.bytes_processed = bytes_processed
        task.total_bytes = total_bytes if total_bytes is not None else task.total_bytes
        task.updated_at = current
    attempt.started_at = attempt.started_at or current
    db.flush()


def record_progress(
    db: Session,
    task: Task,
    attempt: TaskAttempt,
    settings: Settings,
    *,
    sequence: int,
    phase: str,
    bytes_processed: int,
    total_bytes: int | None,
    message: str,
) -> bool:
    """Record a monotonic agent event; duplicate events are safe to replay."""

    if phase not in TASK_PHASES or phase in {"queued", "completed", "failed"}:
        raise TaskError("La fase enviada por el agente no es válida.")
    previous = db.scalar(
        select(TaskEvent).where(TaskEvent.attempt_id == attempt.id, TaskEvent.sequence == sequence)
    )
    if previous is not None:
        same = (
            previous.phase == phase
            and previous.bytes_processed == bytes_processed
            and previous.total_bytes == total_bytes
            and previous.message == message
        )
        if not same:
            raise TaskError("El número de secuencia ya fue usado con otro progreso.")
        return False
    if sequence <= attempt.last_sequence:
        return False
    touch_attempt(
        db,
        task,
        attempt,
        settings,
        phase=phase,
        bytes_processed=bytes_processed,
        total_bytes=total_bytes,
        message=message,
    )
    if phase == "verifying" and task.status == "running":
        transition_task(db, task, "verifying", phase=phase, message=message)
    attempt.last_sequence = sequence
    add_event(
        db,
        task,
        event_type="progress",
        attempt=attempt,
        sequence=sequence,
        phase=phase,
        bytes_processed=bytes_processed,
        total_bytes=total_bytes,
        message=message,
    )
    db.flush()
    return True
