"""Operational health checks shared by probes and the authenticated status screen."""

from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from pyfog.models import Task
from pyfog.storage import ArtifactStore
from pyfog.tasking import ACTIVE_TASK_STATES


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
    }
