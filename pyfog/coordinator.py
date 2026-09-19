"""Active/passive coordinator lease with monotonic fencing tokens."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pyfog.config import MAX_COORDINATOR_LEASE_SECONDS
from pyfog.models import CoordinatorLeaseRecord, now

COORDINATOR_LEASE_ID = "primary"


class CoordinatorError(RuntimeError):
    """A coordinator cannot acquire, renew, or use its lease."""


@dataclass(frozen=True)
class FencingLease:
    holder_id: str
    term: int
    fencing_token: int
    expires_at: datetime


def require_lease(lease: FencingLease | None) -> FencingLease:
    """Convert passive coordinator state into the domain error used by mutations."""

    if lease is None:
        msg = "El coordinador activo no está disponible."
        raise CoordinatorError(msg)
    return lease


def _validate_holder(holder_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", holder_id):
        msg = "La identidad del coordinador no es válida."
        raise CoordinatorError(msg)
    return holder_id


def _lease_from_row(row: CoordinatorLeaseRecord) -> FencingLease:
    if row.lease_expires_at is None:
        msg = "El coordinador no tiene una lease activa."
        raise CoordinatorError(msg)
    return FencingLease(row.holder_id, row.term, row.fencing_token, row.lease_expires_at)


def acquire_lease(
    db: Session,
    holder_id: str,
    *,
    lease_seconds: int = 15,
    current: datetime | None = None,
) -> FencingLease | None:
    """Acquire leadership if the row is free/expired or already belongs to ``holder_id``.

    The returned fencing token changes whenever ownership changes.  Callers must attach it to
    every destructive task transition and call :func:`assert_fenced` immediately before the
    transition.  A passive node gets ``None`` and must remain read-only.
    """

    holder_id = _validate_holder(holder_id)
    if lease_seconds <= 0 or lease_seconds > MAX_COORDINATOR_LEASE_SECONDS:
        msg = "La duración de la lease debe estar entre 1 y 300 segundos."
        raise CoordinatorError(msg)
    current = current or now()
    expires = current + timedelta(seconds=lease_seconds)
    row = db.scalar(
        select(CoordinatorLeaseRecord)
        .where(CoordinatorLeaseRecord.id == COORDINATOR_LEASE_ID)
        .with_for_update()
    )
    if row is None:
        row = CoordinatorLeaseRecord(
            id=COORDINATOR_LEASE_ID,
            holder_id=holder_id,
            term=1,
            fencing_token=1,
            lease_expires_at=expires,
            updated_at=current,
        )
        db.add(row)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            return None
        return _lease_from_row(row)
    if (
        row.lease_expires_at is not None
        and row.lease_expires_at > current
        and row.holder_id != holder_id
    ):
        return None
    if row.holder_id != holder_id:
        row.term += 1
        row.fencing_token += 1
        row.holder_id = holder_id
    row.lease_expires_at = expires
    row.updated_at = current
    db.flush()
    return _lease_from_row(row)


def renew_lease(
    db: Session,
    lease: FencingLease,
    *,
    lease_seconds: int = 15,
    current: datetime | None = None,
) -> FencingLease:
    """Renew only the exact current owner and fencing token."""

    current = current or now()
    row = db.get(CoordinatorLeaseRecord, COORDINATOR_LEASE_ID)
    if row is None or row.holder_id != lease.holder_id or row.fencing_token != lease.fencing_token:
        msg = "La lease del coordinador perdió el fencing token."
        raise CoordinatorError(msg)
    if row.lease_expires_at is None or row.lease_expires_at <= current:
        msg = "La lease del coordinador venció."
        raise CoordinatorError(msg)
    if lease_seconds <= 0 or lease_seconds > MAX_COORDINATOR_LEASE_SECONDS:
        msg = "La duración de la lease debe estar entre 1 y 300 segundos."
        raise CoordinatorError(msg)
    row.lease_expires_at = current + timedelta(seconds=lease_seconds)
    row.updated_at = current
    db.flush()
    return _lease_from_row(row)


def assert_fenced(
    db: Session,
    lease: FencingLease,
    *,
    current: datetime | None = None,
) -> None:
    """Fail closed when a stale active node tries to mutate coordinator-owned state."""

    current = current or now()
    row = db.get(CoordinatorLeaseRecord, COORDINATOR_LEASE_ID)
    if (
        row is None
        or row.holder_id != lease.holder_id
        or row.fencing_token != lease.fencing_token
        or row.lease_expires_at is None
        or row.lease_expires_at <= current
    ):
        msg = "La operación fue bloqueada por fencing del coordinador."
        raise CoordinatorError(msg)


def release_lease(
    db: Session,
    lease: FencingLease,
    *,
    current: datetime | None = None,
) -> None:
    """Release ownership and invalidate the old token immediately."""

    current = current or now()
    row = db.get(CoordinatorLeaseRecord, COORDINATOR_LEASE_ID)
    if row is None or row.holder_id != lease.holder_id or row.fencing_token != lease.fencing_token:
        msg = "No se puede liberar una lease que ya no pertenece al coordinador."
        raise CoordinatorError(msg)
    row.holder_id = ""
    row.term += 1
    row.fencing_token += 1
    row.lease_expires_at = current
    row.updated_at = current
    db.flush()


def coordinator_status(db: Session, *, current: datetime | None = None) -> dict[str, object]:
    """Return a secret-free active/passive status for health checks."""

    current = current or now()
    row = db.get(CoordinatorLeaseRecord, COORDINATOR_LEASE_ID)
    if row is None or row.lease_expires_at is None or row.lease_expires_at <= current:
        return {"status": "passive", "mode": "active-passive", "term": row.term if row else 0}
    return {
        "status": "active",
        "mode": "active-passive",
        "term": row.term,
        "expires_at": row.lease_expires_at.isoformat() + "Z",
    }
