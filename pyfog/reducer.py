"""Offline, fail-closed reduction of the narrow ext4 image profile."""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pyfog.image_manifest import ImageManifest, manifest_capabilities


class ReductionError(ValueError):
    """An image cannot be reduced without violating the supported profile."""


@dataclass(frozen=True)
class ReductionPlan:
    source: Path
    destination: Path
    source_size_bytes: int
    target_size_bytes: int
    sector_bytes: int
    root_partition_number: int
    root_start_sector: int
    root_end_sector: int
    root_partition_guid: str
    root_target_bytes: int


def build_reduction_plan(
    manifest: ImageManifest,
    source: Path,
    destination: Path,
    target_size_bytes: int,
    *,
    minimum_filesystem_bytes: int,
) -> ReductionPlan:
    """Validate the complete reduction contract before opening the source for writing."""

    capabilities = manifest_capabilities(manifest)
    if capabilities.disks != 1 or capabilities.partition_table != "gpt":
        msg = "La reducción sólo admite un disco GPT."
        raise ReductionError(msg)
    if capabilities.encryption != "none" or capabilities.volumes != "partitions":
        msg = "La reducción rechaza cifrado, LVM, RAID y otros volúmenes."
        raise ReductionError(msg)
    if source == destination or source.resolve() == destination.resolve():
        msg = "El artefacto original y la variante no pueden ser el mismo archivo."
        raise ReductionError(msg)
    if source.is_symlink() or not source.is_file():
        msg = "El origen debe ser un archivo regular no enlazado."
        raise ReductionError(msg)
    if destination.exists() or destination.is_symlink():
        msg = "La variante de salida no se sobrescribe."
        raise ReductionError(msg)
    disk = manifest.disk
    root = next((partition for partition in disk.partitions if partition.role == "root"), None)
    if root is None or root.filesystem != "ext4":
        msg = "La reducción sólo admite una raíz ext4."
        raise ReductionError(msg)
    if any(partition.role == "swap" for partition in disk.partitions):
        msg = "La reducción no mueve una partición swap."
        raise ReductionError(msg)
    if target_size_bytes >= disk.size_bytes:
        msg = "La variante reducida debe ser estrictamente menor que el origen."
        raise ReductionError(msg)
    if target_size_bytes % disk.logical_sector_bytes:
        msg = "El tamaño destino debe ser múltiplo del sector lógico."
        raise ReductionError(msg)
    if disk.first_usable_sector is None or disk.last_usable_sector is None:
        msg = "El manifiesto no declara una geometría GPT completa."
        raise ReductionError(msg)
    target_sectors = target_size_bytes // disk.logical_sector_bytes
    gpt_tail = 34
    if target_sectors <= disk.first_usable_sector + 1 or target_sectors <= gpt_tail:
        msg = "El destino no deja espacio para la geometría GPT."
        raise ReductionError(msg)
    new_root_end = target_sectors - gpt_tail - 1
    if new_root_end < root.start_sector:
        msg = "El destino no deja espacio para la raíz."
        raise ReductionError(msg)
    if (
        minimum_filesystem_bytes <= 0
        or minimum_filesystem_bytes
        > (new_root_end - root.start_sector + 1) * disk.logical_sector_bytes
    ):
        msg = "El filesystem ext4 no cabe en el tamaño solicitado."
        raise ReductionError(msg)
    return ReductionPlan(
        source=source,
        destination=destination,
        source_size_bytes=disk.size_bytes,
        target_size_bytes=target_size_bytes,
        sector_bytes=disk.logical_sector_bytes,
        root_partition_number=root.number,
        root_start_sector=root.start_sector,
        root_end_sector=new_root_end,
        root_partition_guid=str(root.partition_guid),
        root_target_bytes=(new_root_end - root.start_sector + 1) * disk.logical_sector_bytes,
    )


def reduction_commands(plan: ReductionPlan, working_image: Path) -> list[list[str]]:
    """Return an auditable command plan using an explicit root-partition placeholder."""

    root_device = "@ROOT_PARTITION@"
    return [
        ["e2fsck", "-f", "-y", root_device],
        ["resize2fs", "-M", root_device],
        ["losetup", "--detach", "@LOOP_DEVICE@"],
        ["truncate", "--size", str(plan.target_size_bytes), str(working_image)],
        ["sgdisk", "--move-second-header", str(working_image)],
        ["sgdisk", f"--delete={plan.root_partition_number}", str(working_image)],
        [
            "sgdisk",
            f"--new={plan.root_partition_number}:{plan.root_start_sector}:{plan.root_end_sector}",
            f"--typecode={plan.root_partition_number}:8300",
            f"--partition-guid={plan.root_partition_number}:{plan.root_partition_guid}",
            str(working_image),
        ],
        ["sgdisk", "--verify", str(working_image)],
    ]


def reduce_ext4_image(
    plan: ReductionPlan,
    *,
    runner: Callable[[list[str]], None] | None = None,
) -> Path:
    """Create and atomically publish a reduced regular-file image."""

    run = runner or _run_command
    plan.destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{plan.destination.name}.",
            suffix=".part",
            dir=plan.destination.parent,
            delete=False,
        ) as target:
            temporary = Path(target.name)
        shutil.copyfile(plan.source, temporary)
        if runner is not None:
            for command in reduction_commands(plan, temporary):
                run(command)
        else:
            _reduce_with_loop(plan, temporary)
        temporary.replace(plan.destination)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        msg = f"La reducción no se publicó: {error}"
        raise ReductionError(msg) from None
    else:
        return plan.destination
    finally:
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink()


def _run_command(arguments: list[str]) -> None:
    executable = shutil.which(arguments[0])
    if not executable:
        msg = f"Falta la herramienta {arguments[0]}."
        raise ReductionError(msg)
    subprocess.run([executable, *arguments[1:]], check=True, capture_output=True)  # noqa: S603


def _run_output(arguments: list[str]) -> str:
    executable = shutil.which(arguments[0])
    if not executable:
        msg = f"Falta la herramienta {arguments[0]}."
        raise ReductionError(msg)
    result = subprocess.run(  # noqa: S603 - arguments are fixed storage-tool parameters
        [executable, *arguments[1:]], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _reduce_with_loop(plan: ReductionPlan, working_image: Path) -> None:
    """Shrink the filesystem through a loop partition, then rewrite GPT after detaching it."""

    loop_device: str | None = None
    try:
        loop_device = _run_output(["losetup", "--find", "--show", "--partscan", str(working_image)])
        if not re.fullmatch(r"/dev/loop[0-9]+", loop_device):
            msg = "losetup devolvió un dispositivo inesperado."
            raise ReductionError(msg)
        root_device = f"{loop_device}p{plan.root_partition_number}"
        _run_command(["e2fsck", "-f", "-y", root_device])
        _run_command(["resize2fs", "-M", root_device])
    finally:
        if loop_device is not None:
            _run_command(["losetup", "--detach", loop_device])
    with working_image.open("r+b") as image:
        image.truncate(plan.target_size_bytes)
        image.flush()
        os.fsync(image.fileno())
    _run_command(["sgdisk", "--move-second-header", str(working_image)])
    _run_command(["sgdisk", f"--delete={plan.root_partition_number}", str(working_image)])
    _run_command(
        [
            "sgdisk",
            f"--new={plan.root_partition_number}:{plan.root_start_sector}:{plan.root_end_sector}",
            f"--typecode={plan.root_partition_number}:8300",
            f"--partition-guid={plan.root_partition_number}:{plan.root_partition_guid}",
            str(working_image),
        ]
    )
    _run_command(["sgdisk", "--verify", str(working_image)])
