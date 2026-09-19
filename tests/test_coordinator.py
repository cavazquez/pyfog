from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from pyfog.coordinator import (
    CoordinatorError,
    acquire_lease,
    assert_fenced,
    coordinator_status,
    release_lease,
)


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
