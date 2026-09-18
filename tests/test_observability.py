import json
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from pyfog.config import Settings
from pyfog.database import Base, make_engine
from pyfog.health import operational_metrics
from pyfog.models import Host, Image, InventoryReport, Task, TaskEvent, User, now
from pyfog.tasking import (
    REQUIRED_CAPABILITIES,
    TASK_EVENT_TYPES,
    TASK_STATES,
    TaskError,
    add_event,
    claim_task,
    failure_code,
    record_progress,
)


@pytest.fixture
def observability_database(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'observability.db'}",
        secret_key="unit-test-observability-secret-at-least-32",  # pragma: allowlist secret
        image_store_path=tmp_path / "images",
        min_storage_free_bytes=0,
    )
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db, settings
    engine.dispose()


def create_task(db: Session) -> Task:
    suffix = uuid.uuid4().hex[:8]
    user = User(username=f"operator-{suffix}", password_hash="not-used")
    host = Host(
        name=f"Linux {suffix}",
        mac_address=f"02:00:00:{suffix[:2]}:{suffix[2:4]}:{suffix[4:6]}",
    )
    image = Image(name=f"Imagen {suffix}", status="capturing")
    db.add_all([user, host, image])
    db.flush()
    report = InventoryReport(
        host_id=host.id,
        report_id=str(uuid.uuid4()),
        collected_at=now(),
        source="api",
        fingerprint="f" * 64,
        data={},
    )
    db.add(report)
    db.flush()
    task = Task(
        operation="capture",
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
    return task


def test_task_events_record_metrics_codes_and_redacted_messages(observability_database):
    db, settings = observability_database
    task = create_task(db)
    db.commit()
    claimed = claim_task(
        db,
        task.host_id,
        str(uuid.uuid4()),
        set(REQUIRED_CAPABILITIES["capture"]),
        settings,
    )
    assert claimed is not None
    claimed.attempt.started_at = now() - timedelta(seconds=2)
    assert (
        record_progress(
            db,
            task,
            claimed.attempt,
            settings,
            sequence=1,
            phase="capturing",
            bytes_processed=512,
            total_bytes=1024,
            message="Avance normal.",
        )
        is True
    )
    error = add_event(
        db,
        task,
        event_type="error",
        attempt=claimed.attempt,
        sequence=2,
        phase="failed",
        bytes_processed=512,
        total_bytes=1024,
        failure_code=failure_code("El manifiesto no coincide."),
        message="Bearer abc123 token=super-secret",
    )
    db.commit()

    progress = db.scalar(
        select(TaskEvent).where(TaskEvent.task_id == task.id, TaskEvent.event_type == "progress")
    )
    assert progress is not None
    assert progress.duration_ms is not None
    assert progress.duration_ms >= 1_000
    assert progress.throughput_bytes_per_second is not None
    assert error.failure_code == "manifest_invalid"
    assert "abc123" not in error.message
    assert "super-secret" not in error.message

    with pytest.raises(TaskError, match="código de fallo"):
        add_event(db, task, event_type="error", failure_code="task-id")

    metrics = operational_metrics(db)
    assert set(metrics["tasks"]["by_status"]) == set(TASK_STATES)
    assert set(metrics["events"]["by_type"]) == set(TASK_EVENT_TYPES)
    assert metrics["events"]["by_type"]["progress"] == 1
    assert metrics["events"]["failures_by_code"]["manifest_invalid"] == 1
    assert metrics["transfer"]["average_throughput_bytes_per_second"] is not None
    serialized = json.dumps(metrics, ensure_ascii=False)
    assert task.id not in serialized


def test_health_metrics_endpoint_is_aggregate_and_secret_free(client):
    response = client.get("/health/metrics")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "tasks" in body["metrics"]
    assert "events" in body["metrics"]
    assert "task_id" not in response.text
    assert "message" not in response.text
