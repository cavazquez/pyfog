import copy
import hashlib
import json
import sqlite3
import urllib.error
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from pyfog.models import Image, Task, TaskEvent
from scripts import run_image_task as task_agent
from scripts.backup_server import create_backup, restore_backup, sqlite_path, verify_backup
from tests.conftest import csrf
from tests.test_image_manifest import valid_manifest
from tests.test_restore import selected_disk, target_selector
from tests.test_tasks import CAPABILITIES, claim, enqueue_capture


def test_source_restore_contract_accepts_larger_target_and_uefi_manifest() -> None:
    manifest = valid_manifest()
    assert task_agent.validate_restore_manifest(manifest, str(manifest["image_id"])) is manifest
    selected = selected_disk(size=2 * 1_073_741_824)
    selector = target_selector(size=2 * 1_073_741_824)
    task_agent.validate_restore_target(selected, selector, manifest)


def test_negative_contract_rejects_corrupt_manifest_and_incompatible_target() -> None:
    manifest = copy.deepcopy(valid_manifest())
    manifest["publishable"] = False
    with pytest.raises(ValueError, match="autoriza"):
        task_agent.validate_restore_manifest(manifest, str(manifest["image_id"]))

    with pytest.raises(ValueError, match="menor"):
        task_agent.validate_restore_target(
            selected_disk(size=512 * 1024**2),
            target_selector(size=512 * 1024**2),
            valid_manifest(),
        )


def test_clone_contract_keeps_identity_changes_scoped_to_target(tmp_path: Path) -> None:
    root = tmp_path / "target-root"
    for relative in (
        "etc/ssh",
        "etc/netplan",
        "etc/systemd/network",
        "etc/NetworkManager/system-connections",
        "etc/udev/rules.d",
        "var/lib/dbus",
        "var/lib/systemd",
    ):
        (root / relative).mkdir(parents=True)
    (root / "etc/machine-id").write_text("source-id\n", encoding="utf-8")
    (root / "etc/ssh/ssh_host_rsa_key").write_text("source-key", encoding="utf-8")
    (root / "var/lib/dbus/machine-id").write_text("source-db-id\n", encoding="utf-8")
    (root / "var/lib/systemd/random-seed").write_bytes(b"source-seed")
    (root / "etc/netplan/50-source.yaml").write_text("addresses: []", encoding="utf-8")

    task_agent.customize_clone_identity(root, "target-b.example.test")

    assert (root / "etc/hostname").read_text(encoding="utf-8") == "target-b.example.test\n"
    assert (root / "etc/machine-id").read_text(encoding="utf-8") == ""
    assert not list((root / "etc/ssh").glob("ssh_host_*"))
    assert not (root / "var/lib/systemd/random-seed").exists()
    assert (root / "etc/netplan/99-pyfog-dhcp.yaml").is_file()


def test_network_loss_does_not_allow_a_lease_heartbeat_to_claim_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise urllib.error.URLError("coordinador no disponible")

    monkeypatch.setattr(task_agent, "json_request", unavailable)
    heartbeat = task_agent.LeaseHeartbeat(
        "http://127.0.0.1:1", "task-id", "test-token", None, interval=0
    )
    heartbeat._run()
    with pytest.raises(ValueError, match="renovar la lease"):
        heartbeat.check()


