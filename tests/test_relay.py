import io
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest

from pyfog import relay
from pyfog.relay import RelayCache, relay_candidates, relay_descriptor
from pyfog.transfer import build_transfer_manifest

EXPECTED_ENTRY_COUNT = 2


def test_relay_cache_publishes_verified_ranges_and_resumes(tmp_path: Path):
    source = tmp_path / "artifact.bin"
    source.write_bytes(bytes(range(256)) * 1024)
    digest = sha256(source.read_bytes()).hexdigest()
    cache = RelayCache(tmp_path / "cache", max_bytes=source.stat().st_size * 2)
    published = cache.publish_file(source, digest=digest, size_bytes=source.stat().st_size)
    assert published.is_file()
    assert cache.read_range(digest, offset=17, length=100) == source.read_bytes()[17:117]

    descriptor = relay_descriptor(
        uuid4(), "relay-a", digest, source.stat().st_size, block_size=1024
    )
    assert descriptor.artifact_sha256 == digest
    assert relay_candidates(["relay-a", "relay-b"], seed_id="seed") == (
        "relay-a",
        "relay-b",
        "seed",
    )


def test_relay_block_staging_does_not_publish_incomplete_data(tmp_path: Path):
    source = tmp_path / "artifact.bin"
    source.write_bytes(b"0123456789" * 100)
    digest = sha256(source.read_bytes()).hexdigest()
    cache = RelayCache(tmp_path / "cache", max_bytes=10_000)
    manifest = build_transfer_manifest(source, block_size=100)
    missing = cache.begin_block_transfer(manifest, expected_sha256=digest)
    assert missing == list(range(len(manifest.blocks)))
    for block in manifest.blocks:
        payload = source.read_bytes()[block.offset : block.offset + block.size]
        cache.write_block(manifest, block, payload, expected_sha256=digest)
    assert not cache.has(digest)
    cache.complete_block_transfer(manifest, expected_sha256=digest)
    assert cache.has(digest, size_bytes=source.stat().st_size)
    assert cache.begin_block_transfer(manifest, expected_sha256=digest) == []


def test_relay_eviction_respects_active_lease(tmp_path: Path):
    cache = RelayCache(tmp_path / "cache", max_bytes=5)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    first_digest = sha256(first.read_bytes()).hexdigest()
    second_digest = sha256(second.read_bytes()).hexdigest()
    cache.publish_file(first, digest=first_digest, size_bytes=5)
    lease = cache.acquire(first_digest, seconds=60, current=datetime.now(UTC))
    cache.publish_file(second, digest=second_digest, size_bytes=6)
    assert cache.has(first_digest)
    assert not cache.has(second_digest)
    cache.release(lease)
    cache.evict()
    assert cache.status()["bytes"] <= cache.max_bytes


def test_relay_eviction_drops_expired_leases(tmp_path: Path):
    cache = RelayCache(tmp_path / "cache", max_bytes=5)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    first_digest = sha256(first.read_bytes()).hexdigest()
    second_digest = sha256(second.read_bytes()).hexdigest()
    cache.publish_file(first, digest=first_digest, size_bytes=5)
    cache.acquire(
        first_digest,
        seconds=1,
        current=datetime.now(UTC) - timedelta(seconds=2),
    )
    cache.publish_file(second, digest=second_digest, size_bytes=6)
    assert not cache.has(first_digest)
    assert not cache.has(second_digest)


