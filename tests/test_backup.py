import json
import sqlite3
import uuid

import pytest
from sqlalchemy.orm import Session

from pyfog.database import Base, make_engine
from pyfog.image_manifest import ImageManifest
from pyfog.models import Host, Image, InventoryReport, LoginSession, Task, TaskAttempt, User, now
from scripts.backup_server import (
    BackupError,
    canonical_manifest_hash,
    create_backup,
    restore_backup,
    verify_backup,
)
from tests.test_image_manifest import valid_manifest

IMAGE_ID = "a5d5eb5c-1c44-4b02-bb1d-9c0ddf0a2b11"
HOST_ID = "ca66ef2d-4fd4-44ab-9b7a-f9e59f8af111"
REPORT_ID = "d1a9aa22-5709-48d6-9fef-0bc4c5a2c222"


def prepare_source(tmp_path):
    database = tmp_path / "source.db"
    image_store = tmp_path / "source-images"
    engine = make_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    manifest = valid_manifest()
    manifest_json = manifest
    with Session(engine) as db:
        user = User(username="operator", password_hash="not-used")
        host = Host(
            id=HOST_ID,
            name="Origen",
            mac_address="02:00:00:00:00:01",
        )
        report = InventoryReport(
            id=REPORT_ID,
            host_id=HOST_ID,
            report_id=REPORT_ID,
            collected_at=now(),
            source="api",
            fingerprint="f" * 64,
            data={},
        )
        image = Image(
            id=IMAGE_ID,
            name="Ubuntu referencia",
            status="ready",
            manifest_image_id=IMAGE_ID,
            manifest_json=manifest_json,
            manifest_sha256=canonical_manifest_hash(ImageManifest.model_validate(manifest)),
            source_host_id=HOST_ID,
            source_hostname="reference-linux",
            total_size_bytes=7,
            compatibility="Ubuntu · x86_64 · UEFI",
            integrity_verified_at=now(),
        )
        db.add_all([user, host, report, image])
        db.flush()
        db.commit()
    store = image_store / "published" / IMAGE_ID
    (store / "partitions").mkdir(parents=True)
    (store / "partitions/01-esp.img").write_bytes(b"esp")
    (store / "partitions/02-root.partclone").write_bytes(b"root")
    (store / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    (image_store / "staging").mkdir()
    engine.dispose()
    return database, image_store


def test_backup_verify_and_restore_preserve_verified_catalog(tmp_path):
    database, image_store = prepare_source(tmp_path)
    backup = tmp_path / "backup"

    create_backup(database, image_store, backup)
    manifest = verify_backup(backup)
    assert manifest["format"] == "pyfog-backup"
    assert (backup / "config-reference.json").is_file()

    restored_database = tmp_path / "restored.db"
    restored_store = tmp_path / "restored-images"
    restore_backup(backup, restored_database, restored_store)
    verify_backup(backup)
    assert (restored_store / "published" / IMAGE_ID / "manifest.json").is_file()
    with sqlite3.connect(restored_database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM images").fetchone() == (1,)


def test_backup_verification_rejects_tampered_artifact(tmp_path):
    database, image_store = prepare_source(tmp_path)
    backup = tmp_path / "backup"
    create_backup(database, image_store, backup)
    artifact = backup / "image-store/published" / IMAGE_ID / "partitions/02-root.partclone"
    artifact.write_bytes(b"tampered")

    with pytest.raises(BackupError, match="suma"):
        verify_backup(backup)


def test_restore_requires_empty_targets(tmp_path):
    database, image_store = prepare_source(tmp_path)
    backup = tmp_path / "backup"
    create_backup(database, image_store, backup)
    occupied_store = tmp_path / "occupied"
    occupied_store.mkdir()
    (occupied_store / "keep").write_text("data", encoding="utf-8")

    with pytest.raises(BackupError, match="no está vacío"):
        restore_backup(backup, tmp_path / "new.db", occupied_store)


def test_restore_invalidates_sessions_and_requires_reconciliation(tmp_path):
    database, image_store = prepare_source(tmp_path)
    engine = make_engine(f"sqlite:///{database}")
    with Session(engine) as db:
        user = db.query(User).filter_by(username="operator").one()
        task = Task(
            operation="restore",
            status="running",
            requested_by=user.id,
            host_id=HOST_ID,
            image_id=IMAGE_ID,
            inventory_report_id=REPORT_ID,
            disk_selector={},
            idempotency_key=str(uuid.uuid4()),
            reservation_key=HOST_ID,
            transfer_slot=1,
            phase="restoring",
            bytes_processed=1,
            total_bytes=7,
            message="Escritura en curso.",
        )
        db.add(task)
        db.flush()
        db.add(
            TaskAttempt(
                task_id=task.id,
                attempt_number=1,
                agent_session_id=str(uuid.uuid4()),
                capability_hash="a" * 64,
                lease_expires_at=now(),
                phase="restoring",
                bytes_processed=1,
                total_bytes=7,
            )
        )
        db.add(
            LoginSession(
                token_hash="b" * 64,
                user_id=user.id,
                expires_at=now(),
            )
        )
        db.commit()
    engine.dispose()

    backup = tmp_path / "backup"
    create_backup(database, image_store, backup)
    restored_database = tmp_path / "restored.db"
    restore_backup(backup, restored_database, tmp_path / "restored-images")

    with sqlite3.connect(restored_database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM login_sessions").fetchone() == (0,)
        assert connection.execute("SELECT status FROM tasks").fetchone() == (
            "intervention_required",
        )
        assert connection.execute("SELECT finished_at FROM task_attempts").fetchone()[0]
