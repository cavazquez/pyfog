"""Versioned image manifest, capabilities, and artifact validation."""

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from pyfog.schemas import OperatingSystem, Schema
from pyfog.transfer import TransferManifest, verify_complete_transfer

IMAGE_FORMAT = "pyfog-disk-image"
LEGACY_IMAGE_FORMAT_VERSION = 1
IMAGE_FORMAT_VERSION = 2
SUPPORTED_IMAGE_FORMAT_VERSIONS = (LEGACY_IMAGE_FORMAT_VERSION, IMAGE_FORMAT_VERSION)
MAX_MANIFEST_BYTES = 1_048_576
MAX_ARTIFACT_BYTES = 2**63 - 1

NonNegativeInt = Annotated[int, Field(ge=0, le=MAX_ARTIFACT_BYTES, strict=True)]
SectorSize = Literal[512, 4096]
Sha256 = Annotated[str, Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$", strict=True)]
FilesystemUuid = Annotated[
    str, Field(min_length=4, max_length=64, pattern=r"^[0-9A-Fa-f-]+$", strict=True)
]
CapabilityName = Annotated[
    str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._+-]+$", strict=True)
]
DiskIdentity = Annotated[
    str,
    Field(
        max_length=256,
        pattern=(
            r"^(?:|wwn:[A-Za-z0-9._:-]+|serial:[A-Za-z0-9._:+-]+|"
            r"path:/dev/[A-Za-z0-9._+-]+)$"
        ),
        strict=True,
    ),
]

FilesystemName = Literal["fat32", "ext4", "xfs", "btrfs", "swap"]


class ImageSubvolume(Schema):
    """A declared Btrfs subvolume; snapshots are deliberately not represented."""

    path: str = Field(min_length=1, max_length=255, pattern=r"^/?[A-Za-z0-9._/@+-]+$")
    mountpoint: Literal["/", "/home", "/var", "/srv", None] = None
    readonly: bool = Field(default=False, strict=True)


class ImageEncryption(Schema):
    """Non-secret LUKS metadata.  Keys and keyslots are never part of this model."""

    type: Literal["luks2"]
    uuid: FilesystemUuid
    cipher: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._+-]+$")
    sector_size: SectorSize


class ImageVolume(Schema):
    """The subset of volume management that a manifest may request."""

    type: Literal["lvm-linear"]
    pv_uuid: FilesystemUuid
    vg_name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._+-]+$")
    vg_uuid: FilesystemUuid
    lv_name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._+-]+$")
    lv_uuid: FilesystemUuid
    size_bytes: Annotated[int, Field(gt=0, le=MAX_ARTIFACT_BYTES, strict=True)]


class ImageRaidArray(Schema):
    """A metadata-only mdadm RAID1 declaration."""

    level: Literal[1]
    uuid: FilesystemUuid
    metadata: Literal["1.0", "1.1", "1.2"]
    member_ids: list[DiskIdentity] = Field(min_length=2, max_length=128)
    size_bytes: Annotated[int, Field(gt=0, le=MAX_ARTIFACT_BYTES, strict=True)]

    @model_validator(mode="after")
    def validate_member_identities(self) -> "ImageRaidArray":
        if any(not member or member.startswith("path:") for member in self.member_ids):
            raise ValueError("RAID1 debe usar identidades estables, nunca el orden /dev.")
        if len(set(self.member_ids)) != len(self.member_ids):
            raise ValueError("RAID1 no puede repetir identidades de miembros.")
        return self


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
    """One immutable payload referenced by a disk layout."""

    path: str = Field(min_length=1, max_length=240)
    size_bytes: Annotated[int, Field(gt=0, le=MAX_ARTIFACT_BYTES, strict=True)]
    compression: Literal["none", "gzip", "zstd"]
    sha256: Sha256
    blocks: TransferManifest | None = None

    _path = field_validator("path")(validate_relative_path)

    @model_validator(mode="after")
    def validate_blocks(self) -> "ImageArtifact":
        if self.blocks is not None and self.blocks.size_bytes != self.size_bytes:
            raise ValueError("El índice de bloques no coincide con el tamaño del artefacto.")
        return self


