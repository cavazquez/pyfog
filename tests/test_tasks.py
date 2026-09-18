import copy
import hashlib
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from pyfog.models import Image, InventoryReport, Task, TaskAttempt, TaskEvent, now
from pyfog.storage import ArtifactStore, StorageError
from tests.conftest import csrf, issue_token
from tests.test_image_manifest import valid_manifest

CAPABILITIES = ["gpt", "partclone.ext4", "partclone.fat"]


def enqueue_capture(admin, app, host_id, inventory, *, consistency="cold"):
    token = issue_token(admin, host_id)
    inventory = copy.deepcopy(inventory)
    inventory["disks"][0].update(
        {"size_bytes": 1_073_741_824, "logical_sector_bytes": 512, "wwn": "0xTESTWWN"}
    )
    inventory_path = f"/api/v1/hosts/{host_id}/inventory"
    response = admin.post(
        inventory_path, json=inventory, headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 201, response.text

    capture_path = f"/hosts/{host_id}/capture"
    response = admin.post(
        capture_path,
        data={
            "csrf": csrf(admin, capture_path),
            "disk_key": "wwn:0xTESTWWN",
            "image_name": "Ubuntu laboratorio",
            "image_description": "Imagen de prueba",
            "image_id": "",
            "confirm": "1",
            "consistency": consistency,
            "idempotency_key": "capture-test-001",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    task_id = response.headers["location"].rsplit("/", maxsplit=1)[-1]
    with Session(app.state.engine) as db:
        task = db.get(Task, task_id)
        assert task is not None
        assert task.image_id is not None
        assert task.inventory_report_id is not None
        report = db.get(InventoryReport, task.inventory_report_id)
        assert report is not None
        return task_id, task.image_id, report.report_id, token


def claim(admin, host_id, token, *, session_id=None, capabilities=None):
    return admin.post(
        "/api/v1/tasks/claim",
        json={
            "protocol_version": 1,
            "host_id": host_id,
            "session_id": session_id or str(uuid.uuid4()),
            "capabilities": capabilities or CAPABILITIES,
        },
        headers={"Authorization": f"Bearer {token}"},
    )


def test_capture_queue_claim_upload_and_atomic_publish(admin, app, host_id, inventory):
    task_id, image_id, report_id, host_token = enqueue_capture(admin, app, host_id, inventory)

    duplicate = admin.post(
        f"/hosts/{host_id}/capture",
        data={
            "csrf": csrf(admin, f"/hosts/{host_id}/capture"),
            "disk_key": "wwn:0xTESTWWN",
            "image_name": "ignored",
            "image_description": "ignored",
            "image_id": "",
            "confirm": "1",
            "idempotency_key": "capture-test-001",
        },
        follow_redirects=False,
    )
    assert duplicate.status_code == 303
    assert duplicate.headers["location"] == f"/tasks/{task_id}"

    response = claim(admin, host_id, host_token)
    assert response.status_code == 200, response.text
    assignment = response.json()
    assert assignment["task_id"] == task_id
    assert assignment["task_token"]
    task_token = assignment["task_token"]
    task_headers = {"Authorization": f"Bearer {task_token}"}

    progress = admin.post(
        f"/api/v1/tasks/{task_id}/progress",
        json={
            "sequence": 1,
            "phase": "inspecting",
            "bytes_processed": 0,
            "total_bytes": 1_073_741_824,
            "message": "Disco validado.",
        },
        headers=task_headers,
    )
    assert progress.status_code == 200
    assert progress.json()["accepted"] is True
    heartbeat = admin.post(
        f"/api/v1/tasks/{task_id}/heartbeat",
        json={"message": "sigo inspeccionando"},
        headers=task_headers,
    )
    assert heartbeat.status_code == 200
    assert heartbeat.json()["phase"] == "inspecting"
    replay = admin.post(
        f"/api/v1/tasks/{task_id}/progress",
        json={
            "sequence": 1,
            "phase": "inspecting",
            "bytes_processed": 0,
            "total_bytes": 1_073_741_824,
            "message": "Disco validado.",
        },
        headers=task_headers,
    )
    assert replay.status_code == 200
    assert replay.json()["accepted"] is False
    regression = admin.post(
        f"/api/v1/tasks/{task_id}/progress",
        json={
            "sequence": 2,
            "phase": "assigned",
            "bytes_processed": 0,
            "total_bytes": 1_073_741_824,
            "message": "Fase anterior.",
        },
        headers=task_headers,
    )
    assert regression.status_code == 409

    artifacts = {"partitions/01-esp.img": b"esp", "partitions/02-root.partclone": b"root"}
    for index, (path, payload) in enumerate(artifacts.items()):
        headers = {
            **task_headers,
            "Content-Type": "application/octet-stream",
            "X-PyFog-Chunk-Index": str(index),
            "X-PyFog-Chunk-Offset": "0",
            "X-PyFog-Artifact-Size": str(len(payload)),
            "X-PyFog-Chunk-SHA256": hashlib.sha256(payload).hexdigest(),
        }
        uploaded = admin.post(
            f"/api/v1/tasks/{task_id}/artifacts/{path}", content=payload, headers=headers
        )
        assert uploaded.status_code == 200, uploaded.text
        assert uploaded.json()["replayed"] is False
        replayed = admin.post(
            f"/api/v1/tasks/{task_id}/artifacts/{path}", content=payload, headers=headers
        )
        assert replayed.status_code == 200
        assert replayed.json()["replayed"] is True

    manifest = copy.deepcopy(valid_manifest())
    manifest["image_id"] = image_id
    manifest["source"]["host_id"] = host_id
    manifest["source"]["inventory_report_id"] = report_id
    manifest["source"]["hostname"] = "linux-lab-01"
    finished = admin.post(
        f"/api/v1/tasks/{task_id}/result",
        json={"sequence": 2, "success": True, "manifest": manifest},
        headers=task_headers,
    )
    assert finished.status_code == 200, finished.text
    assert finished.json() == {"task_id": task_id, "status": "succeeded", "accepted": True}

    with Session(app.state.engine) as db:
        task = db.get(Task, task_id)
        image = db.get(Image, image_id)
        events = db.scalars(
            select(TaskEvent).where(TaskEvent.task_id == task_id).order_by(TaskEvent.id)
        ).all()
        assert task is not None
        assert task.status == "succeeded"
        assert image is not None
        assert image.status == "ready"
        assert image.manifest_image_id == image_id
        assert [event.event_type for event in events] == [
            "created",
            "reserved",
            "assigned",
            "progress",
            "progress",
            "completed",
        ]
    exported = admin.get(f"/tasks/{task_id}/events")
    assert exported.status_code == 200
    event_payload = exported.json()
    assert event_payload["task_id"] == task_id
    assert [event["event_type"] for event in event_payload["events"]] == [
        "created",
        "reserved",
        "assigned",
        "progress",
        "progress",
        "completed",
    ]
    assert {"task_id", "attempt_id", "duration_ms", "throughput_bytes_per_second"}.issubset(
        event_payload["events"][-1]
    )
    published = app.state.artifact_store.published_directory(image_id)
    assert (published / "manifest.json").is_file()
    assert (published / "partitions/01-esp.img").read_bytes() == b"esp"
    assert (published / "partitions/02-root.partclone").read_bytes() == b"root"
    assert not app.state.artifact_store.task_directory(task_id).exists()


def test_task_capabilities_and_expired_lease_require_intervention(admin, app, host_id, inventory):
    task_id, image_id, _report_id, host_token = enqueue_capture(admin, app, host_id, inventory)

    unsupported = claim(admin, host_id, host_token, capabilities=["gpt"])
    assert unsupported.status_code == 409

    response = claim(admin, host_id, host_token)
    assert response.status_code == 200
    task_token = response.json()["task_token"]
    headers = {"Authorization": f"Bearer {task_token}"}
    assert (
        admin.post(
            f"/api/v1/tasks/{task_id}/progress",
            json={
                "sequence": 1,
                "phase": "inspecting",
                "bytes_processed": 0,
                "total_bytes": 1_073_741_824,
                "message": "Inspeccionando.",
            },
            headers=headers,
        ).status_code
        == 200
    )
    conflict = admin.post(
        f"/api/v1/tasks/{task_id}/progress",
        json={
            "sequence": 1,
            "phase": "uploading",
            "bytes_processed": 1,
            "total_bytes": 1_073_741_824,
            "message": "Otro evento.",
        },
        headers=headers,
    )
    assert conflict.status_code == 409

    with Session(app.state.engine) as db:
        attempt = db.scalar(select(TaskAttempt).where(TaskAttempt.task_id == task_id))
        assert attempt is not None
        attempt.lease_expires_at = now() - timedelta(seconds=1)
        db.commit()

    page = admin.get("/tasks")
    assert page.status_code == 200
    assert "Requiere intervención" in page.text
    with Session(app.state.engine) as db:
        task = db.get(Task, task_id)
        image = db.get(Image, image_id)
        assert task is not None
        assert task.status == "intervention_required"
        assert image is not None
        assert image.status == "failed"
        assert task.reservation_key == host_id
    heartbeat = admin.post(
        f"/api/v1/tasks/{task_id}/heartbeat",
        json={"phase": "uploading", "message": "todavía activo"},
        headers=headers,
    )
    assert heartbeat.status_code == 409


def test_hot_capture_requires_the_agent_capability(admin, app, host_id, inventory):
    task_id, _image_id, _report_id, host_token = enqueue_capture(
        admin, app, host_id, inventory, consistency="hot"
    )

    with Session(app.state.engine) as db:
        task = db.get(Task, task_id)
        assert task is not None
        assert task.disk_selector["consistency"] == "hot"

    unsupported = claim(admin, host_id, host_token)
    assert unsupported.status_code == 409

    supported = claim(
        admin,
        host_id,
        host_token,
        capabilities=[*CAPABILITIES, "capture.hot"],
    )
    assert supported.status_code == 200, supported.text


def test_revoking_host_credential_stops_new_claims_and_existing_transfers(
    admin, app, host_id, inventory
):
    task_id, _image_id, _report_id, host_token = enqueue_capture(admin, app, host_id, inventory)
    assignment = claim(admin, host_id, host_token).json()
    task_token = assignment["task_token"]
    revoked = admin.post(
        f"/hosts/{host_id}/token",
        data={"csrf": csrf(admin), "action": "revoke"},
        follow_redirects=False,
    )
    assert revoked.status_code == 303
    heartbeat = admin.post(
        f"/api/v1/tasks/{task_id}/heartbeat",
        json={"message": "credencial revocada"},
        headers={"Authorization": f"Bearer {task_token}"},
    )
    assert heartbeat.status_code == 401
    assert claim(admin, host_id, host_token).status_code == 401


def test_artifact_store_rejects_oversize_and_unsafe_paths(tmp_path):
    store = ArtifactStore(
        tmp_path / "images", max_image_bytes=5, max_chunk_bytes=4, min_free_bytes=0
    )
    task_id = str(uuid.uuid4())
    store.task_directory(task_id, create=True)
    payload = b"1234"
    headers = {"index": 0, "offset": 0, "total_size": 0, "payload": payload}
    assert (
        store.write_chunk(
            task_id,
            "partitions/01-esp.img",
            **headers,
            sha256=hashlib.sha256(payload).hexdigest(),
        )
        is False
    )
    assert (
        store.write_chunk(
            task_id,
            "partitions/01-esp.img",
            **headers,
            sha256=hashlib.sha256(payload).hexdigest(),
        )
        is True
    )
    with pytest.raises(StorageError, match="límite"):
        store.write_chunk(
            task_id,
            "partitions/02-root.img",
            index=0,
            offset=0,
            total_size=0,
            payload=b"12",
            sha256=hashlib.sha256(b"12").hexdigest(),
        )
    with pytest.raises(StorageError, match="ruta"):
        store.artifact_path(task_id, "partitions/../outside")
