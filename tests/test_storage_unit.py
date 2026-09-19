import hashlib
import shutil
import uuid
from pathlib import Path

import pytest

import pyfog.storage as storage_module
from pyfog.image_manifest import ImageManifest
from pyfog.storage import ArtifactStore, StorageError
from pyfog.transfer import build_transfer_manifest
from tests.test_image_manifest import valid_manifest


def make_store(tmp_path: Path, **kwargs: int) -> ArtifactStore:
    options = {"min_free_bytes": 0, **kwargs}
    return ArtifactStore(tmp_path / "images", **options)


def populate_manifest_staging(store: ArtifactStore, task_id: str, manifest: ImageManifest) -> None:
    payloads = {
        "partitions/01-esp.img": b"esp",
        "partitions/02-root.partclone": b"root",
    }
    store.task_directory(task_id, create=True)
    for artifact in manifest.artifacts:
        path = store.artifact_path(task_id, artifact.path, create_parent=True)
        path.write_bytes(payloads[artifact.path])


def test_storage_layout_capacity_and_status_are_fail_closed(tmp_path, monkeypatch):
    store = make_store(tmp_path, max_image_bytes=8, min_free_bytes=1)
    assert store.ensure_layout() == store.root
    assert (store.root / "staging").is_dir()
    assert (store.root / "published").is_dir()

    with pytest.raises(StorageError, match="supera el límite"):
        store.check_capacity(-1)
    with pytest.raises(StorageError, match="supera el límite"):
        store.check_capacity(9)

    usage = shutil.disk_usage(tmp_path)
    monkeypatch.setattr(storage_module.shutil, "disk_usage", lambda _path: usage._replace(free=0))
    with pytest.raises(StorageError, match="espacio libre"):
        store.check_capacity()

    task_id = str(uuid.uuid4())
    image_id = str(uuid.uuid4())
    store.task_directory(task_id, create=True)
    store.published_directory(image_id).mkdir()
    status = store.status()
    assert status["staging_images"] == 1
    assert status["published_images"] == 1

    bad_root = tmp_path / "bad-root"
    bad_root.write_text("not a directory", encoding="utf-8")
    with pytest.raises(StorageError, match="no es un directorio seguro"):
        ArtifactStore(bad_root).ensure_layout()


def test_storage_chunk_validation_replay_and_sequence_contract(tmp_path):
    store = make_store(tmp_path, max_image_bytes=8, max_chunk_bytes=4)
    task_id = str(uuid.uuid4())
    payload = b"1234"
    digest = hashlib.sha256(payload).hexdigest()

    with pytest.raises(StorageError, match="índice"):
        store.write_chunk(
            task_id, "artifact", index=-1, offset=0, total_size=4, payload=payload, sha256=digest
        )
    with pytest.raises(StorageError, match="desplazamiento"):
        store.write_chunk(
            task_id, "artifact", index=0, offset=-1, total_size=4, payload=payload, sha256=digest
        )
    with pytest.raises(StorageError, match="tamaño del fragmento"):
        store.write_chunk(
            task_id, "artifact", index=0, offset=0, total_size=4, payload=b"", sha256=digest
        )
    with pytest.raises(StorageError, match="SHA-256"):
        store.write_chunk(
            task_id, "artifact", index=0, offset=0, total_size=4, payload=payload, sha256="bad"
        )
    with pytest.raises(StorageError, match="suma"):
        store.write_chunk(
            task_id,
            "artifact",
            index=0,
            offset=0,
            total_size=4,
            payload=payload,
            sha256=hashlib.sha256(b"4321").hexdigest(),
        )
    with pytest.raises(StorageError, match="primer fragmento"):
        store.write_chunk(
            task_id,
            "artifact",
            index=0,
            offset=1,
            total_size=0,
            payload=payload,
            sha256=digest,
        )

    assert (
        store.write_chunk(
            task_id,
            "artifact",
            index=0,
            offset=0,
            total_size=4,
            payload=payload,
            sha256=digest,
        )
        is False
    )
    assert (
        store.write_chunk(
            task_id,
            "artifact",
            index=0,
            offset=0,
            total_size=4,
            payload=payload,
            sha256=digest,
        )
        is True
    )
    with pytest.raises(StorageError, match="no coincide"):
        store.write_chunk(
            task_id,
            "artifact",
            index=0,
            offset=0,
            total_size=4,
            payload=b"5678",
            sha256=hashlib.sha256(b"5678").hexdigest(),
        )
    with pytest.raises(StorageError, match="secuencial"):
        store.write_chunk(
            task_id,
            "artifact",
            index=1,
            offset=2,
            total_size=8,
            payload=b"a",
            sha256=hashlib.sha256(b"a").hexdigest(),
        )
    assert (
        store.write_chunk(
            task_id,
            "second",
            index=0,
            offset=0,
            total_size=0,
            payload=payload,
            sha256=digest,
        )
        is False
    )
    with pytest.raises(StorageError, match="límite de tamaño"):
        store.write_chunk(
            task_id,
            "third",
            index=0,
            offset=0,
            total_size=0,
            payload=b"1",
            sha256=hashlib.sha256(b"1").hexdigest(),
        )


