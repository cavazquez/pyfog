"""Versioned image manifest and artifact validation for the Linux MVP."""

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from pyfog.schemas import OperatingSystem, Schema

IMAGE_FORMAT = "pyfog-disk-image"
IMAGE_FORMAT_VERSION = 1
MAX_MANIFEST_BYTES = 1_048_576
MAX_ARTIFACT_BYTES = 2**63 - 1

NonNegativeInt = Annotated[int, Field(ge=0, le=MAX_ARTIFACT_BYTES, strict=True)]
SectorSize = Literal[512, 4096]
Sha256 = Annotated[str, Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$", strict=True)]
FilesystemUuid = Annotated[
    str, Field(min_length=4, max_length=64, pattern=r"^[0-9A-Fa-f-]+$", strict=True)
]


def validate_relative_path(value: str) -> str:
    """Accept only a relative POSIX artifact path inside the image directory."""

    if (
        not value
        or not value.isascii()
        or "\\" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError("La ruta del artefacto debe ser ASCII relativa y no contener controles.")
    path = PurePosixPath(value)
    parts = value.split("/")
    if (
        path.is_absolute()
        or path == PurePosixPath(".")
        or any(part in {"", ".", ".."} for part in parts)
        or value.endswith("/")
    ):
        raise ValueError("La ruta del artefacto no puede salir del directorio de la imagen.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", value):
        raise ValueError("La ruta del artefacto contiene caracteres no permitidos.")
    return value


def validate_optional_path(value: str | None) -> str | None:
    return validate_relative_path(value) if value is not None else None


class ImageArtifact(Schema):
    """One immutable partition payload referenced by the GPT layout."""

    path: str = Field(min_length=1, max_length=240)
    size_bytes: Annotated[int, Field(gt=0, le=MAX_ARTIFACT_BYTES, strict=True)]
    compression: Literal["none", "gzip", "zstd"]
    sha256: Sha256

    _path = field_validator("path")(validate_relative_path)


class ImageSource(Schema):
    host_id: UUID
    inventory_report_id: UUID
    hostname: str = Field(min_length=1, max_length=253)


class ImageFirmware(Schema):
    type: Literal["uefi"]
    secure_boot: Literal[False]


class ImageTool(Schema):
    name: Literal["partclone"]
    version: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
    commands: list[
        Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._+-]+$")]
    ] = Field(min_length=1, max_length=16)


class ImagePartition(Schema):
    number: Annotated[int, Field(ge=1, le=128, strict=True)]
    role: Literal["esp", "boot", "root", "swap"]
    start_sector: NonNegativeInt
    size_sectors: Annotated[int, Field(gt=0, le=MAX_ARTIFACT_BYTES, strict=True)]
    partition_guid: UUID
    filesystem: Literal["fat32", "ext4", "swap"]
    filesystem_uuid: FilesystemUuid
    mountpoint: Literal["/boot/efi", "/boot", "/", None]
    artifact: str | None = Field(default=None, max_length=240)

    _artifact_path = field_validator("artifact")(validate_optional_path)

    @model_validator(mode="after")
    def validate_role(self) -> "ImagePartition":
        expected = {
            "esp": ("fat32", "/boot/efi"),
            "boot": ("ext4", "/boot"),
            "root": ("ext4", "/"),
            "swap": ("swap", None),
        }[self.role]
        if self.filesystem != expected[0] or self.mountpoint != expected[1]:
            raise ValueError(f"La partición {self.role} no coincide con su sistema de archivos.")
        if self.role == "swap" and self.artifact is not None:
            raise ValueError("La partición swap no puede tener un artefacto de datos.")
        if self.role != "swap" and self.artifact is None:
            raise ValueError(f"La partición {self.role} requiere un artefacto de datos.")
        if self.role == "esp" and not re.fullmatch(r"[0-9A-Fa-f]{8}", self.filesystem_uuid):
            raise ValueError("La ESP debe conservar un UUID FAT32 de ocho dígitos hexadecimales.")
        if self.role in {"boot", "root", "swap"}:
            try:
                UUID(self.filesystem_uuid)
            except ValueError:
                raise ValueError(
                    f"La partición {self.role} requiere un UUID de filesystem válido."
                ) from None
        return self


class ImageDisk(Schema):
    size_bytes: Annotated[int, Field(gt=0, le=MAX_ARTIFACT_BYTES, strict=True)]
    logical_sector_bytes: SectorSize
    sector_count: Annotated[int, Field(gt=0, le=MAX_ARTIFACT_BYTES, strict=True)]
    gpt_disk_guid: UUID
    first_usable_sector: NonNegativeInt
    last_usable_sector: NonNegativeInt
    partitions: list[ImagePartition] = Field(min_length=2, max_length=4)

    @model_validator(mode="after")
    def validate_geometry(self) -> "ImageDisk":
        if self.size_bytes != self.logical_sector_bytes * self.sector_count:
            raise ValueError("La capacidad del disco no coincide con sus sectores lógicos.")
        if self.first_usable_sector >= self.last_usable_sector:
            raise ValueError("El rango GPT utilizable no es válido.")
        if self.last_usable_sector >= self.sector_count:
            raise ValueError("El último sector GPT queda fuera de la capacidad del disco.")
        if len({part.number for part in self.partitions}) != len(self.partitions):
            raise ValueError("El GPT contiene números de partición repetidos.")
        if len({part.partition_guid for part in self.partitions}) != len(self.partitions):
            raise ValueError("El GPT contiene GUIDs de partición repetidos.")
        if len({part.filesystem_uuid.lower() for part in self.partitions}) != len(self.partitions):
            raise ValueError("El GPT contiene UUIDs de filesystem repetidos.")
        roles = {part.role for part in self.partitions}
        if not roles.issuperset({"esp", "root"}):
            raise ValueError("El GPT debe incluir exactamente una ESP y una raíz.")
        for role in ("esp", "root"):
            if sum(part.role == role for part in self.partitions) != 1:
                raise ValueError(f"El GPT debe incluir una sola partición {role}.")
        for role in ("boot", "swap"):
            if sum(part.role == role for part in self.partitions) > 1:
                raise ValueError(f"El GPT no puede incluir más de una partición {role}.")
        ordered = sorted(self.partitions, key=lambda part: part.start_sector)
        previous_end = self.first_usable_sector - 1
        for part in ordered:
            end = part.start_sector + part.size_sectors - 1
            if part.start_sector < self.first_usable_sector or end > self.last_usable_sector:
                raise ValueError("Una partición queda fuera del rango GPT utilizable.")
            if part.start_sector <= previous_end:
                raise ValueError("Las particiones GPT se superponen.")
            previous_end = end
        return self


class ImageManifest(Schema):
    format: Literal["pyfog-disk-image"]
    format_version: Literal[1]
    image_id: UUID
    created_at: datetime
    checksum_algorithm: Literal["sha256"]
    source: ImageSource
    system: OperatingSystem
    architecture: Literal["x86_64"]
    firmware: ImageFirmware
    disk: ImageDisk
    tool: ImageTool
    artifacts: list[ImageArtifact] = Field(min_length=2, max_length=4)
    publishable: bool = Field(default=False, strict=True)

    @field_validator("format_version", mode="before")
    @classmethod
    def version_is_integer(cls, value: object) -> object:
        if type(value) is not int or value != IMAGE_FORMAT_VERSION:
            raise ValueError("format_version debe ser el entero 1.")
        return value

    @field_validator("created_at")
    @classmethod
    def valid_date(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("La fecha de creación debe incluir su zona horaria.")
        if value > datetime.now(UTC) + timedelta(minutes=10):
            raise ValueError("La fecha de creación está en el futuro.")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_artifact_references(self) -> "ImageManifest":
        paths = [artifact.path for artifact in self.artifacts]
        if len(set(paths)) != len(paths):
            raise ValueError("El manifiesto contiene rutas de artefacto repetidas.")
        partition_paths = [
            partition.artifact
            for partition in self.disk.partitions
            if partition.artifact is not None
        ]
        if len(set(partition_paths)) != len(partition_paths):
            raise ValueError("Dos particiones no pueden compartir un artefacto.")
        if set(paths) != set(partition_paths):
            raise ValueError("Cada artefacto debe corresponder a una partición de datos.")
        expected_commands = {
            "esp": "partclone.fat",
            "boot": "partclone.ext4",
            "root": "partclone.ext4",
        }
        commands = set(self.tool.commands)
        for partition in self.disk.partitions:
            if partition.role != "swap" and expected_commands[partition.role] not in commands:
                raise ValueError(f"Falta la herramienta para la partición {partition.role}.")
        if self.system.id.lower() != "ubuntu":
            raise ValueError("El manifiesto v1 sólo admite una imagen Ubuntu Linux.")
        return self


def parse_image_manifest(value: bytes | str | dict[str, object]) -> ImageManifest:
    """Parse and validate a manifest from JSON bytes, text, or an already decoded object."""

    if isinstance(value, (bytes, str)):
        if isinstance(value, bytes) and len(value) > MAX_MANIFEST_BYTES:
            raise ValueError("El manifiesto supera el límite permitido.")
        if isinstance(value, str) and len(value.encode("utf-8")) > MAX_MANIFEST_BYTES:
            raise ValueError("El manifiesto supera el límite permitido.")
        try:
            decoded = json.loads(value)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("El manifiesto no contiene JSON válido.") from None
    else:
        decoded = value
    if not isinstance(decoded, dict):
        raise ValueError("El manifiesto debe ser un objeto JSON.")
    try:
        return ImageManifest.model_validate(decoded)
    except ValueError as error:
        raise ValueError(f"Manifiesto inválido: {error}") from None


def load_image_manifest(path: Path) -> ImageManifest:
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise ValueError(f"No se pudo leer el manifiesto: {error}") from None
    return parse_image_manifest(payload)


def verify_image_artifacts(manifest: ImageManifest, root: Path) -> None:
    """Verify regular, contained files against the metadata in a validated manifest."""

    try:
        base = root.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ValueError(f"No se pudo abrir el directorio de artefactos: {error}") from None
    if not base.is_dir():
        raise ValueError("El directorio de artefactos no es un directorio.")
    for artifact in manifest.artifacts:
        path = base / PurePosixPath(artifact.path)
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            raise ValueError(f"Falta el artefacto {artifact.path}.") from None
        if resolved.parent != base and base not in resolved.parents:
            raise ValueError(f"La ruta del artefacto sale del directorio: {artifact.path}.")
        if path.is_symlink() or not resolved.is_file():
            raise ValueError(f"El artefacto no es un archivo regular: {artifact.path}.")
        if resolved.stat().st_size != artifact.size_bytes:
            raise ValueError(f"El tamaño de {artifact.path} no coincide con el manifiesto.")
        digest = hashlib.sha256()
        try:
            with resolved.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as error:
            raise ValueError(f"No se pudo leer el artefacto {artifact.path}: {error}") from None
        if digest.hexdigest() != artifact.sha256:
            raise ValueError(f"La suma SHA-256 de {artifact.path} no coincide.")


def validate_image_manifest(path: Path, artifacts_dir: Path | None = None) -> ImageManifest:
    manifest = load_image_manifest(path)
    if artifacts_dir is not None:
        verify_image_artifacts(manifest, artifacts_dir)
    return manifest
