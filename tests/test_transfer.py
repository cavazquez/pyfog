import hashlib
import io
from pathlib import Path

import pytest

from pyfog import transfer
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
    with pytest.raises(ValueError, match="Faltan"):
        verify_complete_transfer(staging, manifest)
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


def test_transfer_manifest_rejects_invalid_layouts():
    digest = hashlib.sha256(b"data").hexdigest()
    block = TransferBlock(index=0, offset=0, size=4, sha256=digest)

    with pytest.raises(ValueError, match="comenzar en cero"):
        TransferManifest(
            size_bytes=4,
            block_size=4,
            blocks=[block.model_copy(update={"index": 1})],
        )
    with pytest.raises(ValueError, match="sin huecos"):
        TransferManifest(
            size_bytes=4,
            block_size=4,
            blocks=[block.model_copy(update={"offset": 1})],
        )
    with pytest.raises(ValueError, match="tamaño negociado"):
        TransferManifest(size_bytes=5, block_size=4, blocks=[block.model_copy(update={"size": 5})])
    with pytest.raises(ValueError, match="exactamente"):
        TransferManifest(size_bytes=5, block_size=5, blocks=[block])


def test_transfer_build_and_digest_helpers_reject_invalid_sources(tmp_path: Path):
    empty = tmp_path / "empty.bin"
    empty.touch()
    missing = tmp_path / "missing.bin"

    with pytest.raises(ValueError, match="bloque"):
        transfer._digest_stream(io.BytesIO(), 1)
    with pytest.raises(ValueError, match="bloque"):
        build_transfer_manifest(empty, block_size=0)
    with pytest.raises(ValueError, match="contenido"):
        build_transfer_manifest(empty)
    with pytest.raises(ValueError, match="leer el artefacto"):
        build_transfer_manifest(missing)


def test_transfer_verification_handles_missing_oversized_and_unreadable_staging(
    tmp_path: Path, monkeypatch
):
    source = tmp_path / "source.bin"
    source.write_bytes(b"abcdefgh")
    manifest = build_transfer_manifest(source, block_size=4)

    assert verified_block_indices(tmp_path / "missing.bin", manifest) == set()
    oversized = tmp_path / "oversized.bin"
    oversized.write_bytes(b"abcdefghij")
    assert verified_block_indices(oversized, manifest) == set()
    short = tmp_path / "short.bin"
    short.write_bytes(b"ab")
    assert verified_block_indices(short, manifest) == set()

    def fail_open(_path, *_args, **_kwargs):
        raise OSError

    monkeypatch.setattr(Path, "open", fail_open)
    with pytest.raises(ValueError, match="verificar el staging"):
        verified_block_indices(source, manifest)


def test_transfer_write_rejects_invalid_staging_and_surfaces_io_errors(tmp_path: Path, monkeypatch):
    source = tmp_path / "source.bin"
    source.write_bytes(b"abcdefgh")
    manifest = build_transfer_manifest(source, block_size=4)
    block = manifest.blocks[0]

    with pytest.raises(ValueError, match="tamaño del bloque"):
        write_verified_block(tmp_path / "staging.bin", manifest, block, b"x")
    staging_directory = tmp_path / "directory"
    staging_directory.mkdir()
    with pytest.raises(ValueError, match="archivo regular"):
        write_verified_block(staging_directory, manifest, block, b"abcd")

    def fail_fsync(_file_descriptor):
        raise OSError

    monkeypatch.setattr(transfer.os, "fsync", fail_fsync)
    with pytest.raises(ValueError, match="escribir el bloque"):
        write_verified_block(tmp_path / "io-error.bin", manifest, block, b"abcd")


def test_verify_complete_transfer_rejects_wrong_size(tmp_path: Path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"abcdefgh")
    manifest = build_transfer_manifest(source, block_size=4)
    wrong_size = tmp_path / "wrong-size.bin"
    wrong_size.write_bytes(b"")

    with pytest.raises(ValueError, match="tamaño final"):
        verify_complete_transfer(wrong_size, manifest)
