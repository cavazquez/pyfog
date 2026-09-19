"""Verified, resumable block transfers used by agents and distribution helpers.

The transfer format deliberately contains no credentials or transport-specific state.  A
receiver can therefore persist it next to a temporary artifact and safely reconstruct the
missing blocks after a process or network interruption.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import BinaryIO

from pydantic import Field, model_validator

from pyfog.schemas import Schema

DEFAULT_BLOCK_SIZE = 1024 * 1024
MAX_BLOCK_SIZE = 16 * 1024 * 1024
MAX_BLOCKS = 4_194_304


class TransferBlock(Schema):
    """One immutable block in an artifact transfer."""

    index: int = Field(ge=0, lt=MAX_BLOCKS, strict=True)
    offset: int = Field(ge=0, strict=True)
    size: int = Field(gt=0, le=MAX_BLOCK_SIZE, strict=True)
    sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$", strict=True)


class TransferManifest(Schema):
    """A complete, contiguous block index for one immutable artifact."""

    size_bytes: int = Field(gt=0, le=2**63 - 1, strict=True)
    block_size: int = Field(gt=0, le=MAX_BLOCK_SIZE, strict=True)
    blocks: list[TransferBlock] = Field(min_length=1, max_length=MAX_BLOCKS)

    @model_validator(mode="after")
    def validate_layout(self) -> TransferManifest:
        ordered = sorted(self.blocks, key=lambda block: block.index)
        if ordered != self.blocks:
            msg = "Los bloques deben estar ordenados por índice."
            raise ValueError(msg)
        expected_offset = 0
        for index, block in enumerate(self.blocks):
            if block.index != index:
                msg = "El índice de bloques debe comenzar en cero y ser contiguo."
                raise ValueError(msg)
            if block.offset != expected_offset:
                msg = "Los bloques deben cubrir el artefacto sin huecos."
                raise ValueError(msg)
            if block.size > self.block_size:
                msg = "Un bloque intermedio supera el tamaño negociado."
                raise ValueError(msg)
            expected_offset += block.size
        if expected_offset != self.size_bytes:
            msg = "Los bloques no cubren exactamente el tamaño del artefacto."
            raise ValueError(msg)
        return self


def _digest_stream(source: BinaryIO, size: int) -> str:
    digest = hashlib.sha256()
    remaining = size
    while remaining:
        chunk = source.read(min(1024 * 1024, remaining))
        if not chunk:
            msg = "El archivo terminó antes del tamaño del bloque."
            raise ValueError(msg)
        digest.update(chunk)
        remaining -= len(chunk)
    return digest.hexdigest()


def build_transfer_manifest(
    path: Path, *, block_size: int = DEFAULT_BLOCK_SIZE
) -> TransferManifest:
    """Build a deterministic block index without loading the artifact into memory."""

    if not 0 < block_size <= MAX_BLOCK_SIZE:
        msg = "El tamaño de bloque no está permitido."
        raise ValueError(msg)
    try:
        size = path.stat().st_size
        if size <= 0:
            msg = "El artefacto debe tener contenido."
            raise ValueError(msg)
        blocks: list[TransferBlock] = []
        with path.open("rb") as source:
            index = 0
            offset = 0
            while offset < size:
                current_size = min(block_size, size - offset)
                digest = _digest_stream(source, current_size)
                blocks.append(
                    TransferBlock(
                        index=index,
                        offset=offset,
                        size=current_size,
                        sha256=digest,
                    )
                )
                index += 1
                offset += current_size
    except OSError as error:
        msg = f"No se pudo leer el artefacto: {error}"
        raise ValueError(msg) from None
    return TransferManifest(size_bytes=size, block_size=block_size, blocks=blocks)


def verified_block_indices(path: Path, manifest: TransferManifest) -> set[int]:
    """Return only blocks whose bytes and hash are already valid in ``path``."""

    try:
        file_size = path.stat().st_size
    except OSError:
        return set()
    if file_size > manifest.size_bytes:
        return set()
    verified: set[int] = set()
    try:
        with path.open("rb") as source:
            for block in manifest.blocks:
                if block.offset + block.size > file_size:
                    continue
                source.seek(block.offset)
                if _digest_stream(source, block.size) == block.sha256:
                    verified.add(block.index)
    except OSError as error:
        msg = f"No se pudo verificar el staging: {error}"
        raise ValueError(msg) from None
    return verified


def missing_block_indices(path: Path, manifest: TransferManifest) -> list[int]:
    """Return missing or corrupt blocks in deterministic transfer order."""

    verified = verified_block_indices(path, manifest)
    return [block.index for block in manifest.blocks if block.index not in verified]


def write_verified_block(
    path: Path, manifest: TransferManifest, block: TransferBlock, payload: bytes
) -> bool:
    """Write one block atomically enough for retry semantics and verify its hash first.

    The function returns ``True`` for an idempotent replay.  It never trusts an existing block
    merely because its offset exists: both size and SHA-256 must match.
    """

    expected = manifest.blocks[block.index] if block.index < len(manifest.blocks) else None
    if expected != block:
        msg = "El bloque no pertenece al manifiesto de transferencia."
        raise ValueError(msg)
    if len(payload) != block.size:
        msg = "El tamaño del bloque no coincide con el manifiesto."
        raise ValueError(msg)
    if hashlib.sha256(payload).hexdigest() != block.sha256:
        msg = "La suma del bloque no coincide con su contenido."
        raise ValueError(msg)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    try:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            msg = "El staging de transferencia no es un archivo regular."
            raise ValueError(msg)
        if path.exists():
            with path.open("rb") as source:
                source.seek(block.offset)
                current = source.read(block.size)
            if len(current) == block.size and hashlib.sha256(current).hexdigest() == block.sha256:
                return True
                # A corrupt block is deliberately replaceable: the sender can retransmit it
                # after a cut or a failed integrity check.
        with path.open("r+b" if path.exists() else "w+b") as target:
            target.seek(block.offset)
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
    except OSError as error:
        msg = f"No se pudo escribir el bloque: {error}"
        raise ValueError(msg) from None
    return False


def verify_complete_transfer(path: Path, manifest: TransferManifest) -> None:
    """Raise unless every indexed block and the final file size are valid."""

    if path.stat().st_size != manifest.size_bytes:
        msg = "El tamaño final del artefacto no coincide."
        raise ValueError(msg)
    missing = missing_block_indices(path, manifest)
    if missing:
        raise ValueError("Faltan bloques o hay bloques corruptos: " + ", ".join(map(str, missing)))
