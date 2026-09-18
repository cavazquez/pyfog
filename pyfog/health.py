"""Operational health checks and bounded task metrics."""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from pyfog.models import Task, TaskEvent, now
from pyfog.storage import ArtifactStore
from pyfog.tasking import (
    ACTIVE_TASK_STATES,
    TASK_EVENT_TYPES,
    TASK_FAILURE_CODES,
    TASK_STATES,
)

METRICS_WINDOW = timedelta(hours=24)


def operational_metrics(db: Session, *, current: datetime | None = None) -> dict[str, Any]:
    """Return aggregate task metrics with fixed, non-sensitive label sets."""

    current = current or now()
    cutoff = current - METRICS_WINDOW
    status_counts = dict.fromkeys(TASK_STATES, 0)
    for status, count in db.execute(select(Task.status, func.count(Task.id)).group_by(Task.status)):
        if status in status_counts:
            status_counts[status] = int(count)
    active_bytes, active_total = db.execute(
        select(
            func.coalesce(func.sum(Task.bytes_processed), 0),
            func.coalesce(func.sum(Task.total_bytes), 0),
        ).where(Task.status.in_(ACTIVE_TASK_STATES))
    ).one()
    event_counts = dict.fromkeys(TASK_EVENT_TYPES, 0)
    for event_type, count in db.execute(
        select(TaskEvent.event_type, func.count(TaskEvent.id))
        .where(TaskEvent.created_at >= cutoff)
        .group_by(TaskEvent.event_type)
    ):
        if event_type in event_counts:
            event_counts[event_type] = int(count)
    failure_counts = dict.fromkeys(TASK_FAILURE_CODES, 0)
    for code, count in db.execute(
        select(TaskEvent.failure_code, func.count(TaskEvent.id))
        .where(TaskEvent.created_at >= cutoff, TaskEvent.failure_code.is_not(None))
        .group_by(TaskEvent.failure_code)
    ):
        if code in failure_counts:
            failure_counts[code] = int(count)
    duration, throughput = db.execute(
        select(
            func.avg(TaskEvent.duration_ms),
            func.avg(TaskEvent.throughput_bytes_per_second),
        ).where(
            TaskEvent.created_at >= cutoff,
            TaskEvent.event_type.in_({"completed", "error", "cancelled"}),
        )
    ).one()
    recent_results = {"succeeded": 0, "failed": 0, "cancelled": 0}
    for status, count in db.execute(
        select(Task.status, func.count(Task.id))
        .where(Task.completed_at >= cutoff)
        .group_by(Task.status)
    ):
        if status in recent_results:
            recent_results[status] = int(count)
    return {
        "window_seconds": int(METRICS_WINDOW.total_seconds()),
        "tasks": {
            "by_status": status_counts,
            "active": sum(status_counts[status] for status in ACTIVE_TASK_STATES),
            "active_bytes_processed": int(active_bytes or 0),
            "active_total_bytes": int(active_total or 0),
            "recent_results": recent_results,
        },
        "events": {"by_type": event_counts, "failures_by_code": failure_counts},
        "transfer": {
            "average_duration_ms": int(duration) if duration is not None else None,
            "average_throughput_bytes_per_second": int(throughput)
            if throughput is not None
            else None,
        },
    }


def operational_snapshot(db: Session, store: ArtifactStore) -> dict[str, Any]:
    """Return non-sensitive process, database, storage and task-coordinator information."""

    db.execute(text("SELECT 1"))
    active_task_ids = list(
        db.scalars(select(Task.id).where(Task.status.in_(ACTIVE_TASK_STATES))).all()
    )
    storage = store.status()
    staging = store.staging_status(active_task_ids)
    return {
        "database": "ok",
        "storage": {
            "status": "ok",
            "free_bytes": storage["free_bytes"],
            "total_bytes": storage["total_bytes"],
            "published_images": storage["published_images"],
            "staging_images": storage["staging_images"],
        },
        # Task claiming and leases are deliberately integrated into the web process in this MVP.
        "coordinator": {"status": "available", "mode": "integrated"},
        "active_tasks": len(active_task_ids),
        "staging": {
            "active": staging["active"],
            "orphaned": staging["orphaned"],
            "unsafe": staging["unsafe"],
        },
        "metrics": operational_metrics(db),
    }
