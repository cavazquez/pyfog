import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from pyfog.config import Settings
from pyfog.database import Base, make_engine
from pyfog.models import Host, Image, InventoryReport, Task, TaskAttempt, TaskEvent, User, now
from pyfog.tasking import (
    REQUIRED_CAPABILITIES,
    TaskError,
    acknowledge_task_cancellation,
    claim_task,
    expire_stale_tasks,
    reconcile_task,
    request_task_cancellation,
)


@pytest.fixture
def task_database(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'tasking.db'}",
        secret_key="unit-test-tasking-secret-at-least-32",  # pragma: allowlist secret
        image_store_path=tmp_path / "images",
        min_storage_free_bytes=0,
        task_lease_seconds=30,
    )
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db, settings
    engine.dispose()


def create_task(db: Session, operation: str) -> tuple[Task, Host, Image]:
    suffix = uuid.uuid4().hex[:8]
    user = User(username=f"operator-{suffix}", password_hash="not-used")
    host = Host(
        name=f"Linux {suffix}",
        mac_address=f"02:00:00:{suffix[:2]}:{suffix[2:4]}:{suffix[4:6]}",
    )
    image = Image(
        name=f"Imagen {suffix}",
        status="capturing" if operation == "capture" else "ready",
    )
    report = InventoryReport(
        host_id=host.id,
        report_id=str(uuid.uuid4()),
        collected_at=now(),
        source="api",
        fingerprint="f" * 64,
        data={},
    )
    db.add_all([user, host, image])
    db.flush()
    report.host_id = host.id
    db.add(report)
    db.flush()
    task = Task(
        operation=operation,
        status="approved",
        requested_by=user.id,
        host_id=host.id,
        image_id=image.id,
        inventory_report_id=report.id,
        disk_selector={"size_bytes": 1024, "logical_sector_bytes": 512},
        idempotency_key=f"task-{uuid.uuid4()}",
        reservation_key=host.id,
        phase="queued",
        total_bytes=1024,
        message="En espera.",
    )
    db.add(task)
    db.flush()
    return task, host, image


def test_pending_cancellation_is_immediate_and_idempotent(task_database):
    db, _settings = task_database
    task, _host, image = create_task(db, "capture")

    assert request_task_cancellation(db, task, reason="Cancelada por el operador.") is True
    db.commit()
    assert task.status == "cancelled"
    assert task.reservation_key is None
    assert task.cancel_acknowledged_at is not None
    assert image.status == "failed"
    assert db.scalars(select(TaskEvent).where(TaskEvent.task_id == task.id)).one().event_type == (
        "cancelled"
    )
    assert request_task_cancellation(db, task, reason="Repetición segura.") is False


def test_active_cancellation_waits_for_agent_and_marks_partial_destination(task_database):
    db, settings = task_database
    task, host, _image = create_task(db, "restore")
    claimed = claim_task(
        db,
        host.id,
        str(uuid.uuid4()),
        set(REQUIRED_CAPABILITIES["restore"]),
        settings,
    )
    assert claimed is not None
    assert request_task_cancellation(db, task, reason="Detención solicitada.") is True
    assert task.status == "assigned"
    assert task.phase == "cancelling"
    assert task.reservation_key == host.id
    claimed.attempt.started_at = now()
    db.commit()

    acknowledge_task_cancellation(
        db, task, claimed.attempt, reason="El agente detuvo la escritura."
    )
    db.commit()
    assert task.status == "cancelled"
    assert task.reservation_key is None
    assert "destino puede haber quedado incompleto" in task.failure_reason
    assert claimed.attempt.finished_at is not None
    assert claimed.attempt.phase == "failed"


def test_expired_attempt_requires_reconcile_before_new_writer(task_database):
    db, settings = task_database
    task, host, _image = create_task(db, "clone")
    first = claim_task(
        db,
        host.id,
        str(uuid.uuid4()),
        set(REQUIRED_CAPABILITIES["clone"]),
        settings,
    )
    assert first is not None
    first.attempt.lease_expires_at = now() - timedelta(seconds=1)
    db.commit()

    assert expire_stale_tasks(db) == 1
    db.commit()
    assert task.status == "intervention_required"
    assert task.reservation_key == host.id
    assert first.attempt.finished_at is not None
    with pytest.raises(TaskError, match="reconciliación"):
        request_task_cancellation(db, task, reason="No debe saltarse la reconciliación.")

    reconcile_task(db, task)
    db.commit()
    assert task.status == "approved"
    assert task.reservation_key == host.id
    assert task.transfer_slot is None

    second = claim_task(
        db,
        host.id,
        str(uuid.uuid4()),
        set(REQUIRED_CAPABILITIES["clone"]),
        settings,
    )
    assert second is not None
    assert second.attempt.attempt_number == 2
    attempts = db.scalars(
        select(TaskAttempt)
        .where(TaskAttempt.task_id == task.id)
        .order_by(TaskAttempt.attempt_number)
    ).all()
    assert [attempt.attempt_number for attempt in attempts] == [1, 2]
    assert attempts[0].finished_at is not None
