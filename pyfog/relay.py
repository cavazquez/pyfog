"""Content-addressed unicast relay cache for resumable image artifacts."""

from __future__ import annotations

import hashlib
import shutil
import threading
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import Field, field_validator

from pyfog.schemas import Schema
from pyfog.transfer import (
    TransferBlock,
    TransferManifest,
    missing_block_indices,
    verify_complete_transfer,
    write_verified_block,
)

MAX_RELAY_LEASE_SECONDS = 3600
MAX_RELAY_RANGE_BYTES = 16 * 1024 * 1024
SHA256_HEX_LENGTH = 64
SHA256_PATH_SUFFIX_LENGTH = 62


class RelayError(RuntimeError):
    """A relay request cannot be served safely."""


class RelayDescriptor(Schema):
    """Secret-free descriptor distributed by the HTTPS control plane."""

    api_version: int = Field(default=1, ge=1, le=1, strict=True)
    session_id: UUID
    relay_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    artifact_sha256: str = Field(
        min_length=SHA256_HEX_LENGTH,
        max_length=SHA256_HEX_LENGTH,
        pattern=r"^[0-9a-f]{64}$",
    )
    size_bytes: int = Field(gt=0, le=2**63 - 1, strict=True)
    block_size: int = Field(gt=0, le=MAX_RELAY_RANGE_BYTES, strict=True)
    expires_at: datetime
    max_peers: int = Field(default=8, ge=1, le=128, strict=True)

    @field_validator("expires_at")
    @classmethod
    def valid_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("La expiración del relay debe tener zona horaria.")
        if value <= datetime.now(UTC):
            raise ValueError("El descriptor del relay ya venció.")
        return value.astimezone(UTC)


@dataclass(frozen=True)
class RelayLease:
    lease_id: UUID
    artifact_sha256: str
    expires_at: datetime


def _valid_digest(value: str) -> str:
    if len(value) != SHA256_HEX_LENGTH or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise RelayError("El digest del relay no es un SHA-256 lowercase.")
    return value


def _digest_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
    except OSError as error:
        raise RelayError(f"No se pudo leer el artefacto del relay: {error}") from None
    return size, digest.hexdigest()


