from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import Request

from pyfog import api, web
from pyfog.coordinator import CoordinatorError, FencingLease
from pyfog.models import Image, InventoryReport, PairingRequest, Task, TaskAttempt, User, now
from pyfog.security import digest
from tests.conftest import csrf
from tests.test_image_manifest import valid_manifest_v2


def request_for_app(app, *, token: str = "", query: bytes = b"") -> Request:
    headers = [(b"authorization", f"Bearer {token}".encode())] if token else []
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "raw_path": b"/",
            "query_string": query,
            "headers": headers,
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
            "app": app,
            "session": {},
        }
    )


def standalone_task() -> tuple[Task, TaskAttempt]:
    timestamp = datetime(2026, 9, 19, 12, 30, tzinfo=UTC)
    task = Task(
        operation="clone",
        status="running",
        requested_by=1,
        host_id="host-id",
        image_id="image-id",
        inventory_report_id="report-id",
        disk_selector={
            "disks": [
                {"stable_id": "wwn:disk-a", "clone_hostname": "clone.example"},
                {"stable_id": "wwn:disk-b"},
            ],
            "primary": {"stable_id": "wwn:disk-a", "clone_hostname": "clone.example"},
        },
        idempotency_key="idempotency-key",
        phase="uploading",
        bytes_processed=4,
        total_bytes=8,
        message="En progreso.",
        failure_reason="",
        created_at=timestamp,
        updated_at=timestamp,
    )
    task.id = "task-id"
    task.cancel_requested_at = timestamp
    attempt = TaskAttempt(
        task_id=task.id,
        attempt_number=2,
        agent_session_id="session-id",
        capability_hash=digest("task-token"),
        lease_expires_at=timestamp + timedelta(minutes=1),
        last_heartbeat_at=timestamp,
        last_sequence=3,
        phase="uploading",
        bytes_processed=4,
        total_bytes=8,
    )
    attempt.id = "attempt-id"
    return task, attempt


def test_api_task_payload_and_authorization_fail_closed() -> None:
    task, attempt = standalone_task()
    payload = api.task_payload(task, attempt, "task-token")
    assert payload["disk"] == task.disk_selector["primary"]
    assert payload["disks"] == task.disk_selector["disks"]
    assert payload["attempt_id"] == "attempt-id"
    assert payload["task_token"] == "task-token"
    assert payload["cancel_requested"] is True

    pairing = PairingRequest(
        id="pairing-id",
        session_id="session-id",
        mac_address="52:54:00:12:34:56",
        challenge="challenge",
        challenge_hash=digest("challenge"),
        capability_hash=digest("poll-token"),
        expires_at=now() + timedelta(minutes=5),
    )
    request = request_for_app(None, token="poll-token")
    assert api.authorize_pairing(request, pairing) == "poll-token"
    with pytest.raises(HTTPException) as error:
        api.authorize_pairing(request_for_app(None, token="wrong"), pairing)
    assert error.value.status_code == 401


