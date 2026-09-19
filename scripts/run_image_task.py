"""Claim and execute one PyFog Linux image task from the ephemeral agent."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import ipaddress
import json
import os
import re
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn, Protocol

from pyfog.key_provider import external_luks2_key
from pyfog.layouts import (
    LayoutError,
    Luks2Layout,
    LvmLinearLayout,
    Raid1Layout,
    filesystem_tool,
    luks2_layout_from_manifest,
    lvm_layout_from_manifest,
    lvm_layout_to_manifest,
    lvm_restore_commands,
    parse_btrfs_subvolumes,
    parse_luks2_metadata,
    parse_lvm_linear_reports,
    parse_mdraid1_export,
    raid1_layout_from_manifest,
    raid1_layout_to_manifest,
    raid1_restore_commands,
    validate_luks2_match,
)
from pyfog.plugins import PluginRegistry, PluginRunner
from pyfog.transfer import (
    TransferBlock,
    TransferManifest,
    missing_block_indices,
    verify_complete_transfer,
    write_verified_block,
)

MAX_RESPONSE_BYTES = 1_000_000
DEFAULT_CHUNK_BYTES = 512 * 1024
NO_TASK_EXIT = 3
MAX_IMAGE_BYTES = 2**50


class ReadableResponse(Protocol):
    def read(self, size: int = -1) -> bytes: ...


class TaskCancelledError(ValueError):
    """The coordinator asked the agent to stop before the next destructive step."""


def _raise_capture_error(message: str) -> NoReturn:
    raise ValueError(message)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward a task capability to an unexpected destination."""

    def redirect_request(
        self,
        _req: urllib.request.Request,
        _fp: object,
        _code: int,
        _msg: str,
        _headers: object,
        _newurl: str,
    ) -> None:
        return None


def validate_server(server: str) -> urllib.parse.SplitResult:
    try:
        url = urllib.parse.urlsplit(server)
        hostname = url.hostname
        port = url.port
    except ValueError:
        raise ValueError("Usá una URL base HTTPS sin credenciales.") from None
    loopback = hostname == "localhost"
    if hostname:
        with contextlib.suppress(ValueError):
            loopback = loopback or ipaddress.ip_address(hostname).is_loopback
    if (
        url.scheme not in {"http", "https"}
        or not hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path not in {"", "/"}
        or (url.scheme == "http" and not loopback)
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValueError(
            "Usá una URL base HTTPS sin credenciales. HTTP solo se admite en loopback."
        )
    return url


def validate_token(token: str) -> str:
    if not token or len(token) > 256 or "\n" in token or "\r" in token:
        raise ValueError("La capacidad del agente no es válida.")
    return token


def opener(ca_file: str | None) -> urllib.request.OpenerDirector:
    context = ssl.create_default_context(cafile=ca_file)
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=context),
        NoRedirect(),
    )


def response_json(response: ReadableResponse) -> dict[str, Any]:
    body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("La respuesta del servidor supera el límite permitido.")
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("El servidor devolvió una respuesta JSON inválida.") from None
    if not isinstance(value, dict):
        raise ValueError("El servidor devolvió una respuesta JSON inesperada.")
    return value


def json_request(
    endpoint: str,
    *,
    method: str,
    payload: dict[str, Any] | None,
    token: str,
    ca_file: str | None,
    expected_status: set[int],
) -> dict[str, Any]:
    headers = {"Accept": "application/json", "Authorization": f"Bearer {validate_token(token)}"}
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    request = urllib.request.Request(  # noqa: S310 - server URL is validated before use
        endpoint, data=body, headers=headers, method=method
    )
    try:
        with opener(ca_file).open(request, timeout=30) as response:
            if response.status not in expected_status:
                raise ValueError(f"El servidor devolvió HTTP {response.status}.")
            return response_json(response)
    except urllib.error.HTTPError as error:
        raise ValueError(f"El servidor rechazó la tarea (HTTP {error.code}).") from None
    except urllib.error.URLError:
        raise ValueError("No se pudo conectar al servidor durante la captura.") from None


def upload_chunk(
    endpoint: str,
    payload: bytes,
    *,
    token: str,
    ca_file: str | None,
    index: int,
    offset: int,
    chunk_size: int,
) -> dict[str, Any]:
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {validate_token(token)}",
        "Content-Type": "application/octet-stream",
        "X-PyFog-Chunk-Index": str(index),
        "X-PyFog-Chunk-Offset": str(offset),
        # Zero means the total is supplied by the final manifest. The server still applies the
        # configured maximum while accepting the stream.
        "X-PyFog-Artifact-Size": "0",
        "X-PyFog-Chunk-SHA256": hashlib.sha256(payload).hexdigest(),
    }
    if len(payload) > chunk_size:
        raise ValueError("El fragmento generado supera el tamaño negociado.")
    request = urllib.request.Request(  # noqa: S310 - server URL is validated before use
        endpoint, data=payload, headers=headers, method="POST"
    )
    try:
        with opener(ca_file).open(request, timeout=60) as response:
            if response.status != 200:
                raise ValueError(
                    f"El servidor devolvió HTTP {response.status} durante la transferencia."
                )
            return response_json(response)
    except urllib.error.HTTPError as error:
        raise ValueError(f"El servidor rechazó un fragmento (HTTP {error.code}).") from None
    except urllib.error.URLError:
        raise ValueError("Se interrumpió la transferencia del artefacto.") from None


def run_command(
    arguments: list[str],
    *,
    timeout: int = 30,
    check: bool = True,
    input_text: str | None = None,
) -> str:
    executable = shutil.which(arguments[0])
    if not executable:
        raise ValueError(f"Falta la herramienta {arguments[0]} en el agente.")
    result = subprocess.run(  # noqa: S603 - arguments are built from validated device metadata
        [executable, *arguments[1:]],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        input=input_text,
    )
    if check and result.returncode != 0:
        raise ValueError(f"La herramienta {arguments[0]} rechazó el disco.")
    return result.stdout


def parse_export(payload: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in payload.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    return values


def inspect_btrfs_root_subvolume(device: str) -> str:
    """Read the root subvolume through a temporary read-only mount."""

    mountpoint = Path(tempfile.mkdtemp(prefix="pyfog-btrfs-"))
    try:
        run_command(["mount", "-o", "ro", device, str(mountpoint)], timeout=60)
        try:
            subvolumes = parse_btrfs_subvolumes(
                run_command(["btrfs", "subvolume", "list", str(mountpoint)], timeout=60)
            )
        except LayoutError as error:
            raise ValueError(str(error)) from None
        root = next(
            (item.path for item in subvolumes if item.path in {"@", "@/", "."}),
            None,
        )
        if root is None:
            raise ValueError("Btrfs no declara un subvolumen raíz conocido.")
        return root
    finally:
        run_command(["umount", "--", str(mountpoint)], timeout=60, check=False)
        shutil.rmtree(mountpoint, ignore_errors=True)


def inspect_lvm_layout(device: str) -> LvmLinearLayout:
    """Read one LVM chain without activating or changing it."""

    if not re.fullmatch(r"/dev/[A-Za-z0-9._+-]+", device):
        raise ValueError("El PV no es un dispositivo seguro.")
    try:
        return parse_lvm_linear_reports(
            run_command(
                [
                    "pvs",
                    "--reportformat",
                    "json",
                    "--units",
                    "b",
                    "--nosuffix",
                    "--options",
                    "pv_uuid,pv_name,pv_size",
                    device,
                ]
            ),
            run_command(
                [
                    "vgs",
                    "--reportformat",
                    "json",
                    "--options",
                    "vg_name,vg_uuid",
                    device,
                ]
            ),
            run_command(
                [
                    "lvs",
                    "--reportformat",
                    "json",
                    "--units",
                    "b",
                    "--nosuffix",
                    "--options",
                    "lv_name,lv_uuid,vg_name,vg_uuid,lv_layout,lv_attr,lv_size",
                    device,
                ]
            ),
        )
    except LayoutError as error:
        raise ValueError(str(error)) from None


def inspect_lvm_filesystem(device: str) -> tuple[LvmLinearLayout, str, str, str | None]:
    """Inspect one linear PV and its mounted-data filesystem without mutating it."""

    layout = inspect_lvm_layout(device)
    logical_device = f"/dev/{layout.vg_name}/{layout.lv_name}"
    values = parse_export(run_command(["blkid", "-o", "export", logical_device]))
    filesystem = values.get("TYPE", "").lower()
    if filesystem == "vfat":
        filesystem = "fat32"
    if filesystem not in {"ext4", "xfs", "btrfs"}:
        raise ValueError("El LV lineal no contiene un filesystem Linux admitido.")
    filesystem_uuid = values.get("UUID", "")
    if not filesystem_uuid:
        raise ValueError("El LV lineal no declara un UUID de filesystem.")
    subvolume = inspect_btrfs_root_subvolume(logical_device) if filesystem == "btrfs" else None
    return layout, filesystem, filesystem_uuid, subvolume


def prepare_capture_storage(
    selected_geometries: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any], bytes | None]],
    opened_mappings: list[str],
) -> None:
    """Open an already-encrypted source only long enough to inspect/capture its filesystem."""

    for _selected, _selector, geometry, _boot in selected_geometries:
        storage = geometry.get("storage")
        if not isinstance(storage, dict) or storage.get("profile") != "luks2":
            continue
        encryption = storage.get("encryption")
        try:
            expected = luks2_layout_from_manifest(encryption)
        except LayoutError as error:
            raise ValueError(str(error)) from None
        provider = configured_luks2_key_provider(expected)
        mapping_name = "pyfog-capture-" + expected.uuid.replace("-", "")
        root = next(
            (
                partition
                for partition in geometry.get("partitions", [])
                if isinstance(partition, dict)
                and partition.get("outer_filesystem") == "crypto_luks"
            ),
            None,
        )
        if not isinstance(root, dict):
            raise ValueError("El perfil LUKS2 no contiene una partición raíz cifrada.")
        mapping = unlock_luks2(str(root["device"]), mapping_name, expected, provider)
        opened_mappings.append(mapping_name)
        values = parse_export(run_command(["blkid", "-o", "export", mapping]))
        filesystem = values.get("TYPE", "").lower()
        if filesystem == "vfat":
            filesystem = "fat32"
        if filesystem not in {"ext4", "xfs", "btrfs"}:
            raise ValueError("El contenedor LUKS2 no contiene un filesystem Linux admitido.")
        filesystem_uuid = values.get("UUID", "")
        if not filesystem_uuid:
            raise ValueError("El filesystem LUKS2 no declara un UUID.")
        root["filesystem"] = filesystem
        root["filesystem_uuid"] = filesystem_uuid
        root["capture_device"] = mapping
        root["mountpoint"] = "/"
        if filesystem == "btrfs":
            root["subvolume"] = inspect_btrfs_root_subvolume(mapping)


def inspect_raid1_layout(device: str) -> Raid1Layout:
    if not re.fullmatch(r"/dev/md(?:[0-9]+|-[A-Za-z0-9._+-]+)", device):
        raise ValueError("El dispositivo mdadm no es seguro.")
    try:
        return parse_mdraid1_export(run_command(["mdadm", "--detail", "--export", device]))
    except LayoutError as error:
        raise ValueError(str(error)) from None


def _nested_raid_device(value: object) -> str | None:
    """Find an md RAID node reported below a selected disk without guessing /dev order."""

    if not isinstance(value, dict):
        return None
    path = value.get("path") or value.get("name")
    kind = str(value.get("type") or "").casefold()
    if (
        isinstance(path, str)
        and (kind.startswith("raid") or path.startswith("/dev/md"))
        and re.fullmatch(r"/dev/md(?:[0-9]+|-[A-Za-z0-9._+-]+)", path)
    ):
        return path
    children = value.get("children")
    if isinstance(children, list):
        for child in children:
            found = _nested_raid_device(child)
            if found is not None:
                return found
    return None


def _contains_device(value: object, target: str) -> bool:
    if not isinstance(value, dict):
        return False
    path = value.get("path") or value.get("name")
    if path == target:
        return True
    children = value.get("children")
    return isinstance(children, list) and any(_contains_device(child, target) for child in children)


def _raid_device_from_inventory(inventory: object, member: str) -> str | None:
    """Find the assembled md node that owns a member in the complete lsblk tree."""

    if not isinstance(inventory, dict):
        return None
    roots = inventory.get("blockdevices")
    if not isinstance(roots, list):
        return None

    def visit(value: object) -> str | None:
        if not isinstance(value, dict):
            return None
        path = value.get("path") or value.get("name")
        kind = str(value.get("type") or "").casefold()
        if (
            isinstance(path, str)
            and (kind.startswith("raid") or path.startswith("/dev/md"))
            and re.fullmatch(r"/dev/md(?:[0-9]+|-[A-Za-z0-9._+-]+)", path)
            and _contains_device(value, member)
        ):
            return path
        children = value.get("children")
        if isinstance(children, list):
            for child in children:
                found = visit(child)
                if found is not None:
                    return found
        return None

    for root in roots:
        found = visit(root)
        if found is not None:
            return found
    return None


def inspect_raid1_filesystem(
    device: str,
) -> tuple[Raid1Layout, str, str, str | None]:
    """Read the filesystem carried by an already assembled RAID1 array."""

    layout = inspect_raid1_layout(device)
    values = parse_export(run_command(["blkid", "-o", "export", device]))
    filesystem = values.get("TYPE", "").lower()
    if filesystem == "vfat":
        filesystem = "fat32"
    if filesystem not in {"ext4", "xfs", "btrfs"}:
        raise ValueError("El array RAID1 no contiene un filesystem Linux admitido.")
    filesystem_uuid = values.get("UUID", "")
    if not filesystem_uuid:
        raise ValueError("El filesystem RAID1 no declara un UUID.")
    subvolume = inspect_btrfs_root_subvolume(device) if filesystem == "btrfs" else None
    return layout, filesystem, filesystem_uuid, subvolume


def inspect_luks2_layout(device: str) -> Luks2Layout:
    if not re.fullmatch(r"/dev/[A-Za-z0-9._+-]+", device):
        raise ValueError("El contenedor LUKS2 no es un dispositivo seguro.")
    try:
        return parse_luks2_metadata(
            run_command(["cryptsetup", "luksDump", "--dump-json-metadata", device])
        )
    except LayoutError as error:
        raise ValueError(str(error)) from None


def unlock_luks2(
    device: str,
    mapping_name: str,
    expected: Luks2Layout,
    key_provider: Callable[[], bytes],
) -> str:
    """Unlock LUKS2 with a short-lived provider key supplied on stdin only."""

    actual = inspect_luks2_layout(device)
    try:
        validate_luks2_match(expected, actual)
    except LayoutError as error:
        raise ValueError(str(error)) from None
    if not re.fullmatch(r"[A-Za-z0-9._+-]{1,64}", mapping_name):
        raise ValueError("El nombre del mapping LUKS2 no es seguro.")
    key = key_provider()
    return _unlock_luks2_with_key(device, mapping_name, key)


