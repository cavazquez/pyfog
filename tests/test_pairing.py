import copy
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pyfog.models import Host, InventoryReport, PairingRequest, now
from tests.conftest import csrf


def create_request(client, *, mac="52:54:00:12:34:56", session_id=None, challenge=None):
    session_id = session_id or str(uuid.uuid4())
    challenge = challenge or "Challenge-12345678"
    response = client.post(
        "/api/v1/pairing/requests",
        json={"session_id": session_id, "mac_address": mac, "challenge": challenge},
    )
    assert response.status_code == 202, response.text
    return response.json()


def test_pxe_request_requires_approval_and_consumes_capability(admin, app, inventory):
    created = create_request(admin)
    request_id = created["request_id"]
    headers = {"Authorization": f"Bearer {created['poll_token']}"}
    pending = admin.get(f"/api/v1/pairing/requests/{request_id}", headers=headers)
    assert pending.json()["status"] == "pending"
    page = admin.get("/pairing")
    assert created["challenge"] in page.text
    assert created["poll_token"] not in page.text

    wrong = admin.post(
        f"/pairing/{request_id}/approve",
        data={"csrf": csrf(admin), "challenge": "Wrong-1234567890"},
    )
    assert wrong.status_code == 422

    approved = admin.post(
        f"/pairing/{request_id}/approve",
        data={"csrf": csrf(admin), "challenge": created["challenge"]},
        follow_redirects=False,
    )
    assert approved.status_code == 303
    state = admin.get(f"/api/v1/pairing/requests/{request_id}", headers=headers).json()
    assert state["status"] == "approved"
    host_id = state["host_id"]
    inventory_token = state["inventory_token"]

    submitted = admin.post(
        f"/api/v1/pairing/requests/{request_id}/inventory",
        json=inventory,
        headers={"Authorization": f"Bearer {inventory_token}"},
    )
    assert submitted.status_code == 201
    assert submitted.json()["host_id"] == host_id
    with Session(app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(Host)) == 1
        assert db.scalar(select(func.count()).select_from(InventoryReport)) == 1

    replay = admin.post(
        f"/api/v1/pairing/requests/{request_id}/inventory",
        json=inventory,
        headers={"Authorization": f"Bearer {inventory_token}"},
    )
    assert replay.status_code == 200
    altered = copy.deepcopy(inventory)
    altered["report_id"] = str(uuid.uuid4())
    assert (
        admin.post(
            f"/api/v1/pairing/requests/{request_id}/inventory",
            json=altered,
            headers={"Authorization": f"Bearer {inventory_token}"},
        ).status_code
        == 401
    )
    after = admin.get("/pairing")
    assert inventory_token not in after.text


def test_pxe_approval_can_associate_existing_host_without_duplicate(admin, app, host_id):
    created = create_request(admin)
    response = admin.post(
        f"/pairing/{created['request_id']}/approve",
        data={
            "csrf": csrf(admin),
            "challenge": created["challenge"],
            "host_id": host_id,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with Session(app.state.engine) as db:
        pairing = db.get(PairingRequest, created["request_id"])
        assert pairing is not None
        assert pairing.host_id == host_id
        assert db.scalar(select(func.count()).select_from(Host)) == 1


def test_rejected_request_cannot_submit_inventory_and_session_is_unique(
    client, admin, app, inventory
):
    session_id = str(uuid.uuid4())
    created = create_request(client, session_id=session_id, mac="52:54:00:aa:bb:dd")
    duplicate = client.post(
        "/api/v1/pairing/requests",
        json={
            "session_id": session_id,
            "mac_address": "52:54:00:aa:bb:dd",
            "challenge": "Challenge-duplicate",
        },
    )
    assert duplicate.status_code == 409
    rejected = admin.post(
        f"/pairing/{created['request_id']}/reject",
        data={"csrf": csrf(admin), "reason": "No reconozco este equipo"},
        follow_redirects=False,
    )
    assert rejected.status_code == 303
    status = client.get(
        f"/api/v1/pairing/requests/{created['request_id']}",
        headers={"Authorization": f"Bearer {created['poll_token']}"},
    )
    assert status.json()["status"] == "rejected"
    denied = client.post(
        f"/api/v1/pairing/requests/{created['request_id']}/inventory",
        json=inventory,
        headers={"Authorization": f"Bearer {created['poll_token']}"},
    )
    assert denied.status_code == 410
    with Session(app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(Host)) == 0


def test_pxe_request_expires_and_cannot_be_approved(client, admin, app):
    created = create_request(client, mac="52:54:00:aa:bb:cc")
    with Session(app.state.engine) as db:
        pairing = db.get(PairingRequest, created["request_id"])
        assert pairing is not None
        pairing.expires_at = now() - timedelta(seconds=1)
        db.commit()
    status = client.get(
        f"/api/v1/pairing/requests/{created['request_id']}",
        headers={"Authorization": f"Bearer {created['poll_token']}"},
    )
    assert status.status_code == 200
    assert status.json()["status"] == "expired"
    approval = admin.post(
        f"/pairing/{created['request_id']}/approve",
        data={"csrf": csrf(admin), "challenge": created["challenge"]},
    )
    assert approval.status_code == 409


@pytest.mark.parametrize("bad", ["not-a-uuid", "", "x" * 257])
def test_pairing_status_rejects_invalid_capability(client, bad):
    created = create_request(client)
    response = client.get(
        f"/api/v1/pairing/requests/{created['request_id']}",
        headers={"Authorization": f"Bearer {bad}"},
    )
    assert response.status_code == 401