class RelayCache:
    """Atomic, bounded, content-addressed cache used by unicast relays."""

    def __init__(self, root: Path, *, max_bytes: int) -> None:
        if max_bytes <= 0:
            raise RelayError("El tamaño máximo de cache debe ser positivo.")
        self.root = root
        self.max_bytes = max_bytes
        self._leases: dict[str, dict[UUID, datetime]] = {}
        self._lock = threading.RLock()
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.staging = self.root / ".staging"
        self.staging.mkdir(parents=True, mode=0o700, exist_ok=True)

    def path_for(self, digest: str) -> Path:
        digest = _valid_digest(digest)
        return self.root / digest[:2] / digest[2:]

    def has(self, digest: str, *, size_bytes: int | None = None) -> bool:
        path = self.path_for(digest)
        if path.is_symlink() or not path.is_file():
            return False
        return size_bytes is None or path.stat().st_size == size_bytes

    def publish_file(self, source: Path, *, digest: str, size_bytes: int) -> Path:
        """Copy and verify one complete artifact before atomically exposing it."""

        digest = _valid_digest(digest)
        if source.is_symlink() or not source.is_file():
            raise RelayError("El origen del relay debe ser un archivo regular.")
        actual_size, actual_digest = _digest_file(source)
        if actual_size != size_bytes or actual_digest != digest:
            raise RelayError("El artefacto del relay no coincide con su tamaño o digest.")
        destination = self.path_for(digest)
        with self._lock:
            if self.has(digest, size_bytes=size_bytes):
                return destination
            destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            temporary = self.staging / f"{digest}.{uuid4().hex}.part"
            try:
                shutil.copyfile(source, temporary)
                temporary.replace(destination)
            except OSError as error:
                raise RelayError(f"No se pudo publicar el relay: {error}") from None
            finally:
                with suppress(OSError):
                    temporary.unlink()
            self.evict()
        return destination

    def begin_block_transfer(
        self, manifest: TransferManifest, *, expected_sha256: str
    ) -> list[int]:
        """Return missing blocks for a resumable relay artifact."""

        digest = _valid_digest(expected_sha256)
        if self.has(digest, size_bytes=manifest.size_bytes):
            return []
        staging = self.staging / f"{digest}.part"
        return missing_block_indices(staging, manifest)

    def write_block(
        self,
        manifest: TransferManifest,
        block: TransferBlock,
        payload: bytes,
        *,
        expected_sha256: str,
    ) -> bool:
        digest = _valid_digest(expected_sha256)
        staging = self.staging / f"{digest}.part"
        try:
            return write_verified_block(staging, manifest, block, payload)
        except ValueError as error:
            raise RelayError(str(error)) from None

    def complete_block_transfer(
        self,
        manifest: TransferManifest,
        *,
        expected_sha256: str,
    ) -> Path:
        digest = _valid_digest(expected_sha256)
        staging = self.staging / f"{digest}.part"
        try:
            verify_complete_transfer(staging, manifest)
        except (OSError, ValueError) as error:
            raise RelayError(f"El staging del relay está incompleto: {error}") from None
        actual_size, actual_digest = _digest_file(staging)
        if actual_size != manifest.size_bytes or actual_digest != digest:
            raise RelayError("El staging del relay no coincide con el digest esperado.")
        destination = self.path_for(digest)
        with self._lock:
            destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            try:
                staging.replace(destination)
            except OSError as error:
                raise RelayError(f"No se pudo publicar el staging del relay: {error}") from None
            self.evict()
        return destination

    def read_range(self, digest: str, *, offset: int, length: int) -> bytes:
        if offset < 0 or length <= 0 or length > MAX_RELAY_RANGE_BYTES:
            raise RelayError("El rango solicitado al relay no es válido.")
        path = self.path_for(digest)
        if path.is_symlink() or not path.is_file():
            raise RelayError("El relay no tiene el artefacto solicitado.")
        try:
            if offset + length > path.stat().st_size:
                raise RelayError("El rango solicitado excede el artefacto.")
            with path.open("rb") as source:
                source.seek(offset)
                value = source.read(length)
        except OSError as error:
            raise RelayError(f"No se pudo leer el relay: {error}") from None
        if len(value) != length:
            raise RelayError("El relay devolvió un rango incompleto.")
        return value

    def acquire(
        self, digest: str, *, seconds: int = 60, current: datetime | None = None
    ) -> RelayLease:
        digest = _valid_digest(digest)
        if not self.has(digest):
            raise RelayError("No se puede crear una lease para un artefacto ausente.")
        if seconds <= 0 or seconds > MAX_RELAY_LEASE_SECONDS:
            raise RelayError("La duración de la lease del relay no es válida.")
        current = current or datetime.now(UTC)
        current = current.replace(tzinfo=UTC) if current.tzinfo is None else current.astimezone(UTC)
        lease = RelayLease(uuid4(), digest, current + timedelta(seconds=seconds))
        with self._lock:
            self._purge_expired(current)
            self._leases.setdefault(digest, {})[lease.lease_id] = lease.expires_at
        return lease

    def release(self, lease: RelayLease) -> None:
        with self._lock:
            leases = self._leases.get(lease.artifact_sha256)
            if leases is None:
                return
            leases.pop(lease.lease_id, None)
            if not leases:
                self._leases.pop(lease.artifact_sha256, None)

    def _purge_expired(self, current: datetime) -> None:
        for digest, leases in list(self._leases.items()):
            for lease_id, expires_at in list(leases.items()):
                if expires_at <= current:
                    leases.pop(lease_id, None)
            if not leases:
                self._leases.pop(digest, None)

    def evict(self) -> list[str]:
        """Evict oldest complete entries until the bounded cache fits."""

        with self._lock:
            self._purge_expired(datetime.now(UTC))
            entries = [
                path
                for path in self.root.glob("??/*")
                if (
                    path.is_file()
                    and not path.is_symlink()
                    and len(path.name) == SHA256_PATH_SUFFIX_LENGTH
                )
            ]
            total = sum(path.stat().st_size for path in entries)
            removed: list[str] = []
            for path in sorted(entries, key=lambda item: item.stat().st_atime):
                if total <= self.max_bytes:
                    break
                digest = path.parent.name + path.name
                if self._leases.get(digest):
                    continue
                size = path.stat().st_size
                try:
                    path.unlink()
                except OSError:
                    continue
                total -= size
                removed.append(digest)
            return removed

    def status(self) -> dict[str, int]:
        entries = [
            path
            for path in self.root.glob("??/*")
            if (
                path.is_file()
                and not path.is_symlink()
                and len(path.name) == SHA256_PATH_SUFFIX_LENGTH
            )
        ]
        return {"entries": len(entries), "bytes": sum(path.stat().st_size for path in entries)}


def relay_descriptor(
    session_id: UUID,
    relay_id: str,
    digest: str,
    size_bytes: int,
    *,
    block_size: int,
    ttl_seconds: int = 300,
    max_peers: int = 8,
) -> RelayDescriptor:
    """Build the control-plane descriptor without embedding a bearer token."""

    if ttl_seconds <= 0 or ttl_seconds > MAX_RELAY_LEASE_SECONDS:
        raise RelayError("El TTL del descriptor del relay no es válido.")
    return RelayDescriptor(
        session_id=session_id,
        relay_id=relay_id,
        artifact_sha256=_valid_digest(digest),
        size_bytes=size_bytes,
        block_size=block_size,
        expires_at=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
        max_peers=max_peers,
    )


def relay_candidates(relay_ids: list[str], *, seed_id: str) -> tuple[str, ...]:
    """Return deterministic unicast candidates with the coordinator seed as final fallback."""

    values = [item for item in [*relay_ids, seed_id] if item]
    if len(set(values)) != len(values):
        raise RelayError("Los candidatos del relay no son únicos.")
    return tuple(values)
