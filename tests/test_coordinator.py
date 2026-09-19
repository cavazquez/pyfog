from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from pyfog.coordinator import (
    CoordinatorError,
    FencingLease,
    _lease_from_row,
    acquire_lease,
    assert_fenced,
    coordinator_status,
    release_lease,
    renew_lease,
    require_lease,
)
from pyfog.models import CoordinatorLeaseRecord


def test_active_passive_lease_fences_stale_coordinator(app):
    first_now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC).replace(tzinfo=None)
    with Session(app.state.engine) as db:
        first = acquire_lease(db, "coordinator-a", lease_seconds=10, current=first_now)
        assert first is not None
        db.commit()

    with Session(app.state.engine) as db:
        assert acquire_lease(db, "coordinator-b", current=first_now) is None
        db.commit()

    second_now = first.expires_at + timedelta(seconds=1)
    with Session(app.state.engine) as db:
        second = acquire_lease(db, "coordinator-b", lease_seconds=10, current=second_now)
        assert second is not None
        assert second.fencing_token > first.fencing_token
        db.commit()

    with Session(app.state.engine) as db:
        with pytest.raises(CoordinatorError, match="fencing"):
            assert_fenced(db, first, current=second_now)
        assert_fenced(db, second, current=second_now)
        assert coordinator_status(db, current=second_now)["status"] == "active"
        release_lease(db, second, current=second_now)
        db.commit()
        assert coordinator_status(db, current=second_now)["status"] == "passive"


def test_coordinator_rejects_invalid_lease_operations(app):
    with pytest.raises(CoordinatorError, match="no está disponible"):
        require_lease(None)

    with Session(app.state.engine) as db:
        with pytest.raises(CoordinatorError, match="identidad"):
            acquire_lease(db, "invalid holder")
        with pytest.raises(CoordinatorError, match="duración"):
            acquire_lease(db, "coordinator-a", lease_seconds=0)
        assert coordinator_status(db)["status"] == "passive"
        row = CoordinatorLeaseRecord(id="primary", lease_expires_at=None)
        with pytest.raises(CoordinatorError, match="lease activa"):
            _lease_from_row(row)

        lease = acquire_lease(db, "coordinator-a", lease_seconds=10)
        assert lease is not None
        db.commit()

    with Session(app.state.engine) as db:
        mismatched = FencingLease(
            "other-coordinator", lease.term, lease.fencing_token, lease.expires_at
        )
        with pytest.raises(CoordinatorError, match="fencing token"):
            renew_lease(db, mismatched, current=lease.expires_at - timedelta(seconds=1))
        with pytest.raises(CoordinatorError, match="duración"):
            renew_lease(db, lease, lease_seconds=0, current=lease.expires_at - timedelta(seconds=1))
        with pytest.raises(CoordinatorError, match="venció"):
            renew_lease(db, lease, current=lease.expires_at + timedelta(seconds=1))