def capture_and_publish(admin, app, host_id, inventory) -> tuple[str, str]:
    """Drive the HTTP capture protocol far enough to produce a verified publication."""

    task_id, image_id, report_id, host_token = enqueue_capture(admin, app, host_id, inventory)
    assignment = claim(admin, host_id, host_token).json()
    assert assignment["task_id"] == task_id
    task_headers = {"Authorization": f"Bearer {assignment['task_token']}"}
    artifacts = {"partitions/01-esp.img": b"esp", "partitions/02-root.partclone": b"root"}
    for index, (path, payload) in enumerate(artifacts.items()):
        uploaded = admin.post(
            f"/api/v1/tasks/{task_id}/artifacts/{path}",
            content=payload,
            headers={
                **task_headers,
                "Content-Type": "application/octet-stream",
                "X-PyFog-Chunk-Index": str(index),
                "X-PyFog-Chunk-Offset": "0",
                "X-PyFog-Artifact-Size": str(len(payload)),
                "X-PyFog-Chunk-SHA256": hashlib.sha256(payload).hexdigest(),
            },
        )
        assert uploaded.status_code == 200, uploaded.text

    manifest = copy.deepcopy(valid_manifest())
    manifest["image_id"] = image_id
    manifest["source"]["host_id"] = host_id  # type: ignore[index]
    manifest["source"]["inventory_report_id"] = report_id  # type: ignore[index]
    result = admin.post(
        f"/api/v1/tasks/{task_id}/result",
        json={"sequence": 1, "success": True, "manifest": manifest},
        headers=task_headers,
    )
    assert result.status_code == 200, result.text
    assert result.json() == {"task_id": task_id, "status": "succeeded", "accepted": True}
    return image_id, host_token


def test_capture_publication_is_restorable_and_survives_backup_round_trip(
    admin, app, host_id, inventory, tmp_path
) -> None:
    image_id, host_token = capture_and_publish(admin, app, host_id, inventory)

    restore_path = f"/hosts/{host_id}/restore"
    assert admin.get(restore_path).status_code == 200
    requested = admin.post(
        restore_path,
        data={
            "csrf": csrf(admin, restore_path),
            "image_id": image_id,
            "disk_key": "wwn:0xTESTWWN",
            "confirm": "1",
            "idempotency_key": "restore-e2e-contract-001",
        },
        follow_redirects=False,
    )
    assert requested.status_code == 303, requested.text
    restore_task_id = requested.headers["location"].rsplit("/", maxsplit=1)[-1]

    assignment = claim(
        admin,
        host_id,
        host_token,
        capabilities=[*CAPABILITIES, "restore"],
    )
    assert assignment.status_code == 200, assignment.text
    payload = assignment.json()
    assert payload["task_id"] == restore_task_id
    assert payload["operation"] == "restore"
    assert payload["manifest"]["image_id"] == image_id
    task_headers = {"Authorization": f"Bearer {payload['task_token']}"}

    downloaded = admin.get(
        f"{payload['artifact_base']}/partitions/02-root.partclone?offset=1&length=2",
        headers=task_headers,
    )
    assert downloaded.status_code == 200
    assert downloaded.content == b"oo"
    assert downloaded.headers["content-range"] == "bytes 1-2/4"

    finished = admin.post(
        f"/api/v1/tasks/{restore_task_id}/result",
        json={"sequence": 1, "success": True},
        headers=task_headers,
    )
    assert finished.status_code == 200, finished.text
    assert finished.json() == {
        "task_id": restore_task_id,
        "status": "succeeded",
        "accepted": True,
    }

    with Session(app.state.engine) as db:
        image = db.get(Image, image_id)
        restore_task = db.get(Task, restore_task_id)
        assert image is not None
        assert image.status == "ready"
        assert restore_task is not None
        assert restore_task.status == "succeeded"
        events = db.scalars(
            select(TaskEvent).where(TaskEvent.task_id == restore_task_id).order_by(TaskEvent.id)
        ).all()
        assert [event.event_type for event in events] == [
            "created",
            "reserved",
            "assigned",
            "progress",
            "completed",
        ]

    backup = tmp_path / "backup"
    create_backup(
        sqlite_path(app.state.settings.database_url),
        app.state.settings.image_store_path,
        backup,
    )
    assert verify_backup(backup)["format"] == "pyfog-backup"
    restored_database = tmp_path / "restored.db"
    restored_store = tmp_path / "restored-images"
    restore_backup(backup, restored_database, restored_store)

    with sqlite3.connect(restored_database) as connection:
        row = connection.execute(
            "SELECT status, manifest_json FROM images WHERE id = ?", (image_id,)
        ).fetchone()
    assert row is not None
    assert row[0] == "ready"
    assert json.loads(row[1])["image_id"] == image_id
    assert (
        restored_store / "published" / image_id / "partitions/02-root.partclone"
    ).read_bytes() == b"root"
