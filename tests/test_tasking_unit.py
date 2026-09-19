import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pyfog.config import Settings
from pyfog.coordinator import acquire_lease
from pyfog.database import Base, make_engine
from pyfog.models import (
    AgentCredential,
    Host,
    Image,
    InventoryReport,
    Task,
    TaskAttempt,
    TaskEvent,
    User,
    now,
)
from pyfog.tasking import (
    REQUIRED_CAPABILITIES,
    TaskError,
    acknowledge_task_cancellation,
    add_event,
    claim_task,
    event_metrics,
    expire_stale_tasks,
    failure_code,
    reconcile_task,
    record_progress,
    request_task_cancellation,
    required_capabilities,
    safe_event_message,
    touch_attempt,
    transition_task,
)

MULTI_DISK_COUNT = 2
MESSAGE_LIMIT = 500
SECOND_ATTEMPT = 2
PROGRESS_BYTES = 100
TOTAL_BYTES = 200


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
    assert "concesión del agente venció" in task.failure_reason
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
    assert second.attempt.attempt_number == SECOND_ATTEMPT
    attempts = db.scalars(
        select(TaskAttempt)
        .where(TaskAttempt.task_id == task.id)
        .order_by(TaskAttempt.attempt_number)
    ).all()
    assert [attempt.attempt_number for attempt in attempts] == [1, SECOND_ATTEMPT]
    assert attempts[0].finished_at is not None


def test_required_capabilities_include_manifest_features():
    manifest = {
        "firmware": {"type": "bios"},
        "capabilities": {
            "filesystems": ["xfs", "btrfs"],
            "disks": MULTI_DISK_COUNT,
            "encryption": "luks2",
            "volumes": "lvm",
        },
    }

    required = required_capabilities("clone", manifest)

    assert REQUIRED_CAPABILITIES["clone"] <= required
    assert {
        "mbr",
        "partclone.xfs",
        "partclone.btrfs",
        "multidisk",
        "luks2",
        "key-provider",
        "lvm-linear",
    } <= required
    assert required_capabilities("capture", manifest) == REQUIRED_CAPABILITIES["capture"]
    assert (
        required_capabilities("restore", {"capabilities": None}) == REQUIRED_CAPABILITIES["restore"]
    )
    assert (
        required_capabilities("restore", {"capabilities": {"filesystems": "xfs"}})
        == REQUIRED_CAPABILITIES["restore"]
    )
    assert (
        required_capabilities("restore", {"capabilities": {"filesystems": ["ext4"]}})
        == REQUIRED_CAPABILITIES["restore"]
    )
    assert required_capabilities("restore", {"capabilities": {"volumes": "raid1"}}) == (
        REQUIRED_CAPABILITIES["restore"] | {"raid1"}
    )
    with pytest.raises(TaskError, match="operación"):
        required_capabilities("unknown")


def test_transition_task_enforces_state_and_cleans_terminal_reservation(task_database):
    db, _settings = task_database
    task, host, _image = create_task(db, "capture")

    transition_task(db, task, "approved")
    with pytest.raises(TaskError, match="estado"):
        transition_task(db, task, "unknown")
    with pytest.raises(TaskError, match="fase"):
        transition_task(db, task, "approved", phase="unknown")
    with pytest.raises(TaskError, match="No se puede"):
        transition_task(db, task, "succeeded")
    with pytest.raises(TaskError, match="reconciliar"):
        reconcile_task(db, task)

    transition_task(db, task, "assigned", phase="assigned", message="a" * 600)
    assert task.assigned_at is not None
    assert len(task.message) == MESSAGE_LIMIT
    transition_task(db, task, "running", phase="running")
    transition_task(db, task, "verifying", phase="verifying")
    task.reservation_key = host.id
    task.transfer_slot = 1
    transition_task(db, task, "succeeded", phase="completed")

    assert task.started_at is not None
    assert task.completed_at is not None
    assert task.reservation_key is None
    assert task.transfer_slot is None


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("lease expired", "lease_expired"),
        ("La tarea fue cancelada", "cancelled"),
        ("Requiere reconciliación", "reconciliation_required"),
        ("Manifiesto inválido", "manifest_invalid"),
        ("Error de integridad del artefacto", "artifact_integrity"),
        ("Sin espacio en el almacén", "storage_error"),
        ("El destino quedó incompleto", "destination_incomplete"),
        ("Error inesperado", "agent_error"),
    ],
)
def test_failure_code_maps_stable_metric_labels(reason, expected):
    assert failure_code(reason) == expected