class ImageSource(Schema):
    host_id: UUID
    inventory_report_id: UUID
    hostname: str = Field(min_length=1, max_length=253)


class ImageFirmware(Schema):
    type: CapabilityName
    secure_boot: bool = Field(strict=True)


class ImageCapabilities(Schema):
    """Explicit compatibility claims made by a v2 image."""

    firmware: ImageFirmware
    partition_table: CapabilityName
    disks: Annotated[int, Field(ge=1, le=128, strict=True)]
    filesystems: list[CapabilityName] = Field(min_length=1, max_length=16)
    encryption: CapabilityName
    volumes: CapabilityName

    @field_validator("filesystems")
    @classmethod
    def unique_filesystems(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("Las capacidades no pueden repetir sistemas de archivos.")
        return sorted(value)

    @model_validator(mode="after")
    def validate_profile_names(self) -> "ImageCapabilities":
        if self.encryption not in {"none", "luks2"}:
            raise ValueError("El perfil de cifrado no es válido.")
        if self.volumes not in {"partitions", "lvm", "lvm-linear", "raid1"}:
            raise ValueError("El perfil de volúmenes no es válido.")
        return self


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
    partition_guid: UUID | None = None
    filesystem: FilesystemName
    filesystem_uuid: FilesystemUuid
    mountpoint: Literal["/boot/efi", "/boot", "/", None]
    artifact: str | None = Field(default=None, max_length=240)
    subvolume: str | None = Field(default=None, max_length=255)
    mount_options: list[str] = Field(default_factory=list, max_length=16)

    _artifact_path = field_validator("artifact")(validate_optional_path)

    @model_validator(mode="after")
    def validate_role(self) -> "ImagePartition":
        expected_mountpoint = {"esp": "/boot/efi", "boot": "/boot", "root": "/", "swap": None}[
            self.role
        ]
        expected_filesystems = {
            "esp": {"fat32"},
            "boot": {"ext4", "xfs"},
            "root": {"ext4", "xfs", "btrfs"},
            "swap": {"swap"},
        }[self.role]
        if self.filesystem not in expected_filesystems or self.mountpoint != expected_mountpoint:
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
        if self.filesystem == "btrfs" and not self.subvolume:
            raise ValueError("Una raíz Btrfs debe declarar su subvolumen.")
        if self.filesystem != "btrfs" and self.subvolume is not None:
            raise ValueError("Sólo Btrfs puede declarar un subvolumen.")
        if self.filesystem != "btrfs" and self.mount_options:
            raise ValueError("Sólo Btrfs puede declarar opciones de montaje.")
        if any(not re.fullmatch(r"[A-Za-z0-9._+=:@/-]+", option) for option in self.mount_options):
            raise ValueError("Las opciones de montaje contienen caracteres no permitidos.")
        return self


class ImageDisk(Schema):
    disk_id: DiskIdentity = ""
    source_disk_id: DiskIdentity = ""
    size_bytes: Annotated[int, Field(gt=0, le=MAX_ARTIFACT_BYTES, strict=True)]
    logical_sector_bytes: SectorSize
    sector_count: Annotated[int, Field(gt=0, le=MAX_ARTIFACT_BYTES, strict=True)]
    partitions: list[ImagePartition] = Field(min_length=1, max_length=4)
    gpt_disk_guid: UUID | None = None
    first_usable_sector: NonNegativeInt | None = None
    last_usable_sector: NonNegativeInt | None = None
    mbr_disk_signature: (
        Annotated[str, Field(min_length=8, max_length=8, pattern=r"^[0-9A-Fa-f]{8}$", strict=True)]
        | None
    ) = None
    boot_sector: ImageArtifact | None = None

    @model_validator(mode="after")
    def validate_geometry(self) -> "ImageDisk":
        if self.size_bytes != self.logical_sector_bytes * self.sector_count:
            raise ValueError("La capacidad del disco no coincide con sus sectores lógicos.")
        is_gpt = self.gpt_disk_guid is not None
        is_mbr = self.mbr_disk_signature is not None
        if is_gpt == is_mbr:
            raise ValueError("El disco debe declarar exactamente una tabla GPT o MBR.")
        if is_gpt:
            if self.first_usable_sector is None or self.last_usable_sector is None:
                raise ValueError("El disco GPT debe declarar su rango utilizable.")
            if self.first_usable_sector >= self.last_usable_sector:
                raise ValueError("El rango GPT utilizable no es válido.")
            if self.last_usable_sector >= self.sector_count:
                raise ValueError("El último sector GPT queda fuera de la capacidad del disco.")
            if self.boot_sector is not None:
                raise ValueError("Un disco GPT no puede declarar un boot sector MBR.")
            if len(self.partitions) < 2:
                raise ValueError("El GPT debe incluir al menos una ESP y una raíz.")
        else:
            if any(
                value is not None
                for value in (self.first_usable_sector, self.last_usable_sector, self.gpt_disk_guid)
            ):
                raise ValueError("Un disco MBR no puede declarar geometría o GUID GPT.")
            if self.logical_sector_bytes != 512:
                raise ValueError("El arranque BIOS/MBR requiere sectores lógicos de 512 bytes.")
            if self.boot_sector is None:
                raise ValueError("El disco MBR debe conservar un artefacto de boot sector.")
            if (
                self.boot_sector.size_bytes != 446
                or self.boot_sector.compression != "none"
                or self.boot_sector.path != "boot-sector.bin"
            ):
                raise ValueError(
                    "El boot sector MBR debe ser boot-sector.bin, sin compresión y de 446 bytes."
                )
        if len({part.number for part in self.partitions}) != len(self.partitions):
            raise ValueError("La tabla contiene números de partición repetidos.")
        partition_guids = [part.partition_guid for part in self.partitions]
        if is_gpt:
            if any(guid is None for guid in partition_guids):
                raise ValueError("El GPT requiere GUIDs de partición.")
            if len(set(partition_guids)) != len(partition_guids):
                raise ValueError("El GPT contiene GUIDs de partición repetidos.")
        elif any(guid is not None for guid in partition_guids):
            raise ValueError("La tabla MBR no puede declarar GUIDs de partición GPT.")
        if len({part.filesystem_uuid.lower() for part in self.partitions}) != len(self.partitions):
            raise ValueError("La tabla contiene UUIDs de filesystem repetidos.")
        roles = {part.role for part in self.partitions}
        expected_roles = ("esp", "root") if is_gpt else ("root",)
        if not roles.issuperset(expected_roles):
            label = "GPT" if is_gpt else "MBR"
            raise ValueError(f"El {label} debe incluir una raíz{' y una ESP' if is_gpt else ''}.")
        for role in expected_roles:
            if sum(part.role == role for part in self.partitions) != 1:
                label = "GPT" if is_gpt else "MBR"
                raise ValueError(f"El {label} debe incluir una sola partición {role}.")
        for role in ("boot", "swap"):
            if sum(part.role == role for part in self.partitions) > 1:
                label = "GPT" if is_gpt else "MBR"
                raise ValueError(f"El {label} no puede incluir más de una partición {role}.")
        ordered = sorted(self.partitions, key=lambda part: part.start_sector)
        first_sector = self.first_usable_sector if is_gpt else 2_048
        last_sector = self.last_usable_sector if is_gpt else self.sector_count - 1
        if first_sector is None or last_sector is None:
            raise ValueError("La geometría del disco no declara un rango utilizable.")
        previous_end = first_sector - 1
        for part in ordered:
            end = part.start_sector + part.size_sectors - 1
            if part.start_sector < first_sector or end > last_sector:
                label = "GPT utilizable" if is_gpt else "MBR seguro"
                raise ValueError(f"Una partición queda fuera del rango {label}.")
            if part.role == "esp" and not is_gpt:
                raise ValueError("La tabla MBR no puede incluir una ESP UEFI.")
            if part.start_sector <= previous_end:
                raise ValueError("Las particiones se superponen.")
            previous_end = end
        return self


class ImageManifest(Schema):
    format: Literal["pyfog-disk-image"]
    format_version: Literal[1, 2]
    image_id: UUID
    created_at: datetime
    checksum_algorithm: Literal["sha256"]
    source: ImageSource
    system: OperatingSystem
    architecture: Literal["x86_64"]
    firmware: ImageFirmware
    disk: ImageDisk
    tool: ImageTool
    artifacts: list[ImageArtifact] = Field(min_length=1, max_length=512)
    capabilities: ImageCapabilities | None = None
    disks: list[ImageDisk] | None = None
    encryption: ImageEncryption | None = None
    volumes: list[ImageVolume] = Field(default_factory=list, max_length=128)
    raid_arrays: list[ImageRaidArray] = Field(default_factory=list, max_length=128)
    publishable: bool = Field(default=False, strict=True)

    @field_validator("format_version", mode="before")
    @classmethod
    def version_is_integer(cls, value: object) -> object:
        if type(value) is not int or value not in SUPPORTED_IMAGE_FORMAT_VERSIONS:
            raise ValueError("format_version debe ser el entero 1 o 2.")
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
        all_disks = self.disks or [self.disk]
        if self.disks:
            if self.disks[0] != self.disk:
                raise ValueError("El campo disk debe ser el primer disco del manifiesto.")
            if len({disk.disk_id for disk in self.disks if disk.disk_id}) != len(
                [disk for disk in self.disks if disk.disk_id]
            ):
                raise ValueError("Los discos del manifiesto deben tener identidades únicas.")
            if len(self.disks) > 1 and any(not disk.disk_id for disk in self.disks):
                raise ValueError("Una imagen multidisco debe identificar cada disco de origen.")
            if len(self.disks) > 1 and any(disk.disk_id.startswith("path:") for disk in self.disks):
                raise ValueError("Una imagen multidisco no puede depender del orden /dev.")
        partition_references: list[tuple[str, str]] = [
            (partition.artifact, partition.role)
            for disk in all_disks
            for partition in disk.partitions
            if partition.artifact is not None
        ]
        partition_references.extend(
            (disk.boot_sector.path, "boot-sector")
            for disk in all_disks
            if disk.boot_sector is not None
        )
        partition_paths = [path for path, _role in partition_references]
        if len(set(partition_paths)) != len(partition_paths):
            allow_shared_raid_root = (
                self.capabilities is not None
                and self.capabilities.volumes == "raid1"
                and all(
                    role == "root"
                    for path, role in partition_references
                    if partition_paths.count(path) > 1
                )
            )
            if not allow_shared_raid_root:
                raise ValueError("Dos particiones no pueden compartir un artefacto.")
        if set(paths) != set(partition_paths):
            raise ValueError("Cada artefacto debe corresponder a una partición de datos.")
        expected_commands = {
            "fat32": "partclone.fat",
            "ext4": "partclone.ext4",
            "xfs": "partclone.xfs",
            "btrfs": "partclone.btrfs",
        }
        commands = set(self.tool.commands)
        for disk in all_disks:
            for partition in disk.partitions:
                if (
                    partition.role != "swap"
                    and expected_commands[partition.filesystem] not in commands
                ):
                    raise ValueError(f"Falta la herramienta para la partición {partition.role}.")
            if disk.mbr_disk_signature is not None and "mbr" not in commands:
                raise ValueError("Falta la herramienta para validar el boot sector MBR.")
            if disk.gpt_disk_guid is not None and "mbr" in commands:
                raise ValueError("Un manifiesto GPT no puede declarar herramientas MBR.")
        if self.system.id.lower() != "ubuntu":
            raise ValueError("El manifiesto v1 sólo admite una imagen Ubuntu Linux.")
        return self

    @model_validator(mode="after")
    def validate_capability_declaration(self) -> "ImageManifest":
        if self.format_version == LEGACY_IMAGE_FORMAT_VERSION:
            if self.capabilities is not None:
                raise ValueError("El manifiesto v1 no puede declarar capacidades v2.")
            if self.firmware.type != "uefi" or self.firmware.secure_boot is not False:
                raise ValueError("El manifiesto v1 sólo admite firmware UEFI sin Secure Boot.")
            return self
        if self.capabilities is None:
            raise ValueError("El manifiesto v2 debe declarar capacidades explícitas.")
        if self.capabilities.firmware != self.firmware:
            raise ValueError("Las capacidades de firmware no coinciden con el campo firmware.")
        if (
            self.format_version == IMAGE_FORMAT_VERSION
            and self.firmware.type == "bios"
            and self.disks
            and len(self.disks) > 1
        ):
            raise ValueError("El perfil BIOS multidisco no tiene un boot sector por disco.")
        return self


def capabilities_from_legacy(manifest: ImageManifest) -> ImageCapabilities:
    """Derive the explicit v2 profile represented by a legacy v1 manifest."""

    filesystems: list[str] = sorted(
        {partition.filesystem for partition in manifest.disk.partitions}
    )
    return ImageCapabilities(
        firmware=manifest.firmware,
        partition_table="gpt",
        disks=1,
        filesystems=filesystems,
        encryption="none",
        volumes="partitions",
    )


def manifest_capabilities(manifest: ImageManifest) -> ImageCapabilities:
    """Return explicit capabilities, adapting a v1 manifest without mutating it."""

    return manifest.capabilities or capabilities_from_legacy(manifest)


def manifest_disks(manifest: ImageManifest) -> list[ImageDisk]:
    """Return the v2 disk list while keeping the v1/v2 primary field compatible."""

    return manifest.disks or [manifest.disk]


def image_compatibility_errors(
    manifest: ImageManifest, *, allow_extended: bool = False
) -> list[str]:
    """List reasons why an image cannot be used by the current MVP implementation."""

    capabilities = manifest_capabilities(manifest)
    errors: list[str] = []
    firmware_type = capabilities.firmware.type
    partition_table = capabilities.partition_table
    if firmware_type not in {"uefi", "bios"}:
        errors.append("requiere firmware UEFI o BIOS")
    if capabilities.firmware.secure_boot is not False:
        errors.append("Secure Boot todavía no está soportado")
    if partition_table not in {"gpt", "mbr"}:
        errors.append("requiere tabla de particiones GPT o MBR")
    elif (firmware_type, partition_table) not in {("uefi", "gpt"), ("bios", "mbr")}:
        errors.append("el firmware y la tabla de particiones son incompatibles")
    if firmware_type == "uefi" and manifest.disk.gpt_disk_guid is None:
        errors.append("el perfil UEFI requiere un disco GPT")
    if firmware_type == "bios" and manifest.disk.mbr_disk_signature is None:
        errors.append("el perfil BIOS requiere un disco MBR")
    disks = manifest_disks(manifest)
    if capabilities.disks != len(disks):
        errors.append("la cantidad de discos declarada no coincide con el layout")
    if capabilities.disks != 1 and not allow_extended:
        errors.append("requiere exactamente un disco")
    if allow_extended and capabilities.disks > 1 and firmware_type == "bios":
        errors.append("el perfil BIOS multidisco requiere artefactos de boot separados")
    if allow_extended and capabilities.disks > 1:
        disk_ids = [disk.disk_id for disk in disks]
        if any(not disk_id or disk_id.startswith("path:") for disk_id in disk_ids):
            errors.append("una imagen multidisco requiere identidades estables de disco")
        elif len(set(disk_ids)) != len(disk_ids):
            errors.append("las identidades de disco multidisco deben ser únicas")
    actual_filesystems = {partition.filesystem for disk in disks for partition in disk.partitions}
    declared_filesystems = set(capabilities.filesystems)
    allowed_filesystems = {"fat32", "ext4", "swap"}
    if allow_extended:
        allowed_filesystems |= {"xfs", "btrfs"}
    if firmware_type == "bios":
        allowed_filesystems = {"ext4", "swap"}
    unsupported_filesystems = declared_filesystems - allowed_filesystems
    if unsupported_filesystems:
        errors.append(
            "contiene sistemas de archivos no soportados: "
            + ", ".join(sorted(unsupported_filesystems))
        )
    if actual_filesystems != declared_filesystems:
        errors.append("los sistemas de archivos declarados no coinciden con el layout")
    if capabilities.encryption != "none" and not allow_extended:
        errors.append("el cifrado declarado todavía no está soportado")
    if capabilities.volumes != "partitions" and not allow_extended:
        errors.append("la gestión de volúmenes declarada todavía no está soportada")
    if capabilities.encryption == "none" and manifest.encryption is not None:
        errors.append("la metadata LUKS2 no puede acompañar un perfil sin cifrado")
    if allow_extended and capabilities.encryption == "luks2" and manifest.encryption is None:
        errors.append("el perfil de cifrado LUKS2 no declara metadata criptográfica")
    if allow_extended and capabilities.encryption == "luks2" and capabilities.disks != 1:
        errors.append("LUKS2 requiere exactamente un disco")
    if (
        allow_extended
        and capabilities.encryption != "none"
        and capabilities.volumes != "partitions"
    ):
        errors.append("LUKS2 no puede combinarse con LVM o RAID1")
    if allow_extended and capabilities.volumes == "partitions" and manifest.volumes:
        errors.append("el perfil por particiones no puede declarar volúmenes LVM")
    if allow_extended and capabilities.volumes == "raid1" and manifest.volumes:
        errors.append("RAID1 no puede combinarse con metadata LVM")
    if allow_extended and capabilities.volumes != "raid1" and manifest.raid_arrays:
        errors.append("el manifiesto declara arrays RAID fuera del perfil RAID1")
    if (
        allow_extended
        and capabilities.volumes in {"lvm", "lvm-linear"}
        and (capabilities.volumes != "lvm-linear" or len(manifest.volumes) != 1)
    ):
        errors.append("el perfil LVM lineal no declara un volumen válido")
    if allow_extended and capabilities.volumes == "lvm":
        errors.append("el perfil LVM genérico no está soportado; use lvm-linear")
    if allow_extended and capabilities.volumes == "raid1" and len(manifest.raid_arrays) != 1:
        errors.append("el perfil RAID1 no declara arrays")
    if allow_extended and capabilities.volumes == "raid1" and capabilities.disks < 2:
        errors.append("RAID1 requiere al menos dos discos")
    if allow_extended and capabilities.volumes == "lvm-linear" and capabilities.disks != 1:
        errors.append("LVM lineal requiere exactamente un disco")
    return errors


def ensure_supported_image(manifest: ImageManifest) -> ImageManifest:
    """Reject a validly-shaped but unsupported image before deployment or publication."""

    errors = image_compatibility_errors(manifest)
    if errors:
        raise ValueError("La imagen no es compatible con el perfil actual: " + "; ".join(errors))
    return manifest


def ensure_supported_extended_image(manifest: ImageManifest) -> ImageManifest:
    """Validate the P1/P2 profiles after their feature-specific checks are enabled."""

    errors = image_compatibility_errors(manifest, allow_extended=True)
    if errors:
        raise ValueError(
            "La imagen no es compatible con los perfiles extendidos: " + "; ".join(errors)
        )
    return manifest


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


def upgrade_manifest(value: bytes | str | dict[str, object]) -> ImageManifest:
    """Adapt a v1 manifest to the explicit v2 representation without changing its image data."""

    manifest = parse_image_manifest(value)
    if manifest.format_version == IMAGE_FORMAT_VERSION:
        return manifest
    payload = manifest.model_dump(mode="json", exclude_none=True)
    payload["format_version"] = IMAGE_FORMAT_VERSION
    payload["capabilities"] = capabilities_from_legacy(manifest).model_dump(mode="json")
    return ImageManifest.model_validate(payload)


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
        if artifact.blocks is not None:
            try:
                verify_complete_transfer(resolved, artifact.blocks)
            except (OSError, ValueError) as error:
                raise ValueError(
                    f"Los bloques de {artifact.path} no están verificados: {error}"
                ) from None


def validate_image_manifest(path: Path, artifacts_dir: Path | None = None) -> ImageManifest:
    manifest = load_image_manifest(path)
    ensure_supported_image(manifest)
    if artifacts_dir is not None:
        verify_image_artifacts(manifest, artifacts_dir)
    return manifest