def test_api_coordinator_fence_stores_lease_and_translates_passive_errors(
    app, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = request_for_app(app)
    lease = FencingLease("primary", 1, 7, now() + timedelta(seconds=30))
    monkeypatch.setattr(
        app.state,
        "settings",
        SimpleNamespace(coordinator_id="primary", coordinator_lease_seconds=15),
    )
    monkeypatch.setattr(api, "acquire_lease", lambda *_args, **_kwargs: lease)
    monkeypatch.setattr(api, "require_lease", lambda value: value)
    monkeypatch.setattr(api, "assert_fenced", lambda *_args, **_kwargs: None)
    with Session(app.state.engine) as db:
        assert api.coordinator_fence(request, db) == lease
    assert request.state.coordinator_lease == lease

    def unavailable(*_args: object, **_kwargs: object):
        raise CoordinatorError("coordinador pasivo")

    monkeypatch.setattr(api, "acquire_lease", unavailable)
    with Session(app.state.engine) as db, pytest.raises(HTTPException) as error:
        api.coordinator_fence(request_for_app(app), db)
    assert error.value.status_code == 503
    assert "pasivo" in str(error.value.detail)


def test_api_health_endpoints_cover_ready_and_failure_responses(
    app, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = request_for_app(app)
    with Session(app.state.engine) as db:
        assert api.health_live() == {"status": "ok", "service": "pyfog"}
        ready = api.health_ready(request, db)
        assert ready.status_code == 200
        assert ready.body is not None
        assert api.health(request, db).status_code == 200
        assert api.health_metrics(db).status_code == 200

    monkeypatch.setattr(
        api,
        "operational_snapshot",
        lambda *_args: (_ for _ in ()).throw(OSError()),
    )
    with Session(app.state.engine) as db:
        unavailable = api.health_ready(request, db)
    assert unavailable.status_code == 503
    assert unavailable.body is not None

    monkeypatch.setattr(
        api,
        "operational_metrics",
        lambda *_args: (_ for _ in ()).throw(ValueError()),
    )
    with Session(app.state.engine) as db:
        unavailable_metrics = api.health_metrics(db)
    assert unavailable_metrics.status_code == 503


def seed_restore_task(app, host_id: str) -> tuple[str, str, dict[str, object]]:
    manifest = valid_manifest_v2()
    image_id = str(manifest["image_id"])
    token = "restore-task-token"
    with Session(app.state.engine) as db:
        user = db.scalar(select(User).where(User.username == "admin"))
        assert user is not None
        report = InventoryReport(
            host_id=host_id,
            report_id=str(uuid4()),
            collected_at=now(),
            source="api",
            fingerprint="a" * 64,
            data={},
        )
        image = Image(
            id=image_id,
            name="Imagen restaurable",
            status="ready",
            manifest_image_id=image_id,
            manifest_json=manifest,
            source_host_id=host_id,
            source_hostname="reference-linux",
            integrity_verified_at=now(),
        )
        db.add_all([report, image])
        db.flush()
        task = Task(
            operation="restore",
            status="assigned",
            requested_by=user.id,
            host_id=host_id,
            image_id=image_id,
            inventory_report_id=report.id,
            disk_selector={"stable_id": "path:/dev/vda"},
            idempotency_key=str(uuid4()),
            reservation_key=host_id,
            transfer_slot=1,
            phase="assigned",
            total_bytes=7,
            message="Restaurando.",
        )
        db.add(task)
        db.flush()
        attempt = TaskAttempt(
            task_id=task.id,
            attempt_number=1,
            agent_session_id=str(uuid4()),
            capability_hash=digest(token),
            lease_expires_at=now() + timedelta(minutes=5),
            phase="assigned",
            total_bytes=7,
        )
        db.add(attempt)
        db.commit()
        task_id = task.id

    store = app.state.artifact_store
    store.ensure_layout()
    published = store.published_directory(image_id)
    (published / "partitions").mkdir(parents=True)
    (published / "partitions/01-esp.img").write_bytes(b"esp")
    (published / "partitions/02-root.partclone").write_bytes(b"root")
    return task_id, token, manifest


def test_api_published_artifact_ranges_and_restore_result(client, app, host_id) -> None:
    task_id, token, _manifest = seed_restore_task(app, host_id)
    headers = {"Authorization": f"Bearer {token}"}
    base = f"/api/v1/tasks/{task_id}/artifacts/partitions/01-esp.img"
    full = client.get(base, headers=headers)
    assert full.status_code == 200
    assert full.content == b"esp"
    ranged = client.get(base + "?offset=1&length=2", headers=headers)
    assert ranged.status_code == 200
    assert ranged.content == b"sp"
    assert ranged.headers["content-range"] == "bytes 1-2/3"
    assert client.get(base + "?offset=bad&length=1", headers=headers).status_code == 422
    assert client.get(base + "?offset=0&length=99", headers=headers).status_code == 416
    unknown = client.get(
        f"/api/v1/tasks/{task_id}/artifacts/partitions/missing.img", headers=headers
    )
    assert unknown.status_code == 404

    result = client.post(
        f"/api/v1/tasks/{task_id}/result",
        json={"sequence": 1, "success": True},
        headers=headers,
    )
    assert result.status_code == 200
    assert result.json()["accepted"] is True
    with Session(app.state.engine) as db:
        task = db.get(Task, task_id)
        assert task is not None
        assert task.status == "succeeded"


def test_web_helpers_validate_published_images_and_deployment_targets(app, tmp_path) -> None:
    manifest_value = valid_manifest_v2()
    image_id = str(manifest_value["image_id"])
    image = Image(
        id=image_id,
        name="Publicada",
        status="ready",
        source_host_id=str(manifest_value["source"]["host_id"]),  # type: ignore[index]
        manifest_json=manifest_value,
        integrity_verified_at=now(),
    )
    published = app.state.artifact_store.published_directory(image_id)
    (published / "partitions").mkdir(parents=True)
    (published / "partitions/01-esp.img").write_bytes(b"esp")
    (published / "partitions/02-root.partclone").write_bytes(b"root")
    request = request_for_app(app)
    manifest = web.selectable_manifest(request, image)
    assert manifest is not None
    assert len(manifest.artifacts) == 2

    host = SimpleNamespace(id=str(manifest.source.host_id))
    disk = {"size_bytes": manifest.disk.size_bytes, "logical_sector_bytes": 512, "removable": False}
    assert web.deployment_validation(image, manifest, host, disk, operation="restore") == {}
    assert web.deployment_validation(
        image,
        manifest,
        SimpleNamespace(id="other-host"),
        disk,
        operation="clone",
        clone_hostname=manifest.source.hostname,
    )["hostname"]
    errors = web.deployment_validation(
        image,
        manifest,
        SimpleNamespace(id="other-host"),
        {"size_bytes": 1, "logical_sector_bytes": 4096, "removable": True},
        operation="clone",
        clone_hostname="bad..hostname",
    )
    assert {"disk_key", "hostname"}.issubset(errors)
    stale = Image(id=image_id, name="stale", status="draft")
    assert web.selectable_manifest(request, stale) is None


def test_web_task_payloads_and_management_views_cover_error_branches(
    admin, app, host_id, monkeypatch: pytest.MonkeyPatch
) -> None:
    task, attempt = standalone_task()
    task.created_at = now()
    task.updated_at = now()
    event = SimpleNamespace(
        id=4,
        task_id=task.id,
        attempt_id=attempt.id,
        sequence=3,
        event_type="progress",
        phase="uploading",
        bytes_processed=4,
        total_bytes=8,
        duration_ms=100,
        throughput_bytes_per_second=40.0,
        failure_code="",
        message="ok",
        created_at=now(),
    )
    assert web.task_status_payload(task)["status_label"]
    assert web.task_event_payload(event)["throughput_bytes_per_second"] == 40.0

    for path in ("/status", "/audit", "/users", "/tasks", f"/hosts/{host_id}/edit"):
        assert admin.get(path).status_code == 200
    assert admin.get(f"/hosts/{host_id}/import").status_code == 200
    assert admin.get(f"/hosts/{host_id}/capture").status_code == 409

    invalid_edit = admin.post(
        f"/hosts/{host_id}/edit",
        data={"csrf": csrf(admin), "name": "", "notes": ""},
    )
    assert invalid_edit.status_code == 422
    valid_edit = admin.post(
        f"/hosts/{host_id}/edit",
        data={"csrf": csrf(admin), "name": "Equipo actualizado", "notes": "notas"},
        follow_redirects=False,
    )
    assert valid_edit.status_code == 303

    missing_upload = admin.post(
        f"/hosts/{host_id}/import", data={"csrf": csrf(admin, f"/hosts/{host_id}/import")}
    )
    assert missing_upload.status_code == 422

    created = admin.post(
        "/images/new",
        data={"csrf": csrf(admin), "name": "Borrador para borrar", "description": ""},
        follow_redirects=False,
    )
    image_path = created.headers["location"]
    image_id = image_path.rsplit("/", maxsplit=1)[-1]
    wrong_confirmation = admin.post(
        f"/images/{image_id}/delete",
        data={"csrf": csrf(admin, image_path), "confirm_name": "wrong"},
    )
    assert wrong_confirmation.status_code == 422
    deleted = admin.post(
        f"/images/{image_id}/delete",
        data={"csrf": csrf(admin, image_path), "confirm_name": "Borrador para borrar"},
        follow_redirects=False,
    )
    assert deleted.status_code == 303

    monkeypatch.setattr(
        web,
        "operational_snapshot",
        lambda *_args: (_ for _ in ()).throw(OSError()),
    )
    assert admin.get("/status").status_code == 503