def test_expiry_persists_before_claiming_another_task(task_database):
    db, settings = task_database
    expired_task, expired_host, expired_image = create_task(db, "capture")
    first = claim_task(
        db,
        expired_host.id,
        "session-expiring",
        set(REQUIRED_CAPABILITIES["capture"]),
        settings,
    )
    assert first is not None
    first.attempt.lease_expires_at = now() - timedelta(seconds=1)
    db.commit()

    next_task, next_host, _next_image = create_task(db, "capture")
    replacement = claim_task(
        db,
        next_host.id,
        "session-replacement",
        set(REQUIRED_CAPABILITIES["capture"]),
        settings,
    )

    assert replacement is not None
    assert replacement.task.id == next_task.id
    assert expired_task.status == "intervention_required"
    assert expired_image.status == "failed"


def test_event_helpers_redact_measure_and_validate(task_database):
    db, _settings = task_database
    task, _host, _image = create_task(db, "capture")
    current = task.created_at + timedelta(seconds=2)

    message = safe_event_message("  Bearer abc.def token=secret password=hunter2  ")
    assert "Bearer [redacted]" in message
    assert "token=[redacted]" in message
    assert "password=[redacted]" in message
    assert len(safe_event_message("x" * 600)) == MESSAGE_LIMIT
    assert event_metrics(task, None, current, 0) == (2_000, None)
    assert event_metrics(task, None, current, 2_048) == (2_000, 1_024)

    event = add_event(
        db,
        task,
        event_type="completed",
        bytes_processed=2_048,
        total_bytes=4_096,
        message="token=secret",
    )
    assert event.duration_ms is not None
    assert event.message == "token=[redacted]"
    with pytest.raises(TaskError, match="tipo de evento"):
        add_event(db, task, event_type="unknown")
    with pytest.raises(TaskError, match="código de fallo"):
        add_event(db, task, event_type="error", failure_code="unknown")


def test_claim_task_rejects_replayed_sessions_and_respects_transfer_slot(task_database):
    db, settings = task_database
    task, host, image = create_task(db, "restore")
    image.manifest_json = {"capabilities": {}}
    task.disk_selector = {"disks": [{"consistency": "hot"}]}
    capabilities = set(REQUIRED_CAPABILITIES["restore"])
    db.commit()
    with pytest.raises(TaskError, match="capacidades"):
        claim_task(db, host.id, "session-missing-capability", set(), settings)
    credential = AgentCredential(
        host_id=host.id,
        token_hash="c" * 64,
        expires_at=now() + timedelta(days=1),
    )
    db.add(credential)
    db.flush()

    claimed = claim_task(
        db,
        host.id,
        "session-one",
        capabilities,
        settings,
        agent_credential_id=credential.id,
    )

    assert claimed is not None
    assert claimed.task.id == task.id
    assert claimed.attempt.agent_credential_id == credential.id
    assert claimed.attempt.attempt_number == 1
    assert claimed.token
    with pytest.raises(TaskError, match="sesión"):
        claim_task(db, host.id, "session-one", capabilities, settings)

    second_task, second_host, _second_image = create_task(db, "capture")
    assert (
        claim_task(
            db,
            second_host.id,
            "session-two",
            set(REQUIRED_CAPABILITIES["capture"]),
            settings,
        )
        is None
    )
    assert second_task.status == "approved"
    task.transfer_slot = None
    db.commit()
    assert (
        claim_task(
            db,
            str(uuid.uuid4()),
            "session-no-task",
            set(REQUIRED_CAPABILITIES["capture"]),
            settings,
        )
        is None
    )


def test_claim_task_honors_coordinator_fencing(task_database):
    db, settings = task_database
    task, host, _image = create_task(db, "capture")
    lease = acquire_lease(db, "tasking-test")
    assert lease is not None

    claimed = claim_task(
        db,
        host.id,
        "session-fenced",
        set(REQUIRED_CAPABILITIES["capture"]),
        settings,
        coordinator_lease=lease,
    )

    assert claimed is not None
    assert claimed.task.id == task.id


def test_claim_task_rolls_back_on_commit_conflict(task_database, monkeypatch):
    db, settings = task_database
    task, host, _image = create_task(db, "capture")
    db.commit()
    conflict = IntegrityError("commit", {}, RuntimeError("conflict"))

    def fail_commit():
        raise conflict

    monkeypatch.setattr(db, "commit", fail_commit)
    assert (
        claim_task(
            db,
            host.id,
            "session-conflict",
            set(REQUIRED_CAPABILITIES["capture"]),
            settings,
        )
        is None
    )
    assert db.get(Task, task.id).status == "approved"


def test_active_capture_cancellation_acknowledgement_is_guarded(task_database):
    db, settings = task_database
    task, host, image = create_task(db, "capture")
    task.disk_selector = {"disks": [{"consistency": "hot"}]}
    claimed = claim_task(
        db,
        host.id,
        "session-cancel",
        set(REQUIRED_CAPABILITIES["capture"]) | {"capture.hot"},
        settings,
    )
    assert claimed is not None
    with pytest.raises(TaskError, match="cancelación"):
        acknowledge_task_cancellation(db, task, claimed.attempt, reason="Sin solicitud.")

    assert request_task_cancellation(db, task, reason="Detención solicitada.") is True
    assert request_task_cancellation(db, task, reason="Repetición segura.") is False
    image.status = "capturing"
    acknowledge_task_cancellation(db, task, claimed.attempt, reason="Agente detenido.")
    assert task.status == "cancelled"
    assert image.status == "failed"
    acknowledge_task_cancellation(db, task, claimed.attempt, reason="Repetición segura.")