def test_storage_verified_blocks_require_complete_transfer(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"abcdefgh")
    manifest = build_transfer_manifest(source, block_size=4)
    store = make_store(tmp_path, max_chunk_bytes=4)
    task_id = str(uuid.uuid4())
    relative = "parts/root.bin"
    store.task_directory(task_id, create=True)
    store.artifact_path(task_id, relative, create_parent=True)

    assert store.missing_blocks(task_id, relative, manifest) == [0, 1]
    first = source.read_bytes()[:4]
    second = source.read_bytes()[4:]
    assert (
        store.write_block(
            task_id, relative, manifest=manifest, block=manifest.blocks[0], payload=first
        )
        is False
    )
    assert store.verified_blocks(task_id, relative, manifest) == {0}
    assert store.missing_blocks(task_id, relative, manifest) == [1]
    with pytest.raises(StorageError, match="tamaño final"):
        store.verify_transfer(task_id, relative, manifest)
    assert (
        store.write_block(
            task_id, relative, manifest=manifest, block=manifest.blocks[1], payload=second
        )
        is False
    )
    assert (
        store.write_block(
            task_id, relative, manifest=manifest, block=manifest.blocks[1], payload=second
        )
        is True
    )
    store.verify_transfer(task_id, relative, manifest)
    assert store.missing_blocks(task_id, relative, manifest) == []

    with pytest.raises(StorageError, match="suma"):
        store.write_block(
            task_id, "parts/bad.bin", manifest=manifest, block=manifest.blocks[0], payload=b"xxxx"
        )


def test_storage_publish_delete_and_published_artifacts(tmp_path):
    store = make_store(tmp_path)
    task_id = str(uuid.uuid4())
    manifest = ImageManifest.model_validate(valid_manifest())
    image_id = str(manifest.image_id)
    populate_manifest_staging(store, task_id, manifest)

    published = store.verify_and_publish(task_id, image_id, manifest)

    assert published == store.published_directory(image_id)
    assert store.published_artifact(image_id, "partitions/01-esp.img").read_bytes() == b"esp"
    assert (published / "manifest.json").is_file()
    assert store.delete_published_image(image_id) is True
    assert store.delete_published_image(image_id) is False
    with pytest.raises(StorageError, match="no existe"):
        store.published_artifact(image_id, "manifest.json")


def test_storage_publish_rejects_identity_publishability_and_extra_files(tmp_path):
    store = make_store(tmp_path)
    task_id = str(uuid.uuid4())
    manifest = ImageManifest.model_validate(valid_manifest())
    image_id = str(manifest.image_id)
    store.task_directory(task_id, create=True)

    with pytest.raises(StorageError, match="otra imagen"):
        store.verify_and_publish(task_id, str(uuid.uuid4()), manifest)
    manifest.publishable = False
    with pytest.raises(StorageError, match="no autoriza"):
        store.verify_and_publish(task_id, image_id, manifest)
    manifest.publishable = True
    with pytest.raises(StorageError, match="espacio temporal"):
        store.verify_and_publish(str(uuid.uuid4()), image_id, manifest)

    populate_manifest_staging(store, task_id, manifest)
    (store.task_directory(task_id) / "undeclared.bin").write_bytes(b"unexpected")
    with pytest.raises(StorageError, match="no declarado"):
        store.verify_and_publish(task_id, image_id, manifest)


def test_storage_removes_staging_and_reports_invalid_paths(tmp_path):
    store = make_store(tmp_path)
    task_id = str(uuid.uuid4())
    store.task_directory(task_id, create=True)
    assert store.remove_task_staging(task_id) is True
    assert store.remove_task_staging(task_id) is False
    with pytest.raises(StorageError, match="identificador"):
        store.task_directory("not-a-uuid")

    with pytest.raises(StorageError, match="no existe"):
        store.published_artifact(str(uuid.uuid4()), "manifest.json")
