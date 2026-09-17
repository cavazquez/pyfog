import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from pyfog.audit import record_audit
from pyfog.config import Settings
from pyfog.database import Base, make_engine
from pyfog.health import operational_snapshot
from pyfog.models import AuditEvent, Host, User
from pyfog.storage import ArtifactStore


def test_published_image_delete_is_exact_and_idempotent(tmp_path):
    store = ArtifactStore(tmp_path / "images", min_free_bytes=0)
    store.ensure_layout()
    image_id = str(uuid.uuid4())
    other_id = str(uuid.uuid4())
    first = store.published_directory(image_id)
    second = store.published_directory(other_id)
    first.mkdir()
    second.mkdir()
    (first / "manifest.json").write_text("{}", encoding="utf-8")
    (second / "manifest.json").write_text("{}", encoding="utf-8")

    assert store.delete_published_image(image_id) is True
    assert not first.exists()
    assert second.is_dir()
    assert store.delete_published_image(image_id) is False


def test_staging_status_does_not_delete_active_or_orphaned_entries(tmp_path):
    store = ArtifactStore(tmp_path / "images", min_free_bytes=0)
    active_id = str(uuid.uuid4())
    orphan_id = str(uuid.uuid4())
    store.task_directory(active_id, create=True)
    store.task_directory(orphan_id, create=True)
    staging = store.root / "staging"
    (staging / "not-a-task").write_text("leave me", encoding="utf-8")

    status = store.staging_status([active_id])
    assert status == {"active": [active_id], "orphaned": [orphan_id], "unsafe": ["not-a-task"]}
    assert store.task_directory(active_id).is_dir()
    assert store.task_directory(orphan_id).is_dir()
    assert (staging / "not-a-task").is_file()


def test_audit_and_health_snapshot_are_persistent_and_non_sensitive(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'operations.db'}",
        secret_key="unit-test-operations-secret-at-least-32",  # pragma: allowlist secret
        image_store_path=tmp_path / "images",
        min_storage_free_bytes=0,
    )
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = User(username="operator", password_hash="not-used")
        host = Host(name="Agent", mac_address="02:00:00:00:00:01")
        db.add_all([user, host])
        db.flush()
        event = record_audit(
            db,
            actor_user_id=user.id,
            action="token.issue",
            resource_type="host",
            resource_id=host.id,
            detail="Token emitido; el valor no se registra.",
        )
        db.commit()
        assert event.id is not None
        saved = db.scalar(select(AuditEvent).where(AuditEvent.id == event.id))
        assert saved is not None
        assert saved.actor_user_id == user.id
        assert "token" not in saved.detail.lower().replace("token emitido", "")
        snapshot = operational_snapshot(
            db, ArtifactStore(settings.image_store_path, min_free_bytes=0)
        )

    assert snapshot["database"] == "ok"
    assert snapshot["coordinator"] == {"status": "available", "mode": "integrated"}
    assert "path" not in snapshot["storage"]
    engine.dispose()