def _unlock_luks2_with_key(device: str, mapping_name: str, key: object) -> str:
    if not re.fullmatch(r"/dev/[A-Za-z0-9._+-]+", device):
        raise ValueError("El contenedor LUKS2 no es un dispositivo seguro.")
    if not re.fullmatch(r"[A-Za-z0-9._+-]{1,64}", mapping_name):
        raise ValueError("El nombre del mapping LUKS2 no es seguro.")
    if not isinstance(key, bytes) or not 1 <= len(key) <= 4096:
        raise ValueError("El proveedor LUKS2 no entregó una clave válida.")
    executable = shutil.which("cryptsetup")
    if not executable:
        raise ValueError("Falta la herramienta cryptsetup en el agente.")
    result = subprocess.run(  # noqa: S603 - fixed cryptsetup arguments; key is stdin only
        [executable, "luksOpen", device, mapping_name, "--key-file=-"],
        input=key,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        raise ValueError("cryptsetup rechazó la clave LUKS2.")
    return f"/dev/mapper/{mapping_name}"


def format_and_unlock_luks2(
    device: str,
    expected: Luks2Layout,
    key_provider: Callable[[], bytes],
) -> tuple[str, str]:
    """Create a LUKS2 container from non-secret metadata and open it with one ephemeral key."""

    if not re.fullmatch(r"/dev/[A-Za-z0-9._+-]+", device):
        raise ValueError("El contenedor LUKS2 no es un dispositivo seguro.")
    key = key_provider()
    if not isinstance(key, bytes) or not 1 <= len(key) <= 4096:
        raise ValueError("El proveedor LUKS2 no entregó una clave válida.")
    executable = shutil.which("cryptsetup")
    if not executable:
        raise ValueError("Falta la herramienta cryptsetup en el agente.")
    result = subprocess.run(  # noqa: S603 - fixed cryptsetup argv; key is stdin only
        [
            executable,
            "luksFormat",
            "--type",
            "luks2",
            "--batch-mode",
            "--uuid",
            expected.uuid,
            "--cipher",
            expected.cipher,
            "--sector-size",
            str(expected.sector_size),
            "--key-file=-",
            device,
        ],
        input=key,
        capture_output=True,
        check=False,
        timeout=180,
    )
    if result.returncode != 0:
        raise ValueError("cryptsetup no pudo crear el contenedor LUKS2.")
    actual = inspect_luks2_layout(device)
    try:
        validate_luks2_match(expected, actual)
    except LayoutError as error:
        raise ValueError(str(error)) from None
    mapping_name = "pyfog-" + expected.uuid.replace("-", "")
    mapping = _unlock_luks2_with_key(device, mapping_name, key)
    return mapping, mapping_name


def close_luks2(mapping_name: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9._+-]{1,64}", mapping_name):
        raise ValueError("El nombre del mapping LUKS2 no es seguro.")
    run_command(["cryptsetup", "close", mapping_name], timeout=120)


@dataclass(frozen=True)
class RestoreStoragePlan:
    """Validated storage graph to construct after partition tables are recreated."""

    profile: str
    lvm: LvmLinearLayout | None = None
    raid: Raid1Layout | None = None
    luks: Luks2Layout | None = None
    root_artifacts: tuple[str, ...] = ()


def _layout_disks(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    raw = manifest.get("disks") or [manifest.get("disk")]
    if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
        raise ValueError("El manifiesto no contiene layouts de disco válidos.")
    return [item for item in raw if isinstance(item, dict)]


def _root_partition(disk: dict[str, Any]) -> dict[str, Any]:
    partitions = disk.get("partitions")
    if not isinstance(partitions, list):
        raise ValueError("El layout no contiene particiones.")
    roots = [item for item in partitions if isinstance(item, dict) and item.get("role") == "root"]
    if len(roots) != 1:
        raise ValueError("Cada layout debe declarar una única partición raíz.")
    return roots[0]


def _partition_capacity(partition: dict[str, Any], disk: dict[str, Any]) -> int:
    sectors = partition.get("size_sectors")
    sector_size = disk.get("logical_sector_bytes")
    if type(sectors) is not int or sectors <= 0 or type(sector_size) is not int:
        raise ValueError("El tamaño de la partición raíz no es válido.")
    return sectors * sector_size


def _root_artifact_paths(manifest: dict[str, Any], disks: list[dict[str, Any]]) -> tuple[str, ...]:
    paths: list[str] = []
    for disk in disks:
        artifact = _root_partition(disk).get("artifact")
        if not isinstance(artifact, str) or not artifact:
            raise ValueError("La partición raíz no referencia un artefacto.")
        paths.append(artifact)
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("El manifiesto no contiene artefactos.")
    by_path = {str(item.get("path")): item for item in artifacts if isinstance(item, dict)}
    if any(path not in by_path for path in paths):
        raise ValueError("El artefacto raíz no está publicado en el manifiesto.")
    return tuple(paths)


def build_restore_storage_plan(
    manifest: dict[str, Any],
    selected_pairs: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
) -> RestoreStoragePlan:
    """Validate the extended storage graph before any target partition is modified."""

    capabilities = manifest.get("capabilities")
    if not isinstance(capabilities, dict):
        raise ValueError("La tarea no contiene capacidades de almacenamiento explícitas.")
    disks = _layout_disks(manifest)
    if len(disks) != len(selected_pairs):
        raise ValueError("La cantidad de discos no coincide con el plan de almacenamiento.")
    profile = str(capabilities.get("volumes") or "partitions")
    encryption = str(capabilities.get("encryption") or "none")
    if encryption != "none" and profile != "partitions":
        raise ValueError("No se admite combinar LUKS2 con LVM o RAID1 en una misma imagen.")
    root_artifacts = _root_artifact_paths(manifest, disks)

    if profile == "partitions" and encryption == "none":
        if manifest.get("volumes") or manifest.get("raid_arrays"):
            raise ValueError("El perfil por particiones no puede declarar metadata de volumen.")
        return RestoreStoragePlan("partitions", root_artifacts=root_artifacts)

    if profile == "lvm-linear":
        if encryption != "none" or len(disks) != 1 or len(selected_pairs) != 1:
            raise ValueError("LVM lineal requiere un único disco sin cifrado.")
        volumes = manifest.get("volumes")
        if not isinstance(volumes, list) or len(volumes) != 1:
            raise ValueError("LVM lineal requiere exactamente un volumen declarado.")
        lvm_layout = lvm_layout_from_manifest(volumes[0])
        root = _root_partition(disks[0])
        if lvm_layout.size_bytes > _partition_capacity(root, disks[0]):
            raise ValueError("El LV declarado no entra en la partición PV destino.")
        if manifest.get("raid_arrays"):
            raise ValueError("LVM lineal no puede declarar arrays RAID.")
        return RestoreStoragePlan("lvm-linear", lvm=lvm_layout, root_artifacts=root_artifacts)

    if profile == "raid1":
        if encryption != "none" or len(disks) < 2:
            raise ValueError("RAID1 requiere dos o más discos sin cifrado.")
        arrays = manifest.get("raid_arrays")
        if not isinstance(arrays, list) or len(arrays) != 1:
            raise ValueError("RAID1 requiere exactamente un array declarado.")
        raid_layout = raid1_layout_from_manifest(arrays[0])
        source_ids = [str(disk.get("disk_id") or disk.get("source_disk_id")) for disk in disks]
        if any(not value or value.startswith("path:") for value in source_ids):
            raise ValueError("RAID1 requiere identidades estables para todos sus discos.")
        if tuple(source_ids) != raid_layout.member_ids:
            raise ValueError("Los miembros RAID1 no coinciden con el orden del manifiesto.")
        target_sizes = [_partition_capacity(_root_partition(disk), disk) for disk in disks]
        if raid_layout.size_bytes > min(target_sizes):
            raise ValueError("El array RAID1 declarado no entra en alguno de sus destinos.")
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, list):
            raise ValueError("El manifiesto RAID1 no contiene artefactos.")
        by_path = {str(item.get("path")): item for item in artifacts if isinstance(item, dict)}
        root_metadata = [by_path[path] for path in root_artifacts]
        if any(
            item.get("sha256") != root_metadata[0].get("sha256")
            or item.get("size_bytes") != root_metadata[0].get("size_bytes")
            for item in root_metadata[1:]
        ):
            raise ValueError("Los artefactos raíz de RAID1 no tienen el mismo checksum.")
        return RestoreStoragePlan("raid1", raid=raid_layout, root_artifacts=root_artifacts)

    if encryption == "luks2" and profile == "partitions":
        if len(disks) != 1 or len(selected_pairs) != 1:
            raise ValueError("LUKS2 requiere un único disco.")
        encryption_metadata = manifest.get("encryption")
        if not isinstance(encryption_metadata, dict):
            raise ValueError("LUKS2 requiere metadata criptográfica no secreta.")
        luks_layout = luks2_layout_from_manifest(encryption_metadata)
        if manifest.get("volumes") or manifest.get("raid_arrays"):
            raise ValueError("LUKS2 no puede declarar metadata LVM o RAID.")
        return RestoreStoragePlan("luks2", luks=luks_layout, root_artifacts=root_artifacts)

    raise ValueError("El perfil de almacenamiento de la imagen no está soportado.")


def _require_restore_tools(
    manifest: dict[str, Any], plan: RestoreStoragePlan, *, requires_expansion: bool = False
) -> None:
    """Check every destructive/boot tool before the first partition-table write."""

    firmware = manifest.get("firmware")
    required = {"blkid", "mount", "umount", "mkswap", "sgdisk", "grub-install", "chroot"}
    if isinstance(firmware, dict) and firmware.get("type") == "bios":
        required.discard("sgdisk")
        required.add("sfdisk")
    for artifact in manifest.get("artifacts", []):
        if not isinstance(artifact, dict):
            continue
        path = str(artifact.get("path", ""))
        if path == "boot-sector.bin":
            continue
        for disk in _layout_disks(manifest):
            for partition in disk.get("partitions", []):
                if isinstance(partition, dict) and partition.get("artifact") == path:
                    filesystem = str(partition.get("filesystem", ""))
                    if filesystem != "swap":
                        required.add(filesystem_tool(filesystem))
    if plan.profile == "lvm-linear":
        required.update({"pvcreate", "vgcreate", "lvcreate", "lvchange", "lvs", "pvs", "vgs"})
    elif plan.profile == "raid1":
        required.add("mdadm")
    elif plan.profile == "luks2":
        required.add("cryptsetup")
    if requires_expansion:
        required.update({"e2fsck", "resize2fs"})
    missing = sorted(command for command in required if shutil.which(command) is None)
    if missing:
        raise ValueError("Faltan herramientas del perfil de restore: " + ", ".join(missing))


def configured_luks2_key_provider(layout: Luks2Layout) -> Callable[[], bytes]:
    """Load an administrator-owned key provider without putting a key in agent arguments."""

    directory_value = os.environ.get("PYFOG_PLUGIN_DIR", "")
    plugin_name = os.environ.get("PYFOG_LUKS_KEY_PLUGIN", "")
    key_ref = os.environ.get("PYFOG_LUKS_KEY_REF", "")
    if not directory_value or not plugin_name or not key_ref:
        raise ValueError(
            "LUKS2 requiere PYFOG_PLUGIN_DIR, PYFOG_LUKS_KEY_PLUGIN y PYFOG_LUKS_KEY_REF."
        )
    registry = PluginRegistry()
    try:
        registry.load_directory(Path(directory_value))
        runner = PluginRunner(
            registry,
            environment={
                key: value for key, value in os.environ.items() if key.startswith("PYFOG_PLUGIN_")
            },
        )
    except (OSError, ValueError, RuntimeError) as error:
        raise ValueError(f"No se pudo cargar el proveedor LUKS2: {error}") from None

    def provider() -> bytes:
        try:
            return external_luks2_key(runner, plugin_name, layout, key_ref=key_ref)
        except (OSError, ValueError, RuntimeError) as error:
            raise ValueError(f"El proveedor LUKS2 no entregó una clave: {error}") from None

    return provider


def create_lvm_linear_stack(layout: LvmLinearLayout, device: str) -> str:
    """Create and verify the single-PV/VG/LV graph declared by an image."""

    try:
        commands = lvm_restore_commands(layout, device)
    except (LayoutError, ValueError) as error:
        raise ValueError(str(error)) from None
    for command in commands:
        run_command(command, timeout=180)
    target = f"/dev/{layout.vg_name}/{layout.lv_name}"
    actual = inspect_lvm_layout(device)
    if actual != layout:
        raise ValueError("La topología LVM creada no coincide con el manifiesto.")
    return target


def create_raid1_stack(
    layout: Raid1Layout,
    members: list[str],
    *,
    member_ids: tuple[str, ...],
) -> str:
    """Create a named RAID1 array only after every stable member was mapped."""

    try:
        command = raid1_restore_commands(layout, members, member_ids=member_ids)
    except (LayoutError, ValueError) as error:
        raise ValueError(str(error)) from None
    run_command(command, timeout=300)
    device = f"/dev/md-pyfog-{layout.uuid}"
    actual = inspect_raid1_layout(device)
    if (
        actual.uuid != layout.uuid
        or actual.metadata != layout.metadata
        or len(actual.member_ids) != len(layout.member_ids)
        or actual.size_bytes != layout.size_bytes
    ):
        raise ValueError("La metadata RAID1 creada no coincide con el manifiesto.")
    return device


def block_inventory() -> dict[str, Any]:
    output = run_command(
        [
            "lsblk",
            "--json",
            "--bytes",
            "--paths",
            "--output",
            "NAME,KNAME,PATH,TYPE,SIZE,MODEL,SERIAL,WWN,TRAN,LOG-SEC,PTTYPE,RM,FSTYPE,UUID,PARTUUID,MOUNTPOINTS",
        ],
        timeout=30,
    )
    try:
        document = json.loads(output)
    except json.JSONDecodeError:
        raise ValueError("lsblk devolvió un inventario inválido.") from None
    if not isinstance(document, dict) or not isinstance(document.get("blockdevices"), list):
        raise ValueError("lsblk no devolvió discos.")
    return document


def device_path(device: dict[str, Any]) -> str:
    path = device.get("path") or device.get("name")
    if not isinstance(path, str) or not re.fullmatch(r"/dev/[A-Za-z0-9._+-]+", path):
        raise ValueError("El agente recibió una ruta de dispositivo inválida.")
    return path


def select_disk(document: dict[str, Any], selector: dict[str, Any]) -> dict[str, Any]:
    candidates = [
        device
        for device in document["blockdevices"]
        if isinstance(device, dict) and device.get("type") == "disk"
    ]
    stable_id = str(selector.get("stable_id", ""))
    if stable_id.startswith("wwn:"):
        candidates = [device for device in candidates if device.get("wwn") == stable_id[4:]]
    elif stable_id.startswith("serial:"):
        candidates = [
            device
            for device in candidates
            if str(device.get("serial") or "").strip() == stable_id[7:]
        ]
    elif stable_id.startswith("path:"):
        candidates = [device for device in candidates if device_path(device) == stable_id[5:]]
    else:
        raise ValueError("La tarea no contiene una identidad de disco válida.")
    expected_size = selector.get("size_bytes")
    candidates = [device for device in candidates if int(device.get("size") or 0) == expected_size]
    expected_model = str(selector.get("model") or "").strip()
    if expected_model:
        candidates = [
            device
            for device in candidates
            if str(device.get("model") or "").strip() == expected_model
        ]
    if len(candidates) != 1:
        raise ValueError("El disco reservado cambió o no se puede identificar de forma única.")
    selected = candidates[0]
    if str(selected.get("rm", False)).lower() in {"true", "1"}:
        raise ValueError("El disco removible no pertenece a la matriz de captura.")
    sector = int(selected.get("log-sec") or 0)
    if sector not in {512, 4096}:
        raise ValueError("El disco usa un tamaño de sector no admitido.")
    return selected


def manifest_int(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum or value > MAX_IMAGE_BYTES:
        raise ValueError(f"{label} no es un entero seguro.")
    return value


def manifest_uuid(value: object, label: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise ValueError(f"{label} no es un UUID válido.") from None


def validate_manifest_capabilities(
    manifest: dict[str, Any], *, allow_extended: bool = False
) -> None:
    """Validate the explicit v2 profile before any target block is written."""

    version = manifest.get("format_version")
    firmware = manifest.get("firmware")
    if not isinstance(firmware, dict):
        raise ValueError("El manifiesto no contiene firmware válido.")
    capabilities: dict[str, Any]
    if version == 1:
        if "capabilities" in manifest and manifest["capabilities"] is not None:
            raise ValueError("El manifiesto v1 no puede declarar capacidades v2.")
        capabilities = {
            "firmware": firmware,
            "partition_table": "gpt",
            "disks": 1,
            "filesystems": sorted(
                {str(partition.get("filesystem")) for partition in manifest["disk"]["partitions"]}
            ),
            "encryption": "none",
            "volumes": "partitions",
        }
    elif version == 2:
        raw_capabilities = manifest.get("capabilities")
        if not isinstance(raw_capabilities, dict):
            raise ValueError("El manifiesto v2 debe declarar capacidades explícitas.")
        capabilities = raw_capabilities
        capability_firmware = capabilities.get("firmware")
        if capability_firmware != firmware:
            raise ValueError("Las capacidades de firmware no coinciden con el campo firmware.")
    else:
        raise ValueError("El manifiesto de restauración no es compatible con el agente.")

    firmware_type = firmware.get("type")
    if firmware_type not in {"uefi", "bios"} or firmware.get("secure_boot") is not False:
        raise ValueError("El firmware declarado no es compatible con el agente.")
    partition_table = capabilities.get("partition_table")
    if partition_table not in {"gpt", "mbr"}:
        raise ValueError("La tabla de particiones declarada no es compatible con el agente.")
    if (firmware_type, partition_table) not in {("uefi", "gpt"), ("bios", "mbr")}:
        raise ValueError("El firmware y la tabla de particiones declarados son incompatibles.")
    if type(capabilities.get("disks")) is not int or capabilities["disks"] < 1:
        raise ValueError("La cantidad de discos declarada no es válida.")
    filesystems = capabilities.get("filesystems")
    if (
        not isinstance(filesystems, list)
        or not filesystems
        or any(not isinstance(filesystem, str) for filesystem in filesystems)
        or len(set(filesystems)) != len(filesystems)
    ):
        raise ValueError("La lista de sistemas de archivos declarada no es válida.")
    supported_filesystems = {"fat32", "ext4", "swap"}
    if allow_extended:
        supported_filesystems |= {"xfs", "btrfs"}
    if firmware_type == "bios":
        supported_filesystems.discard("fat32")
    unsupported = set(filesystems) - supported_filesystems
    if unsupported:
        raise ValueError(
            "La imagen contiene sistemas de archivos no soportados: "
            + ", ".join(sorted(unsupported))
        )
    disks = manifest.get("disks") or [manifest["disk"]]
    if not isinstance(disks, list):
        raise ValueError("El manifiesto no contiene una lista de discos válida.")
    actual = {
        str(partition.get("filesystem"))
        for disk in disks
        if isinstance(disk, dict)
        for partition in disk.get("partitions", [])
        if isinstance(partition, dict)
    }
    if set(filesystems) != actual:
        raise ValueError("Los sistemas de archivos declarados no coinciden con el layout.")
    encryption = capabilities.get("encryption")
    if encryption != "none" and not (allow_extended and encryption == "luks2"):
        raise ValueError("El cifrado declarado todavía no está soportado por el agente.")
    volumes = capabilities.get("volumes")
    allowed_volumes = {"partitions"}
    if allow_extended:
        allowed_volumes |= {"lvm", "lvm-linear", "raid1"}
    if volumes not in allowed_volumes:
        raise ValueError("La gestión de volúmenes declarada todavía no está soportada.")
    if (
        allow_extended
        and volumes in {"lvm", "lvm-linear"}
        and (volumes == "lvm" or not manifest.get("volumes"))
    ):
        raise ValueError("El perfil LVM lineal no declara metadata de volumen.")
    if allow_extended and volumes == "raid1" and not manifest.get("raid_arrays"):
        raise ValueError("El perfil RAID1 no declara metadata de array.")
    if allow_extended and encryption == "luks2" and not manifest.get("encryption"):
        raise ValueError("El perfil LUKS2 no declara metadata criptográfica.")
    if allow_extended and encryption != "none" and volumes != "partitions":
        raise ValueError("LUKS2 no puede combinarse con LVM o RAID1.")
    if (
        allow_extended
        and volumes == "partitions"
        and (manifest.get("volumes") or manifest.get("raid_arrays"))
    ):
        raise ValueError("El perfil por particiones no puede declarar metadata de volumen.")
    if allow_extended and volumes == "raid1" and manifest.get("volumes"):
        raise ValueError("RAID1 no puede combinarse con metadata LVM.")


def _validate_restore_disk_layout(
    disk: object,
    *,
    firmware_type: str,
    allow_extended: bool,
    artifact_paths: set[str],
    artifact_metadata: dict[str, dict[str, Any]],
    partition_guids: set[str],
    filesystem_uuids: set[str],
) -> set[str]:
    """Validate one disk while keeping artifact and identity uniqueness global."""

    if not isinstance(disk, dict):
        raise ValueError("El manifiesto contiene una geometría de disco inválida.")
    size = manifest_int(disk.get("size_bytes"), "La capacidad de la imagen", minimum=1)
    sector = disk.get("logical_sector_bytes")
    if type(sector) is not int or sector not in {512, 4096}:
        raise ValueError("El sector lógico de la imagen no es válido.")
    sectors = manifest_int(disk.get("sector_count"), "La cantidad de sectores", minimum=1)
    is_gpt = firmware_type == "uefi"
    if is_gpt:
        first = manifest_int(disk.get("first_usable_sector"), "El primer sector GPT")
        last = manifest_int(disk.get("last_usable_sector"), "El último sector GPT")
        if size != sector * sectors or first >= last or last >= sectors:
            raise ValueError("La geometría GPT de la imagen no es segura.")
        manifest_uuid(disk.get("gpt_disk_guid"), "El GUID GPT")
        if disk.get("mbr_disk_signature") is not None or disk.get("boot_sector") is not None:
            raise ValueError("Un disco UEFI no puede declarar datos MBR.")
    else:
        first = 2_048
        last = sectors - 1
        if (
            size != sector * sectors
            or sector != 512
            or not re.fullmatch(r"[0-9A-Fa-f]{8}", str(disk.get("mbr_disk_signature", "")))
        ):
            raise ValueError("La geometría MBR de la imagen no es segura.")
        if any(
            disk.get(field) is not None
            for field in ("gpt_disk_guid", "first_usable_sector", "last_usable_sector")
        ):
            raise ValueError("Un disco MBR no puede declarar geometría GPT.")
        boot_sector = disk.get("boot_sector")
        if (
            not isinstance(boot_sector, dict)
            or boot_sector.get("path") != "boot-sector.bin"
            or boot_sector.get("size_bytes") != 446
            or boot_sector.get("compression") != "none"
        ):
            raise ValueError("El manifiesto MBR no contiene un boot sector de 446 bytes.")

    partitions = disk.get("partitions")
    if not isinstance(partitions, list):
        raise ValueError("El manifiesto no contiene particiones.")
    if len(partitions) < (2 if is_gpt else 1) or len(partitions) > 4:
        raise ValueError("La cantidad de particiones no es válida.")
    referenced: set[str] = set()
    numbers: set[int] = set()
    spans: list[tuple[int, int]] = []
    roles: list[str] = []
    if not is_gpt:
        boot_sector = disk["boot_sector"]
        boot_path = boot_sector["path"]
        if boot_path not in artifact_paths:
            raise ValueError("El manifiesto MBR no publica el artefacto de boot sector.")
        if artifact_metadata[boot_path]["size_bytes"] != 446:
            raise ValueError("El artefacto de boot sector MBR debe medir 446 bytes.")
        referenced.add(boot_path)
    expected_filesystems = {
        "esp": ({"fat32"}, "/boot/efi"),
        "boot": ({"ext4", "xfs"} if allow_extended else {"ext4"}, "/boot"),
        "root": (
            {"ext4", "xfs", "btrfs"} if allow_extended else {"ext4"},
            "/",
        ),
        "swap": ({"swap"}, None),
    }
    for partition in partitions:
        if not isinstance(partition, dict):
            raise ValueError("El manifiesto contiene una partición inválida.")
        number = manifest_int(partition.get("number"), "El número de partición", minimum=1)
        if number > 128 or number in numbers:
            raise ValueError("La tabla contiene números de partición inválidos o repetidos.")
        numbers.add(number)
        role = partition.get("role")
        if role not in expected_filesystems:
            raise ValueError("La partición contiene un rol no admitido.")
        roles.append(role)
        filesystems, mountpoint = expected_filesystems[role]
        filesystem = partition.get("filesystem")
        if filesystem not in filesystems or partition.get("mountpoint") != mountpoint:
            raise ValueError(f"La partición {role} no coincide con su sistema de archivos.")
        raw_partition_guid = partition.get("partition_guid")
        if is_gpt:
            partition_guid = manifest_uuid(raw_partition_guid, "El GUID de partición")
            if partition_guid in partition_guids:
                raise ValueError("El GPT contiene GUIDs de partición repetidos.")
            partition_guids.add(partition_guid)
        elif raw_partition_guid is not None:
            raise ValueError("La tabla MBR no puede declarar GUIDs de partición.")
        filesystem_uuid = partition.get("filesystem_uuid")
        if not isinstance(filesystem_uuid, str):
            raise ValueError("El UUID de filesystem no es válido.")
        if role == "esp":
            if not re.fullmatch(r"[0-9A-Fa-f]{8}", filesystem_uuid):
                raise ValueError(
                    "La ESP debe conservar un UUID FAT32 de ocho dígitos hexadecimales."
                )
        else:
            manifest_uuid(filesystem_uuid, "El UUID de filesystem")
        filesystem_key = filesystem_uuid.lower()
        if filesystem_key in filesystem_uuids:
            raise ValueError("La imagen contiene UUIDs de filesystem repetidos.")
        filesystem_uuids.add(filesystem_key)
        start = manifest_int(partition.get("start_sector"), "El inicio de partición")
        count = manifest_int(partition.get("size_sectors"), "El tamaño de partición", minimum=1)
        end = start + count - 1
        if start < first or end > last:
            label = "GPT" if is_gpt else "MBR"
            raise ValueError(f"Una partición queda fuera del rango {label} seguro.")
        spans.append((start, end))
        if filesystem == "btrfs":
            subvolume = partition.get("subvolume")
            if not isinstance(subvolume, str) or not re.fullmatch(
                r"/?[A-Za-z0-9._/@+-]+", subvolume
            ):
                raise ValueError("La raíz Btrfs debe declarar un subvolumen seguro.")
            if "snapshot" in subvolume.casefold():
                raise ValueError("La imagen Btrfs no puede restaurar un snapshot.")
        elif partition.get("subvolume") is not None:
            raise ValueError("Sólo Btrfs puede declarar subvolúmenes.")
        artifact = partition.get("artifact")
        if role == "swap":
            if artifact is not None:
                raise ValueError("La partición swap no puede tener un artefacto.")
        elif not isinstance(artifact, str):
            raise ValueError(f"La partición {role} no tiene artefacto.")
        else:
            safe_artifact_path(artifact)
            referenced.add(artifact)
    previous_end = first - 1
    for start, end in sorted(spans):
        if start <= previous_end:
            raise ValueError("Las particiones se superponen.")
        previous_end = end
    if (
        (is_gpt and roles.count("esp") != 1)
        or roles.count("root") != 1
        or (not is_gpt and roles.count("esp") != 0)
        or roles.count("boot") > 1
        or roles.count("swap") > 1
    ):
        label = "GPT" if is_gpt else "MBR"
        raise ValueError(f"El {label} no tiene una combinación de roles válida.")
    return referenced


def validate_restore_manifest(
    manifest: dict[str, Any], image_id: str, *, allow_extended: bool = False
) -> dict[str, Any]:
    """Validate the complete safety-critical v1/v2 manifest inside the initramfs."""

    format_version = manifest.get("format_version")
    if (
        manifest.get("format") != "pyfog-disk-image"
        or type(format_version) is not int
        or format_version not in {1, 2}
    ):
        raise ValueError("El manifiesto de restauración no es compatible con el agente.")
    if manifest.get("checksum_algorithm") != "sha256" or manifest.get("publishable") is not True:
        raise ValueError("El manifiesto no autoriza una restauración verificable.")
    manifest_image_id = manifest_uuid(manifest.get("image_id"), "La identidad de la imagen")
    if manifest_image_id != manifest_uuid(image_id, "La identidad de la tarea"):
        raise ValueError("El manifiesto pertenece a otra imagen.")
    source = manifest.get("source")
    if not isinstance(source, dict):
        raise ValueError("El manifiesto no identifica el origen de la imagen.")
    manifest_uuid(source.get("host_id"), "El equipo de origen")
    manifest_uuid(source.get("inventory_report_id"), "El inventario de origen")
    source_hostname = source.get("hostname")
    if (
        not isinstance(source_hostname, str)
        or not 1 <= len(source_hostname) <= 253
        or any(ord(character) < 32 or ord(character) == 127 for character in source_hostname)
    ):
        raise ValueError("El hostname de origen no es válido.")
    if manifest.get("architecture") != "x86_64":
        raise ValueError("La imagen no es compatible con la arquitectura del agente.")
    firmware = manifest.get("firmware")
    if (
        not isinstance(firmware, dict)
        or firmware.get("type") not in {"uefi", "bios"}
        or firmware.get("secure_boot") is not False
    ):
        raise ValueError("La imagen requiere firmware UEFI o BIOS sin Secure Boot.")
    firmware_type = str(firmware["type"])
    system = manifest.get("system")
    if not isinstance(system, dict) or str(system.get("id", "")).lower() != "ubuntu":
        raise ValueError("El agente sólo puede restaurar imágenes Ubuntu compatibles.")
    tool = manifest.get("tool")
    if not isinstance(tool, dict) or tool.get("name") != "partclone":
        raise ValueError("El manifiesto no identifica Partclone.")
    commands = tool.get("commands")
    required_commands = {"partclone.ext4"}
    if firmware_type == "uefi":
        required_commands.add("partclone.fat")
    else:
        required_commands.add("mbr")
    disks_for_tools = manifest.get("disks") or [manifest.get("disk")]
    for layout in disks_for_tools:
        if not isinstance(layout, dict):
            continue
        for partition in layout.get("partitions", []):
            if not isinstance(partition, dict):
                continue
            filesystem = partition.get("filesystem")
            if filesystem == "xfs":
                required_commands.add("partclone.xfs")
            elif filesystem == "btrfs":
                required_commands.add("partclone.btrfs")
    if not isinstance(commands, list) or not required_commands.issubset(commands):
        raise ValueError("El manifiesto no contiene los comandos Partclone requeridos.")
    disk = manifest.get("disk")
    if not isinstance(disk, dict):
        raise ValueError("El manifiesto no contiene la geometría del disco.")
    raw_disks = manifest.get("disks")
    if raw_disks is None:
        disks = [disk]
    elif isinstance(raw_disks, list):
        disks = raw_disks
        if not disks or disks[0] != disk:
            raise ValueError("El campo disk debe ser el primer disco del manifiesto.")
    else:
        raise ValueError("El manifiesto contiene una lista de discos inválida.")
    if len(disks) > 128:
        raise ValueError("La imagen declara demasiados discos.")
    capabilities = manifest.get("capabilities")
    if isinstance(capabilities, dict) and capabilities.get("disks") != len(disks):
        raise ValueError("La cantidad de discos declarada no coincide con el layout.")
    if len(disks) > 1:
        disk_ids = [item.get("disk_id") if isinstance(item, dict) else None for item in disks]
        if any(
            not isinstance(value, str) or not value or value.startswith("path:")
            for value in disk_ids
        ):
            raise ValueError(
                "Una imagen multidisco debe identificar cada disco sin depender de /dev."
            )
        if len(set(disk_ids)) != len(disk_ids):
            raise ValueError("Los discos del manifiesto deben tener identidades únicas.")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("El manifiesto no contiene artefactos.")
    if len(artifacts) < 2 or len(artifacts) > 512:
        raise ValueError("La cantidad de artefactos no es válida.")
    artifact_paths: set[str] = set()
    artifact_metadata: dict[str, dict[str, Any]] = {}
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise ValueError("El manifiesto contiene un artefacto inválido.")
        path = artifact.get("path")
        if not isinstance(path, str):
            raise ValueError("La ruta de un artefacto no es válida.")
        safe_artifact_path(path, allow_boot_sector=firmware_type == "bios")
        if path in artifact_paths:
            raise ValueError("El manifiesto contiene artefactos repetidos.")
        artifact_paths.add(path)
        artifact_size = manifest_int(
            artifact.get("size_bytes"), "El tamaño de un artefacto", minimum=1
        )
        if artifact.get("compression") not in {"none", "gzip", "zstd"}:
            raise ValueError("La compresión del artefacto no es compatible.")
        if not re.fullmatch(r"[0-9a-f]{64}", str(artifact.get("sha256", ""))):
            raise ValueError("El checksum del artefacto no es un SHA-256.")
        blocks = artifact.get("blocks")
        if blocks is not None:
            try:
                block_manifest = TransferManifest.model_validate(blocks)
            except ValueError as error:
                raise ValueError(f"El índice de bloques de {path} no es válido: {error}") from None
            if block_manifest.size_bytes != artifact_size:
                raise ValueError("El índice de bloques no coincide con el artefacto.")
        artifact_metadata[path] = {
            "size_bytes": artifact_size,
            "compression": artifact["compression"],
        }
    referenced: set[str] = set()
    partition_guids: set[str] = set()
    filesystem_uuids: set[str] = set()
    for item in disks:
        referenced.update(
            _validate_restore_disk_layout(
                item,
                firmware_type=firmware_type,
                allow_extended=allow_extended,
                artifact_paths=artifact_paths,
                artifact_metadata=artifact_metadata,
                partition_guids=partition_guids,
                filesystem_uuids=filesystem_uuids,
            )
        )
    if referenced != artifact_paths or set(artifact_metadata) != artifact_paths:
        raise ValueError("El manifiesto no tiene referencias de artefactos consistentes.")
    validate_manifest_capabilities(manifest, allow_extended=allow_extended)
    return manifest


def safe_artifact_path(value: str, *, allow_boot_sector: bool = False) -> str:
    if (
        not value
        or not value.isascii()
        or "\\" in value
        or value.startswith("/")
        or value.endswith("/")
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", value)
        or not (
            value.startswith("partitions/") or (allow_boot_sector and value == "boot-sector.bin")
        )
    ):
        raise ValueError("La ruta del artefacto no es segura.")
    return value


def validate_restore_target(
    selected: dict[str, Any],
    selector: dict[str, Any],
    manifest: dict[str, Any],
    *,
    disk: dict[str, Any] | None = None,
) -> None:
    """Check every target property again immediately before the first write."""

    disk = disk or manifest["disk"]
    target_size = int(selected.get("size") or 0)
    target_sector = int(selected.get("log-sec") or 0)
    source_size = int(disk["size_bytes"])
    source_sector = int(disk["logical_sector_bytes"])
    if int(selector.get("source_size_bytes") or 0) != source_size:
        raise ValueError("La capacidad de origen de la reserva no coincide con la imagen.")
    if int(selector.get("required_logical_sector_bytes") or 0) != source_sector:
        raise ValueError("El sector de origen de la reserva no coincide con la imagen.")
    if target_size < source_size:
        raise ValueError("El disco destino es menor que la imagen.")
    if target_sector != source_sector:
        raise ValueError("El sector lógico del destino no coincide con la imagen.")
    if int(selector.get("size_bytes") or 0) != target_size:
        raise ValueError("La capacidad del disco destino cambió después de confirmar.")
    if int(selector.get("logical_sector_bytes") or 0) != target_sector:
        raise ValueError("El sector lógico del disco destino cambió después de confirmar.")
    if str(selected.get("rm", False)).lower() in {"true", "1"}:
        raise ValueError("El disco removible no puede ser un destino de restauración.")
    children = selected.get("children") or []
    if not isinstance(children, list):
        raise ValueError("El inventario del disco destino no es válido.")
    if any(
        child.get("mountpoints") or child.get("mountpoint")
        for child in children
        if isinstance(child, dict)
    ):
        raise ValueError("El disco destino tiene una partición montada o en uso.")
    firmware = manifest.get("firmware")
    firmware_type = firmware.get("type") if isinstance(firmware, dict) else None
    running_uefi = Path("/sys/firmware/efi").is_dir()
    if firmware_type == "uefi" and not running_uefi:
        raise ValueError("El agente debe arrancar en firmware UEFI para esta imagen.")
    if firmware_type == "bios" and running_uefi:
        raise ValueError("La imagen BIOS/MBR no puede restaurarse desde firmware UEFI.")


def parse_gpt(
    device: str,
    selected: dict[str, Any],
    *,
    inventory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    output = run_command(["sgdisk", "--print", device])
    guid_match = re.search(r"Disk identifier \(GUID\):\s*([0-9A-Fa-f-]{36})", output)
    first_match = re.search(r"First usable sector is\s+(\d+)", output)
    last_match = re.search(r"last usable sector is\s+(\d+)", output, re.IGNORECASE)
    if not guid_match or not first_match or not last_match:
        raise ValueError("No se pudo leer la geometría GPT del disco.")
    partitions: list[dict[str, Any]] = []
    for line in output.splitlines():
        match = re.match(r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+.*?\s+([0-9A-Fa-f]{4})\s+(.*)$", line)
        if not match:
            continue
        number, start, end, code, name = match.groups()
        partitions.append(
            {
                "number": int(number),
                "start_sector": int(start),
                "size_sectors": int(end) - int(start) + 1,
                "code": code.upper(),
                "name": name.strip(),
            }
        )
    if not partitions:
        raise ValueError("El disco no tiene particiones GPT reconocibles.")
    children = selected.get("children") or []
    child_by_number: dict[int, dict[str, Any]] = {}
    for child in children:
        if not isinstance(child, dict):
            continue
        match = re.search(r"(\d+)$", str(child.get("name", "")))
        if match:
            child_by_number[int(match[1])] = child
    result: list[dict[str, Any]] = []
    storage_metadata: dict[str, Any] | None = None
    for partition in partitions:
        number = partition["number"]
        child = child_by_number.get(number, {})
        part_path = (
            device_path(child)
            if child
            else f"{device}{'p' if device[-1].isdigit() else ''}{number}"
        )
        filesystem = str(child.get("fstype") or "").lower()
        filesystem_uuid = str(child.get("uuid") or "")
        part_guid = str(child.get("partuuid") or "")
        if not filesystem_uuid or not part_guid:
            values = parse_export(run_command(["blkid", "-o", "export", part_path], check=False))
            filesystem = filesystem or values.get("TYPE", "").lower()
            filesystem_uuid = filesystem_uuid or values.get("UUID", "")
            part_guid = part_guid or values.get("PARTUUID", "")
        code = partition["code"]
        capture_device = part_path
        outer_filesystem = filesystem
        if filesystem == "lvm2_member":
            if storage_metadata is not None:
                raise ValueError("El disco contiene más de un perfil de almacenamiento especial.")
            lvm_layout, filesystem, filesystem_uuid, subvolume = inspect_lvm_filesystem(part_path)
            capture_device = f"/dev/{lvm_layout.vg_name}/{lvm_layout.lv_name}"
            storage_metadata = {
                "volumes": [lvm_layout_to_manifest(lvm_layout)],
                "profile": "lvm-linear",
            }
        elif filesystem == "linux_raid_member":
            if storage_metadata is not None:
                raise ValueError("El disco contiene más de un perfil de almacenamiento especial.")
            raid_device = _nested_raid_device(child) or _raid_device_from_inventory(
                inventory, part_path
            )
            if raid_device is None:
                raise ValueError("No se encontró el dispositivo md del miembro RAID1.")
            raid_layout, filesystem, filesystem_uuid, subvolume = inspect_raid1_filesystem(
                raid_device
            )
            stable_id = str(selected.get("stable_id") or "")
            if not stable_id or stable_id.startswith("path:"):
                raise ValueError("RAID1 requiere una identidad estable para cada disco.")
            source_layout = Raid1Layout(
                raid_layout.uuid,
                raid_layout.metadata,
                (stable_id,),
                raid_layout.size_bytes,
            )
            capture_device = raid_device
            storage_metadata = {
                "raid_array": raid1_layout_to_manifest(source_layout),
                "raid_device": raid_device,
                "profile": "raid1",
            }
        elif filesystem == "crypto_luks":
            luks_layout = inspect_luks2_layout(part_path)
            storage_metadata = {
                "encryption": {
                    "type": "luks2",
                    "uuid": luks_layout.uuid,
                    "cipher": luks_layout.cipher,
                    "sector_size": luks_layout.sector_size,
                },
                "profile": "luks2",
            }
        else:
            subvolume = None
        if code == "EF00":
            role, expected_fs, mountpoint = "esp", "fat32", "/boot/efi"
            if filesystem == "vfat":
                filesystem = "fat32"
        elif code == "8200":
            role, expected_fs, mountpoint = "swap", "swap", None
        elif filesystem in {"ext4", "xfs"} and "boot" in partition["name"].lower():
            role, expected_fs, mountpoint = "boot", filesystem, "/boot"
        elif filesystem in {"ext4", "xfs", "btrfs"}:
            role, expected_fs, mountpoint = "root", filesystem, "/"
        elif filesystem == "crypto_luks":
            role, expected_fs, mountpoint = "root", "", "/"
        else:
            raise ValueError(f"La partición {number} no pertenece a la matriz Linux admitida.")
        if (
            filesystem not in {expected_fs, "crypto_luks"}
            or (not filesystem_uuid and filesystem != "crypto_luks")
            or not part_guid
        ):
            raise ValueError(f"La partición {number} no tiene filesystem y UUID compatibles.")
        if filesystem == "btrfs" and subvolume is None:
            subvolume = inspect_btrfs_root_subvolume(capture_device)
        result.append(
            {
                "number": number,
                "role": role,
                "start_sector": partition["start_sector"],
                "size_sectors": partition["size_sectors"],
                "partition_guid": part_guid,
                "filesystem": filesystem,
                "filesystem_uuid": filesystem_uuid,
                "mountpoint": mountpoint,
                "device": part_path,
                "capture_device": capture_device,
                "outer_filesystem": outer_filesystem,
                "subvolume": subvolume,
            }
        )
    roles = [partition["role"] for partition in result]
    if roles.count("esp") != 1 or roles.count("root") != 1:
        raise ValueError("El disco debe tener una ESP y una raíz ext4 únicas.")
    geometry: dict[str, Any] = {
        "size_bytes": int(selected["size"]),
        "logical_sector_bytes": int(selected["log-sec"]),
        "sector_count": int(selected["size"]) // int(selected["log-sec"]),
        "gpt_disk_guid": guid_match[1],
        "first_usable_sector": int(first_match[1]),
        "last_usable_sector": int(last_match[1]),
        "partitions": result,
    }
    if storage_metadata is not None:
        geometry["storage"] = storage_metadata
    return geometry


def parse_mbr(
    device: str,
    selected: dict[str, Any],
    *,
    inventory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read a DOS partition table and classify its Linux filesystems without guessing UEFI."""

    if int(selected.get("log-sec") or 0) != 512:
        raise ValueError("El arranque BIOS/MBR requiere sectores lógicos de 512 bytes.")
    try:
        document = json.loads(run_command(["sfdisk", "--json", device]))
    except json.JSONDecodeError:
        raise ValueError("sfdisk devolvió una tabla MBR inválida.") from None
    table = document.get("partitiontable") if isinstance(document, dict) else None
    if not isinstance(table, dict) or str(table.get("label", "")).lower() != "dos":
        raise ValueError("El disco no contiene una tabla MBR.")
    raw_signature = str(table.get("id", ""))
    signature_match = re.fullmatch(r"0x([0-9A-Fa-f]{8})", raw_signature)
    if not signature_match:
        raise ValueError("La tabla MBR no tiene una firma de disco válida.")
    raw_partitions = table.get("partitions")
    if not isinstance(raw_partitions, list) or not raw_partitions:
        raise ValueError("El disco no tiene particiones MBR reconocibles.")
    children = selected.get("children") or []
    child_by_number: dict[int, dict[str, Any]] = {}
    for child in children:
        if not isinstance(child, dict):
            continue
        match = re.search(r"(\d+)$", str(child.get("name") or child.get("path") or ""))
        if match:
            child_by_number[int(match[1])] = child
    result: list[dict[str, Any]] = []
    storage_metadata: dict[str, Any] | None = None
    for raw_partition in raw_partitions:
        if not isinstance(raw_partition, dict):
            raise ValueError("sfdisk devolvió una partición MBR inválida.")
        node = raw_partition.get("node")
        number_match = re.search(r"(\d+)$", str(node or ""))
        if not number_match:
            raise ValueError("No se pudo determinar el número de una partición MBR.")
        number = int(number_match[1])
        start = raw_partition.get("start")
        count = raw_partition.get("size")
        if type(start) is not int or type(count) is not int or start < 2_048 or count <= 0:
            raise ValueError("Una partición MBR no deja espacio suficiente para GRUB BIOS.")
        child = child_by_number.get(number, {})
        part_path = (
            device_path(child)
            if child
            else f"{device}{'p' if device[-1].isdigit() else ''}{number}"
        )
        filesystem = str(child.get("fstype") or "").lower()
        filesystem_uuid = str(child.get("uuid") or "")
        if not filesystem_uuid or not filesystem:
            values = parse_export(run_command(["blkid", "-o", "export", part_path], check=False))
            filesystem = filesystem or values.get("TYPE", "").lower()
            filesystem_uuid = filesystem_uuid or values.get("UUID", "")
        capture_device = part_path
        outer_filesystem = filesystem
        if filesystem == "lvm2_member":
            if storage_metadata is not None:
                raise ValueError("El disco contiene más de un perfil de almacenamiento especial.")
            lvm_layout, filesystem, filesystem_uuid, subvolume = inspect_lvm_filesystem(part_path)
            capture_device = f"/dev/{lvm_layout.vg_name}/{lvm_layout.lv_name}"
            storage_metadata = {
                "volumes": [lvm_layout_to_manifest(lvm_layout)],
                "profile": "lvm-linear",
            }
        elif filesystem == "linux_raid_member":
            if storage_metadata is not None:
                raise ValueError("El disco contiene más de un perfil de almacenamiento especial.")
            raid_device = _nested_raid_device(child) or _raid_device_from_inventory(
                inventory, part_path
            )
            if raid_device is None:
                raise ValueError("No se encontró el dispositivo md del miembro RAID1.")
            raid_layout, filesystem, filesystem_uuid, subvolume = inspect_raid1_filesystem(
                raid_device
            )
            stable_id = str(selected.get("stable_id") or "")
            if not stable_id or stable_id.startswith("path:"):
                raise ValueError("RAID1 requiere una identidad estable para cada disco.")
            source_layout = Raid1Layout(
                raid_layout.uuid,
                raid_layout.metadata,
                (stable_id,),
                raid_layout.size_bytes,
            )
            capture_device = raid_device
            storage_metadata = {
                "raid_array": raid1_layout_to_manifest(source_layout),
                "raid_device": raid_device,
                "profile": "raid1",
            }
        elif filesystem == "crypto_luks":
            luks_layout = inspect_luks2_layout(part_path)
            storage_metadata = {
                "encryption": {
                    "type": "luks2",
                    "uuid": luks_layout.uuid,
                    "cipher": luks_layout.cipher,
                    "sector_size": luks_layout.sector_size,
                },
                "profile": "luks2",
            }
        else:
            subvolume = None
        mountpoints = child.get("mountpoints") or child.get("mountpoint") or []
        if isinstance(mountpoints, str):
            mountpoints = [mountpoints]
        if filesystem == "swap":
            role, mountpoint = "swap", None
        elif filesystem in {"ext4", "xfs"} and "/boot" in mountpoints:
            role, mountpoint = "boot", "/boot"
        elif filesystem == "crypto_luks" or filesystem in {"ext4", "xfs", "btrfs"}:
            role, mountpoint = "root", "/"
        else:
            raise ValueError(f"La partición {number} no pertenece a la matriz BIOS/MBR admitida.")
        if not filesystem_uuid and filesystem != "crypto_luks":
            raise ValueError(f"La partición {number} no tiene UUID de filesystem.")
        if filesystem == "btrfs" and subvolume is None:
            subvolume = inspect_btrfs_root_subvolume(capture_device)
        result.append(
            {
                "number": number,
                "role": role,
                "start_sector": start,
                "size_sectors": count,
                "partition_guid": None,
                "filesystem": filesystem,
                "filesystem_uuid": filesystem_uuid,
                "mountpoint": mountpoint,
                "device": part_path,
                "capture_device": capture_device,
                "outer_filesystem": outer_filesystem,
                "subvolume": subvolume,
            }
        )
    roles = [partition["role"] for partition in result]
    if roles.count("root") != 1 or roles.count("boot") > 1 or roles.count("swap") > 1:
        raise ValueError("El disco MBR debe tener una raíz única y como máximo /boot y swap.")
    sectors = int(selected["size"]) // int(selected["log-sec"])
    if any(
        int(partition["start_sector"]) + int(partition["size_sectors"]) > sectors
        for partition in result
    ):
        raise ValueError("Una partición MBR queda fuera de la capacidad del disco.")
    spans = sorted(
        (
            int(partition["start_sector"]),
            int(partition["start_sector"]) + int(partition["size_sectors"]) - 1,
        )
        for partition in result
    )
    if any(
        start <= previous_end
        for (start, _), (_, previous_end) in zip(spans[1:], spans, strict=False)
    ):
        raise ValueError("Las particiones MBR se superponen.")
    geometry: dict[str, Any] = {
        "size_bytes": int(selected["size"]),
        "logical_sector_bytes": int(selected["log-sec"]),
        "sector_count": sectors,
        "mbr_disk_signature": signature_match[1].lower(),
        "partitions": result,
    }
    if storage_metadata is not None:
        geometry["storage"] = storage_metadata
    return geometry


def read_mbr_boot_sector(device: str) -> bytes:
    try:
        with Path(device).open("rb", buffering=0) as source:
            value = source.read(512)
    except OSError as error:
        raise ValueError(f"No se pudo leer el sector de arranque MBR: {error}") from None
    if len(value) != 512 or value[510:512] != b"\x55\xaa":
        raise ValueError("El sector de arranque MBR no tiene la firma 55aa.")
    if not any(value[:446]):
        raise ValueError("El sector de arranque MBR no contiene código de arranque.")
    return value


def validate_mbr_boot_sector(device: str, geometry: dict[str, Any]) -> bytes:
    value = read_mbr_boot_sector(device)
    if any(int(partition["start_sector"]) < 2_048 for partition in geometry["partitions"]):
        raise ValueError("El MBR no deja el espacio requerido para incrustar GRUB BIOS.")
    return value[:446]


def assert_disk_is_quiescent(partitions: list[dict[str, Any]]) -> None:
    devices = {
        str(partition.get("capture_device") or partition["device"])
        for partition in partitions
        if isinstance(partition.get("device"), str)
    }
    try:
        mounted = Path("/proc/self/mountinfo").read_text(encoding="utf-8", errors="replace")
    except OSError:
        mounted = ""
    mounted_devices: set[str] = set()
    for line in mounted.splitlines():
        separator = line.find(" - ")
        if separator >= 0:
            fields = line[separator + 3 :].split()
            if len(fields) >= 2:
                mounted_devices.add(fields[1])
    if devices & mounted_devices:
        raise ValueError("El disco tiene una partición montada y no se puede capturar.")
    swaps = Path("/proc/swaps")
    if swaps.is_file():
        content = swaps.read_text(encoding="utf-8", errors="replace")
        swap_devices = {line.split()[0] for line in content.splitlines()[1:] if line.split()}
        if devices & swap_devices:
            raise ValueError("El disco tiene swap activo y no se puede capturar.")


def mounted_filesystems(partitions: list[dict[str, Any]]) -> list[str]:
    """Find supported mountpoints for a hot capture without trusting user input."""

    try:
        document = json.loads(
            run_command(["findmnt", "--json", "--output", "SOURCE,TARGET,FSTYPE"])
        )
    except json.JSONDecodeError:
        raise ValueError("findmnt devolvió un inventario de montajes inválido.") from None
    filesystems = document.get("filesystems") if isinstance(document, dict) else None
    if not isinstance(filesystems, list):
        raise ValueError("findmnt no devolvió filesystems montados.")
    devices = {
        str(partition.get("capture_device") or partition.get("device"))
        for partition in partitions
        if isinstance(partition.get("device"), str)
    }
    mounts: list[str] = []
    for item in filesystems:
        if not isinstance(item, dict) or item.get("source") not in devices:
            continue
        target = item.get("target")
        filesystem = str(item.get("fstype") or "").lower()
        if (
            not isinstance(target, str)
            or not target.startswith("/")
            or target == "/proc"
            or filesystem not in {"ext4", "xfs", "btrfs"}
        ):
            raise ValueError("El hot capture encontró un filesystem o montaje no soportado.")
        mounts.append(target)
    if not mounts:
        raise ValueError("No se encontró un filesystem soportado para congelar.")
    return sorted(set(mounts), key=lambda path: (path.count("/"), path))


def _validate_mountpoint(mountpoint: str) -> None:
    if not mountpoint.startswith("/") or "\x00" in mountpoint:
        raise ValueError("El punto de montaje no es seguro.")


def freeze_filesystems(mountpoints: list[str]) -> list[str]:
    """Freeze mounts in order and thaw already-frozen mounts on partial failure."""

    frozen: list[str] = []
    try:
        for mountpoint in mountpoints:
            _validate_mountpoint(mountpoint)
            run_command(["fsfreeze", "--freeze", mountpoint], timeout=60)
            frozen.append(mountpoint)
    except (OSError, ValueError, subprocess.SubprocessError):
        thaw_filesystems(frozen)
        raise
    return frozen


def thaw_filesystems(mountpoints: list[str]) -> None:
    """Always attempt every thaw and report the first failure."""

    first_error: ValueError | None = None
    for mountpoint in reversed(mountpoints):
        try:
            run_command(["fsfreeze", "--unfreeze", mountpoint], timeout=60)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            first_error = first_error or ValueError(f"No se pudo descongelar {mountpoint}: {error}")
    if first_error is not None:
        raise first_error


@contextmanager
def hot_capture_scope(partitions: list[dict[str, Any]]) -> Iterator[None]:
    """Freeze supported filesystems for the smallest possible capture window."""

    frozen = freeze_filesystems(mounted_filesystems(partitions))
    try:
        yield
    finally:
        thaw_filesystems(frozen)


def assert_target_is_quiescent(selected: dict[str, Any]) -> None:
    """Reject a target with mounted children or an active whole-disk mount/swap."""

    children = selected.get("children") or []
    if not isinstance(children, list):
        raise ValueError("El inventario del disco destino no es válido.")
    partition_paths: list[str] = []
    for child in children:
        if not isinstance(child, dict):
            continue
        path = child.get("path") or child.get("name")
        if isinstance(path, str):
            partition_paths.append(path)
        mountpoints = child.get("mountpoints") or child.get("mountpoint")
        if mountpoints:
            raise ValueError("El disco destino tiene una partición montada o en uso.")
    device = selected.get("path") or selected.get("name")
    if isinstance(device, str):
        partition_paths.append(device)
    assert_disk_is_quiescent([{"device": path} for path in partition_paths])


def command_version() -> str:
    output = run_command(["partclone.ext4", "--version"], check=False)
    match = re.search(r"(?:version|v)\s*([0-9][A-Za-z0-9._+-]*)", output, re.IGNORECASE)
    return match[1] if match else "0.3.45"


def local_agent_capabilities() -> list[str]:
    """Advertise only profiles whose tools are present in this agent image."""

    capabilities = {
        "gpt",
        "mbr",
        "partclone.ext4",
        "partclone.fat",
        "restore",
        "clone",
        "identity",
        "multidisk",
    }
    if shutil.which("partclone.xfs"):
        capabilities.add("partclone.xfs")
    if shutil.which("partclone.btrfs"):
        capabilities.add("partclone.btrfs")
    if shutil.which("fsfreeze") and shutil.which("findmnt"):
        capabilities.add("capture.hot")
    if shutil.which("e2fsck") and shutil.which("resize2fs"):
        capabilities.add("expand.ext4")
    lvm_tools = {"pvs", "vgs", "lvs", "pvcreate", "vgcreate", "lvcreate", "lvchange"}
    if all(shutil.which(tool) for tool in lvm_tools):
        capabilities.add("lvm-linear")
    if shutil.which("mdadm"):
        capabilities.add("raid1")
    if shutil.which("cryptsetup"):
        capabilities.add("luks2")
        plugin_directory = os.environ.get("PYFOG_PLUGIN_DIR", "")
        if (
            plugin_directory
            and Path(plugin_directory).is_dir()
            and os.environ.get("PYFOG_LUKS_KEY_PLUGIN")
            and os.environ.get("PYFOG_LUKS_KEY_REF")
        ):
            capabilities.add("key-provider")
    return sorted(capabilities)


def capture_stream(command: list[str], *, chunk_bytes: int) -> Iterator[bytes]:
    executable = shutil.which(command[0])
    if not executable:
        raise ValueError(f"Falta la herramienta {command[0]} en el agente.")
    process = subprocess.Popen(  # noqa: S603 - fixed capture arguments and validated devices
        [executable, *command[1:]],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if process.stdout is None:
        process.kill()
        process.wait(timeout=60)
        raise ValueError("No se pudo abrir la salida de Partclone.")
    gzip_executable = shutil.which("gzip")
    if not gzip_executable:
        process.kill()
        process.wait(timeout=60)
        raise ValueError("Falta la herramienta gzip en el agente.")
    compressor = subprocess.Popen(  # noqa: S603 - fixed gzip invocation
        [gzip_executable, "-n", "-9"],
        stdin=process.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    process.stdout.close()
    if compressor.stdout is None:
        compressor.kill()
        process.kill()
        compressor.wait(timeout=60)
        process.wait(timeout=60)
        raise ValueError("No se pudo abrir la salida comprimida.")
    try:
        while True:
            chunk = compressor.stdout.read(chunk_bytes)
            if not chunk:
                break
            yield chunk
    finally:
        compressor.stdout.close()
        compressor.wait(timeout=60)
        process.wait(timeout=60)
    if process.returncode != 0 or compressor.returncode != 0:
        raise ValueError("Partclone no pudo capturar la partición.")


def post_progress(
    base: str,
    task_id: str,
    token: str,
    ca_file: str | None,
    sequence: int,
    *,
    phase: str,
    processed: int,
    total: int,
    message: str,
) -> int:
    json_request(
        f"{base}/api/v1/tasks/{task_id}/progress",
        method="POST",
        payload={
            "sequence": sequence,
            "phase": phase,
            "bytes_processed": processed,
            "total_bytes": total,
            "message": message,
        },
        token=token,
        ca_file=ca_file,
        expected_status={200},
    )
    return sequence + 1


class LeaseHeartbeat:
    """Renew a claimed task while a block-reading command is running."""

    def __init__(
        self, base: str, task_id: str, token: str, ca_file: str | None, interval: int
    ) -> None:
        self._endpoint = f"{base}/api/v1/tasks/{task_id}/heartbeat"
        self._token = token
        self._ca_file = ca_file
        self._interval = interval
        self._stop = threading.Event()
        self._failed = threading.Event()
        self._cancelled = threading.Event()
        self._thread = threading.Thread(target=self._run, name="pyfog-lease", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                response = json_request(
                    self._endpoint,
                    method="POST",
                    payload={"message": "Lease renovada por el agente."},
                    token=self._token,
                    ca_file=self._ca_file,
                    expected_status={200},
                )
                if response.get("cancel_requested") is True:
                    self._cancelled.set()
                    return
            except (OSError, ValueError):
                self._failed.set()
                return

    def check(self) -> None:
        if self._cancelled.is_set():
            raise TaskCancelledError("El coordinador solicitó cancelar la tarea.")
        if self._failed.is_set():
            raise ValueError("No se pudo renovar la lease de la tarea.")

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)


def staging_path(root: Path, relative: str) -> Path:
    safe = safe_artifact_path(relative, allow_boot_sector=True)
    if root.exists() and (root.is_symlink() or not root.is_dir()):
        raise ValueError("El directorio de staging no es seguro.")
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    path = root / PurePosixPath(safe)
    parent = path.parent
    parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    resolved_root = root.resolve()
    resolved_parent = parent.resolve()
    if resolved_parent != resolved_root and resolved_root not in resolved_parent.parents:
        raise ValueError("La ruta de staging sale de su directorio.")
    if path.exists() and path.is_symlink():
        raise ValueError("El archivo de staging no puede ser un enlace simbólico.")
    return path


def download_artifact(
    endpoint: str,
    destination: Path,
    *,
    token: str,
    ca_file: str | None,
    expected_size: int,
    expected_sha256: str,
    chunk_bytes: int,
    check_cancel: Callable[[], None],
    block_manifest: dict[str, Any] | None = None,
) -> None:
    """Stream one published artifact to staging and verify it without buffering the image."""

    if block_manifest is not None:
        manifest = TransferManifest.model_validate(block_manifest)
        if manifest.size_bytes != expected_size:
            raise ValueError("El índice de bloques no coincide con el tamaño del artefacto.")
        destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if destination.is_symlink() or (destination.exists() and not destination.is_file()):
            raise ValueError("El archivo de staging no es regular.")
        if destination.exists() and destination.stat().st_size > manifest.size_bytes:
            destination.unlink()
        for index in missing_block_indices(destination, manifest):
            block = manifest.blocks[index]
            check_cancel()
            separator = "&" if "?" in endpoint else "?"
            ranged_endpoint = f"{endpoint}{separator}offset={block.offset}&length={block.size}"
            headers = {
                "Accept": "application/octet-stream",
                "Authorization": f"Bearer {validate_token(token)}",
                "X-PyFog-Block-Index": str(block.index),
            }
            request = urllib.request.Request(  # noqa: S310 - endpoint is constrained by the coordinator URL
                ranged_endpoint, headers=headers, method="GET"
            )
            try:
                with opener(ca_file).open(request, timeout=60) as response:
                    payload = response.read(block.size + 1)
            except urllib.error.HTTPError as error:
                raise ValueError(
                    f"El servidor rechazó el bloque {index} (HTTP {error.code})."
                ) from None
            except urllib.error.URLError:
                raise ValueError(f"Se interrumpió la descarga del bloque {index}.") from None
            if len(payload) != block.size:
                raise ValueError(f"El servidor devolvió un tamaño inválido para el bloque {index}.")
            write_verified_block(destination, manifest, block, payload)
        verify_complete_transfer(destination, manifest)
        return

    if destination.exists():
        if destination.is_symlink() or not destination.is_file():
            raise ValueError("El archivo de staging no es regular.")
        digest = hashlib.sha256()
        with destination.open("rb") as source:
            for chunk in iter(lambda: source.read(chunk_bytes), b""):
                digest.update(chunk)
        if destination.stat().st_size == expected_size and digest.hexdigest() == expected_sha256:
            return
        destination.unlink()
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.part")
    headers = {
        "Accept": "application/octet-stream",
        "Authorization": f"Bearer {validate_token(token)}",
    }
    request = urllib.request.Request(endpoint, headers=headers, method="GET")  # noqa: S310
    digest = hashlib.sha256()
    received = 0
    try:
        with opener(ca_file).open(request, timeout=60) as response, temporary.open("xb") as target:
            while True:
                check_cancel()
                chunk = response.read(chunk_bytes)
                if not chunk:
                    break
                received += len(chunk)
                if received > expected_size:
                    raise ValueError("El artefacto descargado supera su tamaño declarado.")
                digest.update(chunk)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        if received != expected_size or digest.hexdigest() != expected_sha256:
            raise ValueError("El checksum o tamaño del artefacto descargado no coincide.")
        temporary.replace(destination)
    except urllib.error.HTTPError as error:
        raise ValueError(f"El servidor rechazó el artefacto (HTTP {error.code}).") from None
    except urllib.error.URLError:
        raise ValueError("Se interrumpió la descarga del artefacto.") from None
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink()


def partition_device(device: str, number: int) -> str:
    if not 1 <= number <= 128:
        raise ValueError("El número de partición no es válido.")
    base = device_path({"path": device})
    return f"{base}{'p' if base[-1].isdigit() else ''}{number}"


def role_code(role: str, partition_table: str = "gpt") -> str:
    try:
        codes = {
            "gpt": {"esp": "EF00", "boot": "8300", "root": "8300", "swap": "8200"},
            "mbr": {"boot": "83", "root": "83", "swap": "82"},
        }
        return codes[partition_table][role]
    except KeyError:
        raise ValueError("La imagen contiene un rol de partición no admitido.") from None


def restore_partition_table(device: str, disk: dict[str, Any]) -> None:
    """Recreate only the manifest layout; the caller must validate the target first."""

    partitions = disk["partitions"]
    if disk.get("mbr_disk_signature") is not None:
        lines = [
            "label: dos",
            f"label-id: 0x{str(disk['mbr_disk_signature']).lower().removeprefix('0x')}",
            "unit: sectors",
            "sector-size: 512",
            "",
        ]
        for partition in partitions:
            role = str(partition["role"])
            if role == "esp":
                raise ValueError("Una tabla MBR no puede contener una ESP UEFI.")
            start = int(partition["start_sector"])
            count = int(partition["size_sectors"])
            bootable = ", bootable" if role in {"root", "boot"} else ""
            lines.append(f"start={start}, size={count}, type={role_code(role, 'mbr')}{bootable}")
        run_command(
            ["sfdisk", "--wipe", "always", "--no-reread", device],
            timeout=120,
            input_text="\n".join(lines) + "\n",
        )
        if shutil.which("partprobe"):
            run_command(["partprobe", device], timeout=60, check=False)
        run_command(["sfdisk", "--verify", device], timeout=120)
        return
    run_command(["sgdisk", "--zap-all", device], timeout=120)
    run_command(["sgdisk", "--clear", f"--disk-guid={disk['gpt_disk_guid']}", device], timeout=120)
    for partition in partitions:
        number = int(partition["number"])
        start = int(partition["start_sector"])
        end = start + int(partition["size_sectors"]) - 1
        role = str(partition["role"])
        name = {
            "esp": "EFI System",
            "boot": "Linux /boot",
            "root": "Linux root",
            "swap": "Linux swap",
        }[role]
        run_command(
            [
                "sgdisk",
                f"--new={number}:{start}:{end}",
                f"--typecode={number}:{role_code(role)}",
                f"--change-name={number}:{name}",
                f"--partition-guid={number}:{partition['partition_guid']}",
                device,
            ],
            timeout=120,
        )
    # On a larger destination this moves the secondary GPT to the actual end of the disk.
    run_command(["sgdisk", "--move-second-header", device], timeout=120)
    if shutil.which("partprobe"):
        run_command(["partprobe", device], timeout=60, check=False)
    run_command(["sgdisk", "--verify", device], timeout=120)


def validate_target_expansion(selected: dict[str, Any], disk: dict[str, Any]) -> None:
    """Validate the narrow, non-destructive expansion profile before partition writes."""

    target_size = int(selected.get("size") or 0)
    source_size = int(disk.get("size_bytes") or 0)
    if target_size <= source_size:
        return
    if disk.get("mbr_disk_signature") is not None:
        raise ValueError("La expansión segura sólo está disponible para un destino GPT.")
    partitions = disk.get("partitions")
    if not isinstance(partitions, list):
        raise ValueError("El manifiesto no contiene particiones para expandir.")
    root = next((partition for partition in partitions if partition.get("role") == "root"), None)
    if not isinstance(root, dict) or root.get("filesystem") != "ext4":
        raise ValueError("La expansión sólo admite una raíz ext4.")
    ordered = sorted(
        partitions,
        key=lambda partition: int(partition.get("start_sector") or 0),
    )
    if ordered[-1] is not root:
        raise ValueError("La raíz ext4 debe ser la última partición para expandirla.")
    if any(partition.get("role") == "swap" for partition in partitions):
        raise ValueError("La expansión no mueve una partición swap posterior a la raíz.")


def _new_gpt_last_usable_sector(output: str) -> int:
    match = re.search(r"last usable sector is\s+(\d+)", output, re.IGNORECASE)
    if not match:
        raise ValueError("No se pudo determinar el último sector GPT del destino.")
    return int(match[1])


def expand_gpt_root_partition(device: str, selected: dict[str, Any], disk: dict[str, Any]) -> None:
    """Expand the final ext4 root partition and filesystem on a larger GPT target."""

    validate_target_expansion(selected, disk)
    target_size = int(selected.get("size") or 0)
    if target_size <= int(disk["size_bytes"]):
        return
    root = next(partition for partition in disk["partitions"] if partition["role"] == "root")
    number = int(root["number"])
    start = int(root["start_sector"])
    guid = str(root["partition_guid"])
    run_command(["sgdisk", "--move-second-header", device], timeout=120)
    geometry = run_command(["sgdisk", "--print", device], timeout=120)
    last = _new_gpt_last_usable_sector(geometry)
    run_command(["sgdisk", f"--delete={number}", device], timeout=120)
    run_command(
        [
            "sgdisk",
            f"--new={number}:{start}:{last}",
            f"--typecode={number}:8300",
            f"--change-name={number}:Linux root",
            f"--partition-guid={number}:{guid}",
            device,
        ],
        timeout=120,
    )
    if shutil.which("partprobe"):
        run_command(["partprobe", device], timeout=60, check=False)
    root_device = partition_device(device, number)
    run_command(["e2fsck", "-f", "-p", root_device], timeout=600)
    run_command(["resize2fs", root_device], timeout=600)
    run_command(["sgdisk", "--verify", device], timeout=120)


def write_mbr_boot_code(artifact: Path, device: str) -> None:
    try:
        value = artifact.read_bytes()
    except OSError as error:
        raise ValueError(f"No se pudo leer el boot sector MBR publicado: {error}") from None
    if len(value) != 446:
        raise ValueError("El artefacto de boot sector MBR debe medir 446 bytes.")
    try:
        with Path(device).open("r+b", buffering=0) as target:
            target.seek(0)
            target.write(value)
            target.flush()
            os.fsync(target.fileno())
    except OSError as error:
        raise ValueError(f"No se pudo instalar el boot sector MBR: {error}") from None


def restore_partition_artifact(
    artifact: Path,
    compression: str,
    partition: str,
    role: str,
    filesystem: str = "",
    *,
    timeout: int = 3600,
) -> None:
    command = filesystem_tool(filesystem or ("fat32" if role == "esp" else "ext4"))
    executable = shutil.which(command)
    if not executable:
        raise ValueError(f"Falta la herramienta {command} en el agente.")
    if compression == "none":
        result = subprocess.run(  # noqa: S603 - commands and paths were validated above
            [executable, "-r", "-s", str(artifact), "-o", partition],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            raise ValueError(f"Partclone no pudo restaurar la partición {role}.")
        return
    decompressor_name = "gzip" if compression == "gzip" else "zstd"
    decompressor = shutil.which(decompressor_name)
    if not decompressor:
        raise ValueError(f"Falta la herramienta {decompressor_name} en el agente.")
    source = subprocess.Popen(  # noqa: S603 - commands and paths were validated above
        [decompressor, "-dc", str(artifact)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if source.stdout is None:
        source.kill()
        source.wait(timeout=60)
        raise ValueError("No se pudo abrir el artefacto comprimido.")
    writer = subprocess.Popen(  # noqa: S603 - commands and paths were validated above
        [executable, "-r", "-s", "-", "-o", partition],
        stdin=source.stdout,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    source.stdout.close()
    try:
        writer.wait(timeout=timeout)
        source.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        writer.kill()
        source.kill()
        writer.wait(timeout=60)
        source.wait(timeout=60)
        raise ValueError(f"Se agotó el tiempo al restaurar la partición {role}.") from None
    if writer.returncode != 0 or source.returncode != 0:
        raise ValueError(f"Partclone no pudo restaurar la partición {role}.")


def initialize_swap(partition: dict[str, Any], device: str) -> None:
    filesystem_uuid = str(partition.get("filesystem_uuid", ""))
    manifest_uuid(filesystem_uuid, "El UUID de swap")
    run_command(
        [
            "mkswap",
            "-U",
            filesystem_uuid,
            partition_device(device, int(partition["number"])),
        ],
        timeout=120,
    )


def verify_restored_layout(
    device: str,
    disk: dict[str, Any],
    *,
    root_device_override: str | None = None,
    root_outer_type: str | None = None,
) -> None:
    if shutil.which("partprobe"):
        run_command(["partprobe", device], timeout=60, check=False)
    if disk.get("mbr_disk_signature") is not None:
        run_command(["sfdisk", "--verify", device], timeout=120)
        try:
            document = json.loads(run_command(["sfdisk", "--json", device]))
        except json.JSONDecodeError:
            raise ValueError("sfdisk devolvió un layout MBR inválido al verificar.") from None
        table = document.get("partitiontable") if isinstance(document, dict) else None
        if not isinstance(table, dict) or str(table.get("label", "")).lower() != "dos":
            raise ValueError("La restauración no conservó la tabla MBR.")
        actual_signature = str(table.get("id", "")).lower().removeprefix("0x")
        if actual_signature != str(disk["mbr_disk_signature"]).lower().removeprefix("0x"):
            raise ValueError("La firma de disco MBR restaurada no coincide.")
        read_mbr_boot_sector(device)
    else:
        run_command(["sgdisk", "--verify", device], timeout=120)
    expected_types = {
        "esp": {"vfat", "fat32"},
        "boot": {"ext4", "xfs"},
        "root": {"ext4", "xfs", "btrfs"},
        "swap": {"swap"},
    }
    for partition in disk["partitions"]:
        path = partition_device(device, int(partition["number"]))
        values = parse_export(run_command(["blkid", "-o", "export", path]))
        filesystem = values.get("TYPE", "").lower()
        if partition.get("role") == "root" and root_outer_type is not None:
            if filesystem.casefold() != root_outer_type.casefold():
                raise ValueError("La partición raíz no contiene el contenedor esperado.")
            continue
        expected = expected_types[str(partition["role"])]
        if filesystem not in expected:
            raise ValueError(f"La partición {partition['role']} no tiene el filesystem esperado.")
        if values.get("UUID", "").lower() != str(partition["filesystem_uuid"]).lower():
            raise ValueError(f"El UUID de la partición {partition['role']} no coincide.")
    if root_device_override is not None:
        root = next(partition for partition in disk["partitions"] if partition["role"] == "root")
        values = parse_export(run_command(["blkid", "-o", "export", root_device_override]))
        filesystem = values.get("TYPE", "").lower()
        expected = expected_types["root"]
        if filesystem not in expected:
            raise ValueError("El filesystem raíz del volumen restaurado no es compatible.")
        if values.get("UUID", "").lower() != str(root["filesystem_uuid"]).lower():
            raise ValueError("El UUID del filesystem raíz restaurado no coincide.")


def safe_target_path(root: Path, relative: str, *, allow_symlink: bool = False) -> Path:
    path = root / PurePosixPath(relative)
    resolved_root = root.resolve(strict=True)
    try:
        resolved_parent = path.parent.resolve(strict=False)
    except OSError as error:
        raise ValueError(f"No se pudo preparar el destino del clon: {error}") from None
    if resolved_parent != resolved_root and resolved_root not in resolved_parent.parents:
        raise ValueError("La ruta del clon sale del sistema restaurado.")
    if path.exists() and path.is_symlink() and not allow_symlink:
        raise ValueError("El sistema restaurado contiene un enlace inseguro.")
    return path


def atomic_text(path: Path, value: str, mode: int = 0o600) -> None:
    if path.exists() and path.is_symlink():
        raise ValueError("No se puede sobrescribir un enlace simbólico del sistema restaurado.")
    path.parent.mkdir(parents=True, mode=0o755, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(value, encoding="utf-8")
        temporary.chmod(mode)
        temporary.replace(path)
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink()


def validate_clone_hostname(hostname: str) -> str:
    if (
        not hostname
        or ".." in hostname
        or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", hostname)
        or any(
            len(label) > 63 or label.startswith("-") or label.endswith("-")
            for label in hostname.split(".")
        )
    ):
        raise ValueError("El hostname del clon no es válido.")
    return hostname.lower()


def customize_clone_identity(root: Path, hostname: str) -> None:
    """Remove source identity and install a first-boot regeneration hook in the target root."""

    hostname = validate_clone_hostname(hostname)
    etc = safe_target_path(root, "etc")
    if not etc.is_dir() or etc.is_symlink():
        raise ValueError("La raíz restaurada no tiene un /etc seguro.")
    atomic_text(safe_target_path(root, "etc/hostname"), hostname + "\n")
    for relative in ("etc/machine-id", "var/lib/dbus/machine-id"):
        path = safe_target_path(root, relative, allow_symlink=True)
        if path.is_symlink():
            path.unlink()
        elif path.exists() and not path.is_file():
            raise ValueError("La identidad del sistema no es un archivo regular.")
        atomic_text(path, "")
    random_seed = safe_target_path(root, "var/lib/systemd/random-seed")
    if random_seed.exists():
        if random_seed.is_symlink() or not random_seed.is_file():
            raise ValueError("La semilla aleatoria del clon no es segura.")
        random_seed.unlink()
    ssh_directory = safe_target_path(root, "etc/ssh")
    if ssh_directory.is_dir() and not ssh_directory.is_symlink():
        for key in ssh_directory.glob("ssh_host_*"):
            if key.is_symlink() or not key.is_file():
                raise ValueError("Las claves SSH del clon contienen un archivo inseguro.")
            key.unlink()
    netplan = safe_target_path(root, "etc/netplan")
    netplan.mkdir(parents=True, mode=0o755, exist_ok=True)
    for config in netplan.glob("*.yaml"):
        if config.is_symlink() or not config.is_file():
            raise ValueError("La configuración de red del clon no es segura.")
        config.unlink()
    for relative in ("etc/systemd/network", "etc/NetworkManager/system-connections"):
        directory = safe_target_path(root, relative)
        if directory.is_dir() and not directory.is_symlink():
            for config in directory.iterdir():
                if config.is_symlink() or not config.is_file():
                    raise ValueError("La configuración de red del clon no es segura.")
                config.unlink()
    persistent_net = safe_target_path(root, "etc/udev/rules.d/70-persistent-net.rules")
    if persistent_net.exists():
        if persistent_net.is_symlink() or not persistent_net.is_file():
            raise ValueError("La identidad de red persistente del clon no es segura.")
        persistent_net.unlink()
    atomic_text(
        netplan / "99-pyfog-dhcp.yaml",
        (
            "network:\n"
            "  version: 2\n"
            "  ethernets:\n"
            "    pyfog-dhcp:\n"
            "      match:\n"
            '        name: "en*"\n'
            "      dhcp4: true\n"
            "      dhcp6: false\n"
        ),
    )
    marker = safe_target_path(root, "etc/pyfog/clone-identity")
    marker.parent.mkdir(parents=True, mode=0o755, exist_ok=True)
    atomic_text(marker, "pyfog-clone-v1\n")
    hook = safe_target_path(root, "usr/local/sbin/pyfog-first-boot-identity")
    hook.parent.mkdir(parents=True, mode=0o755, exist_ok=True)
    atomic_text(
        hook,
        (
            "#!/bin/sh\n"
            "set -eu\n"
            "if command -v systemd-machine-id-setup >/dev/null 2>&1; then "
            "systemd-machine-id-setup; fi\n"
            "if command -v ssh-keygen >/dev/null 2>&1; then ssh-keygen -A; fi\n"
            "rm -f /etc/pyfog/clone-identity\n"
        ),
        mode=0o700,
    )
    unit = safe_target_path(root, "etc/systemd/system/pyfog-first-boot-identity.service")
    unit.parent.mkdir(parents=True, mode=0o755, exist_ok=True)
    atomic_text(
        unit,
        (
            "[Unit]\n"
            "Description=Generate independent PyFog clone identity\n"
            "Before=ssh.service sshd.service\n"
            "ConditionPathExists=/etc/pyfog/clone-identity\n\n"
            "[Service]\nType=oneshot\n"
            "ExecStart=/usr/local/sbin/pyfog-first-boot-identity\n\n"
            "[Install]\nWantedBy=multi-user.target\n"
        ),
        mode=0o644,
    )
    wants = safe_target_path(root, "etc/systemd/system/multi-user.target.wants")
    wants.mkdir(parents=True, mode=0o755, exist_ok=True)
    link = wants / "pyfog-first-boot-identity.service"
    if link.exists() or link.is_symlink():
        if not link.is_symlink() or link.resolve() != unit.resolve():
            raise ValueError("El enlace de identidad del clon ya existe con otro destino.")
    else:
        link.symlink_to("../pyfog-first-boot-identity.service")


@contextlib.contextmanager
def mounted_target(
    root: Path,
    device: str,
    partitions: list[dict[str, Any]],
    partition_table: str = "gpt",
    root_device_override: str | None = None,
) -> Iterator[dict[str, Path]]:
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    by_role = {str(partition["role"]): partition for partition in partitions}
    root_device = root_device_override or partition_device(device, int(by_role["root"]["number"]))
    root_mount = root / "root"
    esp_mount = root_mount / "boot/efi"
    boot_mount = root_mount / "boot"
    mounted: list[Path] = []
    try:
        root_mount.mkdir(parents=True, mode=0o755, exist_ok=True)
        root_options: list[str] = []
        root_partition = by_role["root"]
        if root_partition.get("filesystem") == "btrfs":
            subvolume = root_partition.get("subvolume")
            if not isinstance(subvolume, str) or not re.fullmatch(
                r"/?[A-Za-z0-9._/@+-]+", subvolume
            ):
                raise ValueError("El subvolumen Btrfs del destino no es seguro.")
            root_options.append(f"subvol={subvolume.lstrip('/')}")
        mount_options = root_partition.get("mount_options") or []
        if not isinstance(mount_options, list) or any(
            not isinstance(option, str) or not re.fullmatch(r"[A-Za-z0-9._+=:@/-]+", option)
            for option in mount_options
        ):
            raise ValueError("Las opciones de montaje Btrfs no son seguras.")
        root_options.extend(mount_options)
        mount_command = ["mount"]
        if root_options:
            mount_command.extend(["-o", ",".join(root_options)])
        mount_command.extend([root_device, str(root_mount)])
        run_command(mount_command, timeout=60)
        mounted.append(root_mount)
        if "boot" in by_role:
            boot_mount.mkdir(parents=True, mode=0o755, exist_ok=True)
            run_command(
                [
                    "mount",
                    partition_device(device, int(by_role["boot"]["number"])),
                    str(boot_mount),
                ],
                timeout=60,
            )
            mounted.append(boot_mount)
        if partition_table == "gpt":
            if "esp" not in by_role:
                raise ValueError("El layout UEFI no contiene una ESP.")
            esp_mount.mkdir(parents=True, mode=0o755, exist_ok=True)
            run_command(
                [
                    "mount",
                    partition_device(device, int(by_role["esp"]["number"])),
                    str(esp_mount),
                ],
                timeout=60,
            )
            mounted.append(esp_mount)
        yield {"root": root_mount, "esp": esp_mount, "boot": boot_mount}
    finally:
        for mountpoint in reversed(mounted):
            run_command(["umount", "--", str(mountpoint)], timeout=60, check=False)


def ensure_uefi_boot(
    root: Path,
    device: str,
    partitions: list[dict[str, Any]],
    *,
    root_device_override: str | None = None,
) -> None:
    with mounted_target(
        root, device, partitions, root_device_override=root_device_override
    ) as mounts:
        grub = shutil.which("grub-install")
        if not grub:
            raise ValueError("Falta grub-install para preparar el arranque UEFI.")
        run_command(
            [
                "grub-install",
                "--target=x86_64-efi",
                f"--efi-directory={mounts['esp']}",
                f"--boot-directory={mounts['boot']}",
                "--removable",
                "--no-nvram",
                "--recheck",
            ],
            timeout=300,
        )
        fallback_loader = mounts["esp"] / "EFI/BOOT/BOOTX64.EFI"
        if fallback_loader.is_symlink() or not fallback_loader.is_file():
            raise ValueError("GRUB no instaló el cargador UEFI de fallback.")
        update_grub = mounts["root"] / "usr/sbin/update-grub"
        if update_grub.is_file() and not update_grub.is_symlink():
            run_command(["chroot", str(mounts["root"]), "/usr/sbin/update-grub"], timeout=300)
    if shutil.which("sync"):
        run_command(["sync"], timeout=60, check=False)


def ensure_bios_boot(
    root: Path,
    device: str,
    partitions: list[dict[str, Any]],
    *,
    root_device_override: str | None = None,
) -> None:
    with mounted_target(
        root,
        device,
        partitions,
        "mbr",
        root_device_override=root_device_override,
    ) as mounts:
        grub = shutil.which("grub-install")
        if not grub:
            raise ValueError("Falta grub-install para preparar el arranque BIOS.")
        run_command(
            [
                "grub-install",
                "--target=i386-pc",
                f"--boot-directory={mounts['boot']}",
                "--recheck",
                device,
            ],
            timeout=300,
        )
        read_mbr_boot_sector(device)
        update_grub = mounts["root"] / "usr/sbin/update-grub"
        if update_grub.is_file() and not update_grub.is_symlink():
            run_command(["chroot", str(mounts["root"]), "/usr/sbin/update-grub"], timeout=300)
    if shutil.which("sync"):
        run_command(["sync"], timeout=60, check=False)


def restore_claimed_task(
    base: str,
    claim: dict[str, Any],
    task_token: str,
    ca_file: str | None,
    lease: LeaseHeartbeat,
    staging_dir: Path,
    key_provider: Callable[[], bytes] | None = None,
) -> bool:
    opened_mappings: list[str] = []
    try:
        return _restore_claimed_task(
            base,
            claim,
            task_token,
            ca_file,
            lease,
            staging_dir,
            opened_mappings=opened_mappings,
            key_provider=key_provider,
        )
    finally:
        for mapping_name in reversed(opened_mappings):
            with contextlib.suppress(OSError, ValueError, subprocess.SubprocessError):
                close_luks2(mapping_name)


def _restore_claimed_task(
    base: str,
    claim: dict[str, Any],
    task_token: str,
    ca_file: str | None,
    lease: LeaseHeartbeat,
    staging_dir: Path,
    *,
    opened_mappings: list[str],
    key_provider: Callable[[], bytes] | None,
) -> bool:
    operation = str(claim.get("operation", ""))
    if operation not in {"restore", "clone"}:
        raise ValueError("La operación recibida no es de restauración.")
    task_id = str(uuid.UUID(str(claim.get("task_id"))))
    image_id = str(uuid.UUID(str(claim.get("image_id"))))
    manifest_value = claim.get("manifest")
    if not isinstance(manifest_value, dict):
        raise ValueError("La tarea no contiene el manifiesto de la imagen.")
    # The initramfs advertises only tools that were staged by build-agent; the full profile is
    # therefore validated here before any target write.  Direct contract callers keep the
    # conservative MVP default of validate_restore_manifest().
    manifest = validate_restore_manifest(manifest_value, image_id, allow_extended=True)
    firmware_type = str(manifest["firmware"]["type"])
    partition_table = "gpt" if firmware_type == "uefi" else "mbr"
    target = claim.get("target")
    target_data = target if isinstance(target, dict) else {}
    selector = target_data.get("disk") or claim.get("disk")
    raw_selectors = target_data.get("disks") or claim.get("disks")
    if raw_selectors is None:
        raw_selectors = [selector]
    if not isinstance(raw_selectors, list) or any(
        not isinstance(item, dict) for item in raw_selectors
    ):
        raise ValueError("La tarea no contiene selectores de discos válidos.")
    layouts = manifest.get("disks") or [manifest["disk"]]
    if not isinstance(layouts, list) or len(layouts) != len(raw_selectors):
        raise ValueError("La cantidad de discos de la tarea no coincide con la imagen.")
    if len(layouts) > 1 and firmware_type == "bios":
        raise ValueError("La restauración multidisco BIOS requiere artefactos de boot separados.")
    if not isinstance(selector, dict):
        raise ValueError("La tarea no contiene el selector del disco destino.")
    if operation == "clone":
        hostname = str(target_data.get("hostname", ""))
        hostname = validate_clone_hostname(hostname)
        if selector.get("clone_hostname") != hostname:
            raise ValueError("El hostname de la reserva no coincide con el destino del clon.")
    else:
        hostname = ""
    document = block_inventory()
    selected_pairs: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for layout, disk_selector in zip(layouts, raw_selectors, strict=True):
        if not isinstance(layout, dict):
            raise ValueError("La imagen contiene un layout de disco inválido.")
        if disk_selector.get("operation") != operation:
            raise ValueError("La operación del selector no coincide con la tarea.")
        if len(layouts) > 1 and str(disk_selector.get("stable_id", "")).startswith("path:"):
            raise ValueError("Una restauración multidisco no puede depender del orden /dev.")
        source_disk_id = layout.get("disk_id") or layout.get("source_disk_id")
        if source_disk_id and disk_selector.get("source_disk_id") != source_disk_id:
            raise ValueError("El selector no corresponde al disco de origen reservado.")
        selected = select_disk(document, disk_selector)
        validate_restore_target(selected, disk_selector, manifest, disk=layout)
        assert_target_is_quiescent(selected)
        if (
            int(selected.get("size") or 0) > int(layout.get("size_bytes") or 0)
            and str((manifest.get("capabilities") or {}).get("volumes")) != "partitions"
        ):
            raise ValueError("La expansión de destinos no está disponible para LVM, RAID1 o LUKS2.")
        validate_target_expansion(selected, layout)
        selected_pairs.append((selected, disk_selector, layout))
    storage_plan = build_restore_storage_plan(manifest, selected_pairs)
    requires_expansion = any(
        int(selected.get("size") or 0) > int(disk.get("size_bytes") or 0)
        for selected, _selector, disk in selected_pairs
    )
    _require_restore_tools(manifest, storage_plan, requires_expansion=requires_expansion)
    luks_key: bytes | None = None
    if storage_plan.profile == "luks2":
        provider = key_provider or configured_luks2_key_provider(storage_plan.luks)  # type: ignore[arg-type]
        candidate = provider()
        if not isinstance(candidate, bytes) or not 1 <= len(candidate) <= 4096:
            raise ValueError("El proveedor LUKS2 no entregó una clave válida.")
        luks_key = candidate
    lease.check()
    artifacts = manifest["artifacts"]
    total = sum(int(artifact["size_bytes"]) for artifact in artifacts)
    sequence = 1
    sequence = post_progress(
        base,
        task_id,
        task_token,
        ca_file,
        sequence,
        phase="inspecting",
        processed=0,
        total=total,
        message=(
            f"Destino validado para {firmware_type.upper()}/{partition_table.upper()}; "
            f"{len(selected_pairs)} disco(s); "
            "todavía no se escribió ningún bloque."
        ),
    )
    artifact_files: dict[str, Path] = {}
    artifact_by_path = {str(artifact["path"]): artifact for artifact in artifacts}
    processed = 0
    for artifact in artifacts:
        lease.check()
        path = str(artifact["path"])
        destination = staging_path(staging_dir / task_id, path)
        download_artifact(
            f"{base}/api/v1/tasks/{task_id}/artifacts/{urllib.parse.quote(path, safe='/')}",
            destination,
            token=task_token,
            ca_file=ca_file,
            expected_size=int(artifact["size_bytes"]),
            expected_sha256=str(artifact["sha256"]),
            chunk_bytes=DEFAULT_CHUNK_BYTES,
            check_cancel=lease.check,
            block_manifest=artifact.get("blocks")
            if isinstance(artifact.get("blocks"), dict)
            else None,
        )
        artifact_files[path] = destination
        processed += int(artifact["size_bytes"])
        sequence = post_progress(
            base,
            task_id,
            task_token,
            ca_file,
            sequence,
            phase="downloading",
            processed=processed,
            total=total,
            message=f"Artefacto verificado: {path}.",
        )
    lease.check()
    for selected, _disk_selector, disk in selected_pairs:
        device = device_path(selected)
        restore_partition_table(device, disk)
        if firmware_type == "bios":
            boot_path = str(disk["boot_sector"]["path"])
            write_mbr_boot_code(artifact_files[boot_path], device)
    root_target: str | None = None
    root_outer_type: str | None = None
    if storage_plan.profile == "lvm-linear":
        first_device = device_path(selected_pairs[0][0])
        first_root = _root_partition(selected_pairs[0][2])
        root_target = create_lvm_linear_stack(
            storage_plan.lvm,  # type: ignore[arg-type]
            partition_device(first_device, int(first_root["number"])),
        )
        root_outer_type = "LVM2_member"
    elif storage_plan.profile == "luks2":
        first_device = device_path(selected_pairs[0][0])
        first_root = _root_partition(selected_pairs[0][2])
        if luks_key is None or storage_plan.luks is None:
            raise ValueError("No se pudo preparar la clave LUKS2 antes de escribir.")
        root_target, mapping_name = format_and_unlock_luks2(
            partition_device(first_device, int(first_root["number"])),
            storage_plan.luks,
            lambda: luks_key,
        )
        opened_mappings.append(mapping_name)
        root_outer_type = "crypto_LUKS"
    elif storage_plan.profile == "raid1":
        if storage_plan.raid is None:
            raise ValueError("No se pudo preparar el layout RAID1.")
        member_devices = [
            partition_device(device_path(selected), int(_root_partition(disk)["number"]))
            for selected, _selector, disk in selected_pairs
        ]
        member_ids = tuple(
            str(disk.get("disk_id") or disk.get("source_disk_id"))
            for _selected, _selector, disk in selected_pairs
        )
        root_target = create_raid1_stack(
            storage_plan.raid,
            member_devices,
            member_ids=member_ids,
        )
        root_outer_type = "linux_raid_member"
    sequence = post_progress(
        base,
        task_id,
        task_token,
        ca_file,
        sequence,
        phase="restoring",
        processed=0,
        total=total,
        message=f"Layout {partition_table.upper()} creado; restaurando particiones verificadas.",
    )
    processed = 0
    for disk_index, (selected, _disk_selector, disk) in enumerate(selected_pairs):
        device = device_path(selected)
        for partition in disk["partitions"]:
            lease.check()
            role = str(partition["role"])
            if role == "swap":
                initialize_swap(partition, device)
                sequence = post_progress(
                    base,
                    task_id,
                    task_token,
                    ca_file,
                    sequence,
                    phase="restoring",
                    processed=processed,
                    total=total,
                    message="Partición swap inicializada.",
                )
                continue
            path = str(partition["artifact"])
            if role == "root" and storage_plan.profile == "raid1" and disk_index != 0:
                sequence = post_progress(
                    base,
                    task_id,
                    task_token,
                    ca_file,
                    sequence,
                    phase="restoring",
                    processed=processed,
                    total=total,
                    message=(
                        "Artefacto raíz RAID1 verificado; se escribe una sola vez sobre el array."
                    ),
                )
                continue
            target_device = partition_device(device, int(partition["number"]))
            if role == "root" and root_target is not None:
                target_device = root_target
            restore_partition_artifact(
                artifact_files[path],
                str(artifact_by_path[path]["compression"]),
                target_device,
                role,
                str(partition.get("filesystem", "")),
            )
            processed += int(artifact_by_path[path]["size_bytes"])
            sequence = post_progress(
                base,
                task_id,
                task_token,
                ca_file,
                sequence,
                phase="restoring",
                processed=processed,
                total=total,
                message=f"Partición {role} restaurada.",
            )
        if storage_plan.profile == "partitions" and int(selected.get("size") or 0) > int(
            disk["size_bytes"]
        ):
            expand_gpt_root_partition(device, selected, disk)
        verify_restored_layout(
            device,
            disk,
            root_device_override=root_target if disk_index == 0 else None,
            root_outer_type=root_outer_type,
        )
    lease.check()
    sequence = post_progress(
        base,
        task_id,
        task_token,
        ca_file,
        sequence,
        phase="finalizing",
        processed=total,
        total=total,
        message=(
            f"Layout {partition_table.upper()} y sistemas de archivos verificados; "
            f"preparando arranque {firmware_type.upper()}."
        ),
    )
    first_selected, _first_selector, first_disk = selected_pairs[0]
    first_device = device_path(first_selected)
    if operation == "clone":
        with mounted_target(
            staging_dir / f"{task_id}.mount",
            first_device,
            first_disk["partitions"],
            partition_table,
            root_device_override=root_target,
        ) as mounts:
            customize_clone_identity(mounts["root"], hostname)
    for index, (selected, _disk_selector, disk) in enumerate(selected_pairs):
        boot_device = device_path(selected)
        boot_root = staging_dir / f"{task_id}.boot-{index}"
        if firmware_type == "uefi":
            ensure_uefi_boot(
                boot_root,
                boot_device,
                disk["partitions"],
                root_device_override=root_target,
            )
        else:
            ensure_bios_boot(
                boot_root,
                boot_device,
                disk["partitions"],
                root_device_override=root_target,
            )
    lease.check()
    sequence = post_progress(
        base,
        task_id,
        task_token,
        ca_file,
        sequence,
        phase="verifying",
        processed=total,
        total=total,
        message=(
            f"Arranque {firmware_type.upper()} listo; el servidor recibirá la confirmación "
            "antes de reiniciar."
        ),
    )
    json_request(
        f"{base}/api/v1/tasks/{task_id}/result",
        method="POST",
        payload={"sequence": sequence, "success": True},
        token=task_token,
        ca_file=ca_file,
        expected_status={200},
    )
    with contextlib.suppress(OSError):
        shutil.rmtree(staging_dir / task_id)
        shutil.rmtree(staging_dir / f"{task_id}.mount")
        for index in range(len(selected_pairs)):
            shutil.rmtree(staging_dir / f"{task_id}.boot-{index}")
    return True


def capture_task(
    server: str, host_id: str, token: str, ca_file: str | None, staging_dir: Path | None = None
) -> bool:
    validate_server(server)
    valid_host_id = str(uuid.UUID(host_id))
    base = server.rstrip("/")
    claim = json_request(
        f"{base}/api/v1/tasks/claim",
        method="POST",
        payload={
            "protocol_version": 1,
            "host_id": valid_host_id,
            "session_id": str(uuid.uuid4()),
            "capabilities": local_agent_capabilities(),
        },
        token=token,
        ca_file=ca_file,
        expected_status={200},
    )
    if claim.get("task") is None and "task_id" not in claim:
        return False
    task_id = claim.get("task_id")
    task_token = claim.get("task_token")
    if not isinstance(task_id, str) or not isinstance(task_token, str):
        raise ValueError("El servidor entregó una tarea incompleta.")
    validate_token(task_token)
    lease_seconds = int(claim.get("lease_seconds") or 90)
    heartbeat_seconds = int(claim.get("heartbeat_seconds") or 20)
    heartbeat_interval = max(1, min(heartbeat_seconds, max(1, lease_seconds // 2)))
    lease = LeaseHeartbeat(base, task_id, task_token, ca_file, heartbeat_interval)
    lease.start()
    sequence = 1
    frozen_mounts: list[str] = []
    opened_mappings: list[str] = []
    try:
        if claim.get("operation") in {"restore", "clone"}:
            return restore_claimed_task(
                base,
                claim,
                task_token,
                ca_file,
                lease,
                staging_dir or Path("/run/pyfog/staging"),
            )
        document = block_inventory()
        lease.check()
        raw_selectors = claim.get("disks") or [claim.get("disk")]
        if not isinstance(raw_selectors, list) or any(
            not isinstance(item, dict) for item in raw_selectors
        ):
            _raise_capture_error("La tarea no contiene selectores de disco válidos.")
        if len(raw_selectors) > 1 and any(
            str(item.get("stable_id", "")).startswith("path:") for item in raw_selectors
        ):
            _raise_capture_error("Una captura multidisco no puede depender del orden /dev.")
        selected_geometries: list[
            tuple[dict[str, Any], dict[str, Any], dict[str, Any], bytes | None]
        ] = []
        partition_table = ""
        for selector in raw_selectors:
            selected = select_disk(document, selector)
            selected["stable_id"] = str(selector.get("stable_id") or "")
            device = device_path(selected)
            detected_table = str(selected.get("pttype") or "").lower()
            if not detected_table:
                detected_table = parse_export(
                    run_command(["blkid", "-o", "export", device], check=False)
                ).get("PTTYPE", "gpt")
            if detected_table in {"dos", "mbr"}:
                detected_table = "mbr"
                geometry = parse_mbr(device, selected, inventory=document)
            elif detected_table == "gpt":
                geometry = parse_gpt(device, selected, inventory=document)
            else:
                _raise_capture_error("El disco no contiene una tabla GPT o MBR admitida.")
            if partition_table and detected_table != partition_table:
                _raise_capture_error("Los discos de una imagen multidisco deben compartir layout.")
            partition_table = detected_table
            boot_code = (
                validate_mbr_boot_sector(device, geometry) if detected_table == "mbr" else None
            )
            selected_geometries.append((selected, selector, geometry, boot_code))
        prepare_capture_storage(selected_geometries, opened_mappings)
        if len(selected_geometries) > 1 and partition_table == "mbr":
            _raise_capture_error("La captura multidisco BIOS requiere boot sector por disco.")
        raw_storage_profiles = [
            geometry.get("storage")
            for _selected, _selector, geometry, _boot in selected_geometries
            if geometry.get("storage") is not None
        ]
        storage_profiles = [
            profile for profile in raw_storage_profiles if isinstance(profile, dict)
        ]
        profile_names = {str(profile.get("profile")) for profile in storage_profiles}
        if len(profile_names) > 1 or len(storage_profiles) != len(raw_storage_profiles):
            _raise_capture_error(
                "Los discos de una captura no pueden mezclar perfiles de almacenamiento."
            )
        if profile_names == {"raid1"}:
            if len(storage_profiles) != len(selected_geometries) or len(selected_geometries) < 2:
                _raise_capture_error("La captura RAID1 requiere todos sus discos miembros.")
            first_array = storage_profiles[0].get("raid_array")
            if not isinstance(first_array, dict):
                _raise_capture_error("La captura RAID1 no contiene metadata de array.")
            member_ids = tuple(
                str(selector.get("stable_id") or "")
                for _selected, selector, _geometry, _boot in selected_geometries
            )
            if any(not member or member.startswith("path:") for member in member_ids):
                _raise_capture_error("RAID1 requiere identidades estables para todos sus discos.")
            storage_profile = {
                "profile": "raid1",
                "raid_array": {
                    **first_array,
                    "member_ids": list(member_ids),
                },
            }
            for profile in storage_profiles[1:]:
                array = profile.get("raid_array")
                if not isinstance(array, dict) or any(
                    array.get(key) != first_array.get(key)
                    for key in ("uuid", "metadata", "size_bytes")
                ):
                    _raise_capture_error("Los discos seleccionados no pertenecen al mismo RAID1.")
        elif storage_profiles:
            if len(storage_profiles) != 1 or len(selected_geometries) != 1:
                _raise_capture_error("LVM y LUKS2 requieren un único disco de origen.")
            storage_profile = storage_profiles[0]
        else:
            storage_profile = None
        volume_profile = (
            str(storage_profile.get("profile")) if storage_profile is not None else "partitions"
        )
        encryption_profile = (
            "luks2" if storage_profile and storage_profile.get("profile") == "luks2" else "none"
        )
        hot_capture = any(item[1].get("consistency") == "hot" for item in selected_geometries)
        if hot_capture and any(item[1].get("consistency") != "hot" for item in selected_geometries):
            _raise_capture_error(
                "Todos los discos de una captura multidisco deben usar el mismo modo."
            )
        if hot_capture:
            sequence = post_progress(
                base,
                task_id,
                task_token,
                ca_file,
                sequence,
                phase="freezing",
                processed=0,
                total=sum(int(item[0]["size"]) for item in selected_geometries),
                message="Preparando fsfreeze; no se transferirá ningún bloque todavía.",
            )
            mounts = [
                mount
                for _selected, _selector, geometry, _boot in selected_geometries
                for mount in mounted_filesystems(geometry["partitions"])
            ]
            frozen_mounts = freeze_filesystems(sorted(set(mounts)))
            sequence = post_progress(
                base,
                task_id,
                task_token,
                ca_file,
                sequence,
                phase="capturing",
                processed=0,
                total=sum(int(item[0]["size"]) for item in selected_geometries),
                message="Filesystem congelado; iniciando captura consistente.",
            )
        else:
            for _selected, _selector, geometry, _boot in selected_geometries:
                assert_disk_is_quiescent(geometry["partitions"])
        total = sum(int(item[0]["size"]) for item in selected_geometries)
        sequence = post_progress(
            base,
            task_id,
            task_token,
            ca_file,
            sequence,
            phase="capturing" if hot_capture else "inspecting",
            processed=0,
            total=total,
            message="Disco validado en modo de solo lectura.",
        )
        artifacts: list[dict[str, Any]] = []
        disk_payloads: list[dict[str, Any]] = []
        commands: set[str] = set()
        processed_total = 0
        negotiated_chunk = min(
            DEFAULT_CHUNK_BYTES, int(claim.get("chunk_bytes") or DEFAULT_CHUNK_BYTES)
        )
        captured_artifacts: dict[str, dict[str, Any]] = {}
        for disk_index, (_selected, selector, geometry, boot_code) in enumerate(
            selected_geometries
        ):
            disk_prefix = "" if len(selected_geometries) == 1 else f"disk-{disk_index + 1:02d}-"
            boot_artifact: dict[str, Any] | None = None
            if boot_code is not None:
                boot_path = "boot-sector.bin"
                upload_chunk(
                    f"{base}/api/v1/tasks/{task_id}/artifacts/{boot_path}",
                    boot_code,
                    token=task_token,
                    ca_file=ca_file,
                    index=0,
                    offset=0,
                    chunk_size=negotiated_chunk,
                )
                boot_block = TransferBlock(
                    index=0,
                    offset=0,
                    size=len(boot_code),
                    sha256=hashlib.sha256(boot_code).hexdigest(),
                )
                boot_transfer = TransferManifest(
                    size_bytes=len(boot_code),
                    block_size=len(boot_code),
                    blocks=[boot_block],
                )
                boot_artifact = {
                    "path": boot_path,
                    "size_bytes": len(boot_code),
                    "compression": "none",
                    "sha256": hashlib.sha256(boot_code).hexdigest(),
                    "blocks": boot_transfer.model_dump(mode="json"),
                }
                artifacts.append(boot_artifact)
                commands.add("mbr")
                processed_total += len(boot_code)
            partition_artifacts: list[dict[str, Any]] = []
            for partition in geometry["partitions"]:
                if partition["role"] == "swap":
                    continue
                command = filesystem_tool(str(partition["filesystem"]))
                commands.add(command)
                if volume_profile == "raid1" and partition["role"] == "root":
                    path = "partitions/raid1-root.partclone.gz"
                    cached = captured_artifacts.get(path)
                    if cached is not None:
                        partition_artifacts.append(cached)
                        continue
                else:
                    path = (
                        f"partitions/{disk_prefix}{partition['number']:02d}-"
                        f"{partition['role']}.partclone.gz"
                    )
                processed_partition = 0
                digest = hashlib.sha256()
                block_entries: list[TransferBlock] = []
                for chunk_index, chunk in enumerate(
                    capture_stream(
                        [
                            command,
                            "-c",
                            "-s",
                            str(partition.get("capture_device") or partition["device"]),
                            "-o",
                            "-",
                        ],
                        chunk_bytes=negotiated_chunk,
                    )
                ):
                    digest.update(chunk)
                    block_entries.append(
                        TransferBlock(
                            index=chunk_index,
                            offset=processed_partition,
                            size=len(chunk),
                            sha256=hashlib.sha256(chunk).hexdigest(),
                        )
                    )
                    upload_chunk(
                        f"{base}/api/v1/tasks/{task_id}/artifacts/{path}",
                        chunk,
                        token=task_token,
                        ca_file=ca_file,
                        index=chunk_index,
                        offset=processed_partition,
                        chunk_size=negotiated_chunk,
                    )
                    lease.check()
                    processed_partition += len(chunk)
                    processed_total += len(chunk)
                    if (
                        processed_partition == len(chunk)
                        or processed_partition % (8 * DEFAULT_CHUNK_BYTES) == 0
                    ):
                        sequence = post_progress(
                            base,
                            task_id,
                            task_token,
                            ca_file,
                            sequence,
                            phase="uploading",
                            processed=min(total, processed_total),
                            total=total,
                            message=f"Transfiriendo {path}.",
                        )
                transfer = TransferManifest(
                    size_bytes=processed_partition,
                    block_size=negotiated_chunk,
                    blocks=block_entries,
                )
                artifact = {
                    "path": path,
                    "size_bytes": processed_partition,
                    "compression": "gzip",
                    "sha256": digest.hexdigest(),
                    "blocks": transfer.model_dump(mode="json"),
                }
                partition_artifacts.append(artifact)
                artifacts.append(artifact)
                captured_artifacts[path] = artifact
            partition_payload = [
                {
                    key: value
                    for key, value in partition.items()
                    if key not in {"device", "capture_device", "outer_filesystem", "code", "name"}
                }
                for partition in geometry["partitions"]
            ]
            for partition, artifact in zip(
                [part for part in partition_payload if part["role"] != "swap"],
                partition_artifacts,
                strict=True,
            ):
                partition["artifact"] = artifact["path"]
            for partition in partition_payload:
                partition.setdefault("artifact", None)
            stable_id = str(selector.get("stable_id") or "")
            if not stable_id:
                _raise_capture_error("El selector de captura no contiene una identidad de disco.")
            disk_payload: dict[str, Any] = {
                **{key: geometry[key] for key in geometry if key not in {"partitions", "storage"}},
                "disk_id": stable_id,
                "source_disk_id": stable_id,
                "partitions": partition_payload,
            }
            if boot_artifact is not None:
                disk_payload["boot_sector"] = boot_artifact
            disk_payloads.append(disk_payload)
        source = claim.get("source")
        system = claim.get("system")
        firmware = (
            {"type": "bios", "secure_boot": False}
            if partition_table == "mbr"
            else {"type": "uefi", "secure_boot": False}
        )
        manifest = {
            "format": "pyfog-disk-image",
            "format_version": 2,
            "image_id": claim["image_id"],
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "checksum_algorithm": "sha256",
            "source": source,
            "system": system,
            "architecture": "x86_64",
            "firmware": firmware,
            "capabilities": {
                "firmware": firmware,
                "partition_table": partition_table,
                "disks": len(disk_payloads),
                "filesystems": sorted(
                    {
                        part["filesystem"]
                        for disk_payload in disk_payloads
                        for part in disk_payload["partitions"]
                    }
                ),
                "encryption": encryption_profile,
                "volumes": volume_profile,
            },
            "disk": disk_payloads[0],
            "tool": {
                "name": "partclone",
                "version": command_version(),
                "commands": sorted(commands),
            },
            "artifacts": artifacts,
            "publishable": True,
        }
        if len(disk_payloads) > 1:
            manifest["disks"] = disk_payloads
        if storage_profile:
            if volume_profile == "lvm-linear":
                manifest["volumes"] = storage_profile["volumes"]
            elif volume_profile == "raid1":
                manifest["raid_arrays"] = [storage_profile["raid_array"]]
            elif encryption_profile != "none":
                manifest["encryption"] = storage_profile["encryption"]
        if frozen_mounts:
            sequence = post_progress(
                base,
                task_id,
                task_token,
                ca_file,
                sequence,
                phase="thawing",
                processed=processed_total,
                total=total,
                message="Liberando fsfreeze antes de publicar la captura.",
            )
            thaw_filesystems(frozen_mounts)
            frozen_mounts = []
        sequence = post_progress(
            base,
            task_id,
            task_token,
            ca_file,
            sequence,
            phase="verifying",
            processed=total,
            total=total,
            message="Artefactos transferidos; verificando la publicación.",
        )
        lease.check()
        json_request(
            f"{base}/api/v1/tasks/{task_id}/result",
            method="POST",
            payload={"sequence": sequence, "success": True, "manifest": manifest},
            token=task_token,
            ca_file=ca_file,
            expected_status={200},
        )
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        with contextlib.suppress(OSError, ValueError, urllib.error.URLError):
            json_request(
                f"{base}/api/v1/tasks/{task_id}/result",
                method="POST",
                payload={
                    "sequence": sequence,
                    "success": False,
                    "cancelled": isinstance(error, TaskCancelledError),
                    "error": str(error)[:500],
                },
                token=task_token,
                ca_file=ca_file,
                expected_status={200},
            )
        raise
    else:
        return True
    finally:
        if frozen_mounts:
            with contextlib.suppress(OSError, ValueError, subprocess.SubprocessError):
                thaw_filesystems(frozen_mounts)
        for mapping_name in reversed(opened_mappings):
            with contextlib.suppress(OSError, ValueError, subprocess.SubprocessError):
                close_luks2(mapping_name)
        lease.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True)
    parser.add_argument("--host-id", required=True)
    parser.add_argument("--ca-file")
    parser.add_argument("--staging-dir", default="/run/pyfog/staging")
    args = parser.parse_args()
    token = os.environ.get("PYFOG_AGENT_TOKEN") or os.environ.get("PYFOG_INVENTORY_TOKEN", "")
    validate_token(token)
    if not capture_task(args.server, args.host_id, token, args.ca_file, Path(args.staging_dir)):
        raise SystemExit(NO_TASK_EXIT)


if __name__ == "__main__":
    main()
