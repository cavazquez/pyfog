from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from pyfog.relay import RelayCache, relay_candidates, relay_descriptor
from pyfog.transfer import build_transfer_manifest


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
    assert cache.status()["bytes"] <= 5


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
