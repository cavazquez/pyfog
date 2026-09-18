import hashlib

import pytest

from pyfog.transfer import (
    TransferBlock,
    TransferManifest,
    build_transfer_manifest,
    missing_block_indices,
    verified_block_indices,
    verify_complete_transfer,
    write_verified_block,
)


def test_transfer_manifest_is_deterministic_and_resumable(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"abcdefghij")

    manifest = build_transfer_manifest(source, block_size=4)
    staging = tmp_path / "staging.bin"
    for block in (manifest.blocks[0], manifest.blocks[2]):
        payload = source.read_bytes()[block.offset : block.offset + block.size]
        assert not write_verified_block(staging, manifest, block, payload)

    assert verified_block_indices(staging, manifest) == {0, 2}
    assert missing_block_indices(staging, manifest) == [1]
    middle = manifest.blocks[1]
    payload = source.read_bytes()[middle.offset : middle.offset + middle.size]
    assert not write_verified_block(staging, manifest, middle, payload)
    assert write_verified_block(staging, manifest, middle, payload)
    verify_complete_transfer(staging, manifest)


def test_corrupt_or_foreign_blocks_are_rejected(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"abcdefgh")
    manifest = build_transfer_manifest(source, block_size=4)
    block = manifest.blocks[0]

    with pytest.raises(ValueError, match="suma"):
        write_verified_block(tmp_path / "staging.bin", manifest, block, b"xxxx")

    foreign = TransferBlock(
        index=0,
        offset=0,
        size=4,
        sha256=hashlib.sha256(b"wxyz").hexdigest(),
    )
    with pytest.raises(ValueError, match="no pertenece"):
        write_verified_block(tmp_path / "staging.bin", manifest, foreign, b"wxyz")


def test_manifest_rejects_gaps_and_reordered_blocks(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"abcdefgh")
    manifest = build_transfer_manifest(source, block_size=4)

    with pytest.raises(ValueError, match="ordenados"):
        TransferManifest(
            size_bytes=8,
            block_size=4,
            blocks=list(reversed(manifest.blocks)),
        )