def test_touch_attempt_enforces_monotonic_progress_and_cancellation(task_database):
    db, settings = task_database
    task, host, _image = create_task(db, "capture")
    task.total_bytes = TOTAL_BYTES
    claimed = claim_task(
        db,
        host.id,
        "session-touch",
        set(REQUIRED_CAPABILITIES["capture"]),
        settings,
    )
    assert claimed is not None

    touch_attempt(
        db,
        task,
        claimed.attempt,
        settings,
        phase="inspecting",
        bytes_processed=PROGRESS_BYTES,
        total_bytes=TOTAL_BYTES,
        message="Inspeccionando.",
    )
    assert task.status == "running"
    assert claimed.attempt.started_at is not None

    for invalid_phase in ("queued", "completed", "failed"):
        with pytest.raises(TaskError, match="fase"):
            touch_attempt(
                db,
                task,
                claimed.attempt,
                settings,
                phase=invalid_phase,
                bytes_processed=PROGRESS_BYTES,
                total_bytes=TOTAL_BYTES,
                message="inválido",
            )
    with pytest.raises(TaskError, match="retroceder"):
        touch_attempt(
            db,
            task,
            claimed.attempt,
            settings,
            phase="assigned",
            bytes_processed=PROGRESS_BYTES,
            total_bytes=TOTAL_BYTES,
            message="retroceso",
        )
    with pytest.raises(TaskError, match="progreso"):
        touch_attempt(
            db,
            task,
            claimed.attempt,
            settings,
            phase="uploading",
            bytes_processed=PROGRESS_BYTES - 1,
            total_bytes=TOTAL_BYTES,
            message="retroceso",
        )
    with pytest.raises(TaskError, match="tamaño total"):
        touch_attempt(
            db,
            task,
            claimed.attempt,
            settings,
            phase="uploading",
            bytes_processed=PROGRESS_BYTES,
            total_bytes=TOTAL_BYTES - 1,
            message="retroceso",
        )
    with pytest.raises(TaskError, match="supera"):
        touch_attempt(
            db,
            task,
            claimed.attempt,
            settings,
            phase="uploading",
            bytes_processed=TOTAL_BYTES + 1,
            total_bytes=TOTAL_BYTES,
            message="exceso",
        )

    touch_attempt(
        db,
        task,
        claimed.attempt,
        settings,
        phase="uploading",
        bytes_processed=TOTAL_BYTES,
        total_bytes=None,
        message="Subiendo.",
    )
    task.cancel_requested_at = now()
    touch_attempt(
        db,
        task,
        claimed.attempt,
        settings,
        phase="finalizing",
        bytes_processed=TOTAL_BYTES,
        total_bytes=TOTAL_BYTES,
        message="Finalizando.",
    )
    assert task.phase == "cancelling"
    assert task.message.startswith("Cancelación solicitada")


def test_record_progress_is_idempotent_and_enters_verification(task_database):
    db, settings = task_database
    task, host, _image = create_task(db, "capture")
    task.total_bytes = TOTAL_BYTES
    claimed = claim_task(
        db,
        host.id,
        "session-progress",
        set(REQUIRED_CAPABILITIES["capture"]),
        settings,
    )
    assert claimed is not None

    progress = {
        "sequence": 1,
        "phase": "inspecting",
        "bytes_processed": PROGRESS_BYTES,
        "total_bytes": TOTAL_BYTES,
        "message": "Inspeccionando.",
    }
    with pytest.raises(TaskError, match="fase"):
        record_progress(
            db,
            task,
            claimed.attempt,
            settings,
            **{**progress, "phase": "queued"},
        )
    assert record_progress(db, task, claimed.attempt, settings, **progress) is True
    assert record_progress(db, task, claimed.attempt, settings, **progress) is False
    with pytest.raises(TaskError, match="secuencia"):
        record_progress(
            db,
            task,
            claimed.attempt,
            settings,
            **{**progress, "message": "Otro mensaje."},
        )
    assert (
        record_progress(
            db,
            task,
            claimed.attempt,
            settings,
            sequence=-1,
            phase="inspecting",
            bytes_processed=PROGRESS_BYTES,
            total_bytes=TOTAL_BYTES,
            message="Anterior.",
        )
        is False
    )

    assert (
        record_progress(
            db,
            task,
            claimed.attempt,
            settings,
            sequence=2,
            phase="verifying",
            bytes_processed=TOTAL_BYTES,
            total_bytes=None,
            message="Verificando.",
        )
        is True
    )
    assert task.status == "verifying"
    assert claimed.attempt.last_sequence == SECOND_ATTEMPT