def test_relay_rejects_invalid_requests_and_descriptors(tmp_path: Path):
    with pytest.raises(relay.RelayError, match="positivo"):
        RelayCache(tmp_path / "invalid", max_bytes=0)

    cache = RelayCache(tmp_path / "cache", max_bytes=100)
    source = tmp_path / "artifact.bin"
    source.write_bytes(b"data")
    digest = sha256(source.read_bytes()).hexdigest()

    with pytest.raises(relay.RelayError, match="digest"):
        cache.path_for("not-a-digest")
    with pytest.raises(relay.RelayError, match="archivo regular"):
        cache.publish_file(tmp_path, digest=digest, size_bytes=source.stat().st_size)
    with pytest.raises(relay.RelayError, match="tamaño o digest"):
        cache.publish_file(source, digest=digest, size_bytes=source.stat().st_size + 1)
    cache.publish_file(source, digest=digest, size_bytes=source.stat().st_size)
    lease = cache.acquire(digest, seconds=60)
    second_lease = cache.acquire(digest, seconds=60)
    assert lease.artifact_sha256 == digest
    cache.release(lease)
    cache.release(second_lease)
    with pytest.raises(relay.RelayError, match="no tiene"):
        cache.read_range("1" * relay.SHA256_HEX_LENGTH, offset=0, length=1)
    with pytest.raises(relay.RelayError, match="excede"):
        cache.read_range(digest, offset=3, length=2)

    with pytest.raises(relay.RelayError, match="rango"):
        cache.read_range(digest, offset=-1, length=1)
    with pytest.raises(relay.RelayError, match="rango"):
        cache.read_range(digest, offset=0, length=relay.MAX_RELAY_RANGE_BYTES + 1)
    with pytest.raises(relay.RelayError, match="ausente"):
        cache.acquire("0" * relay.SHA256_HEX_LENGTH)
    with pytest.raises(relay.RelayError, match="duración"):
        cache.acquire(digest, seconds=0)
    with pytest.raises(relay.RelayError, match="TTL"):
        relay_descriptor(uuid4(), "relay-a", digest, 4, block_size=1, ttl_seconds=0)
    with pytest.raises(ValueError, match="zona horaria"):
        relay.RelayDescriptor(
            session_id=uuid4(),
            relay_id="relay-a",
            artifact_sha256=digest,
            size_bytes=4,
            block_size=1,
            expires_at=datetime.now(UTC).replace(tzinfo=None),
        )
    with pytest.raises(ValueError, match="venció"):
        relay.RelayDescriptor(
            session_id=uuid4(),
            relay_id="relay-a",
            artifact_sha256=digest,
            size_bytes=4,
            block_size=1,
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    with pytest.raises(relay.RelayError, match="únicos"):
        relay_candidates(["relay-a", "relay-a"], seed_id="seed")
    cache.release(relay.RelayLease(uuid4(), "1" * relay.SHA256_HEX_LENGTH, datetime.now(UTC)))


def test_relay_reports_publish_and_read_io_failures(tmp_path: Path, monkeypatch):
    source = tmp_path / "artifact.bin"
    source.write_bytes(b"payload")
    digest = sha256(source.read_bytes()).hexdigest()
    cache = RelayCache(tmp_path / "cache", max_bytes=100)

    with pytest.raises(relay.RelayError, match="leer el artefacto"):
        relay._digest_file(tmp_path)
    published = cache.publish_file(source, digest=digest, size_bytes=source.stat().st_size)
    assert cache.publish_file(source, digest=digest, size_bytes=source.stat().st_size) == published

    def fail_copyfile(*_args, **_kwargs):
        raise OSError

    other = tmp_path / "other.bin"
    other.write_bytes(b"other")
    other_digest = sha256(other.read_bytes()).hexdigest()
    monkeypatch.setattr(relay.shutil, "copyfile", fail_copyfile)
    with pytest.raises(relay.RelayError, match="publicar el relay"):
        cache.publish_file(other, digest=other_digest, size_bytes=other.stat().st_size)

    def fail_open(_path, *_args, **_kwargs):
        raise OSError

    monkeypatch.setattr(Path, "open", fail_open)
    with pytest.raises(relay.RelayError, match="leer el relay"):
        cache.read_range(digest, offset=0, length=1)
    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: io.BytesIO(b""))
    with pytest.raises(relay.RelayError, match="incompleto"):
        cache.read_range(digest, offset=0, length=1)


def test_relay_reports_block_integrity_and_staging_failures(tmp_path: Path, monkeypatch):
    source = tmp_path / "artifact.bin"
    source.write_bytes(b"0123456789" * 10)
    digest = sha256(source.read_bytes()).hexdigest()
    manifest = build_transfer_manifest(source, block_size=10)
    cache = RelayCache(tmp_path / "cache", max_bytes=1000)
    block = manifest.blocks[0]

    with pytest.raises(relay.RelayError, match="suma"):
        cache.write_block(manifest, block, b"x" * block.size, expected_sha256=digest)
    with pytest.raises(relay.RelayError, match="incompleto"):
        cache.complete_block_transfer(manifest, expected_sha256=digest)

    wrong_digest = "0" * relay.SHA256_HEX_LENGTH
    payload = source.read_bytes()
    for item in manifest.blocks:
        cache.write_block(
            manifest,
            item,
            payload[item.offset : item.offset + item.size],
            expected_sha256=wrong_digest,
        )
    with pytest.raises(relay.RelayError, match="digest esperado"):
        cache.complete_block_transfer(manifest, expected_sha256=wrong_digest)

    complete_cache = RelayCache(tmp_path / "complete-cache", max_bytes=1000)
    for item in manifest.blocks:
        complete_cache.write_block(
            manifest,
            item,
            payload[item.offset : item.offset + item.size],
            expected_sha256=digest,
        )

    def fail_replace(*_args, **_kwargs):
        raise OSError

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(relay.RelayError, match="publicar el staging"):
        complete_cache.complete_block_transfer(manifest, expected_sha256=digest)


def test_relay_eviction_handles_unremovable_entries(tmp_path: Path, monkeypatch):
    cache = RelayCache(tmp_path / "cache", max_bytes=100)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    first_digest = sha256(first.read_bytes()).hexdigest()
    second_digest = sha256(second.read_bytes()).hexdigest()
    cache.publish_file(first, digest=first_digest, size_bytes=first.stat().st_size)
    cache.publish_file(second, digest=second_digest, size_bytes=second.stat().st_size)
    cache.max_bytes = 1

    def fail_unlink(*_args, **_kwargs):
        raise OSError

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    assert cache.evict() == []
    assert cache.status()["entries"] == EXPECTED_ENTRY_COUNT
