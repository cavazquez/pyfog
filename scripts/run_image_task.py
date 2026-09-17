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
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any

MAX_RESPONSE_BYTES = 1_000_000
DEFAULT_CHUNK_BYTES = 512 * 1024
NO_TASK_EXIT = 3
MAX_IMAGE_BYTES = 2**50


class TaskCancelledError(ValueError):
    """The coordinator asked the agent to stop before the next destructive step."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward a task capability to an unexpected destination."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
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


def response_json(response: Any) -> dict[str, Any]:
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


def run_command(arguments: list[str], *, timeout: int = 30, check: bool = True) -> str:
    executable = shutil.which(arguments[0])
    if not executable:
        raise ValueError(f"Falta la herramienta {arguments[0]} en el agente.")
    result = subprocess.run(  # noqa: S603 - arguments are built from validated device metadata
        [executable, *arguments[1:]],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
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


def block_inventory() -> dict[str, Any]:
    output = run_command(
        [
            "lsblk",
            "--json",
            "--bytes",
            "--paths",
            "--output",
            "NAME,KNAME,PATH,TYPE,SIZE,MODEL,SERIAL,WWN,TRAN,LOG-SEC,RM,FSTYPE,UUID,PARTUUID,MOUNTPOINTS",
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


def manifest_int(value: Any, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum or value > MAX_IMAGE_BYTES:
        raise ValueError(f"{label} no es un entero seguro.")
    return value


def manifest_uuid(value: Any, label: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise ValueError(f"{label} no es un UUID válido.") from None


def validate_restore_manifest(manifest: dict[str, Any], image_id: str) -> dict[str, Any]:
    """Validate the complete safety-critical v1 manifest inside the initramfs."""

    if manifest.get("format") != "pyfog-disk-image" or manifest.get("format_version") != 1:
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
        or firmware.get("type") != "uefi"
        or firmware.get("secure_boot") is not False
    ):
        raise ValueError("La imagen requiere firmware UEFI sin Secure Boot.")
    system = manifest.get("system")
    if not isinstance(system, dict) or str(system.get("id", "")).lower() != "ubuntu":
        raise ValueError("El agente sólo puede restaurar imágenes Ubuntu v1.")
    tool = manifest.get("tool")
    if not isinstance(tool, dict) or tool.get("name") != "partclone":
        raise ValueError("El manifiesto no identifica Partclone.")
    commands = tool.get("commands")
    if not isinstance(commands, list) or not {"partclone.ext4", "partclone.fat"}.issubset(commands):
        raise ValueError("El manifiesto no contiene los comandos Partclone requeridos.")
    disk = manifest.get("disk")
    if not isinstance(disk, dict):
        raise ValueError("El manifiesto no contiene la geometría del disco.")
    size = manifest_int(disk.get("size_bytes"), "La capacidad de la imagen", minimum=1)
    sector = disk.get("logical_sector_bytes")
    if type(sector) is not int or sector not in {512, 4096}:
        raise ValueError("El sector lógico de la imagen no es válido.")
    sectors = manifest_int(disk.get("sector_count"), "La cantidad de sectores", minimum=1)
    first = manifest_int(disk.get("first_usable_sector"), "El primer sector GPT")
    last = manifest_int(disk.get("last_usable_sector"), "El último sector GPT")
    if size != sector * sectors or first >= last or last >= sectors:
        raise ValueError("La geometría de la imagen no es segura.")
    manifest_uuid(disk.get("gpt_disk_guid"), "El GUID GPT")
    partitions = disk.get("partitions")
    artifacts = manifest.get("artifacts")
    if not isinstance(partitions, list) or not isinstance(artifacts, list):
        raise ValueError("El manifiesto no contiene particiones y artefactos.")
    if len(partitions) < 2 or len(partitions) > 4 or len(artifacts) < 2 or len(artifacts) > 4:
        raise ValueError("La cantidad de particiones o artefactos no es válida.")
    artifact_paths: set[str] = set()
    artifact_metadata: dict[str, dict[str, Any]] = {}
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise ValueError("El manifiesto contiene un artefacto inválido.")
        path = artifact.get("path")
        if not isinstance(path, str):
            raise ValueError("La ruta de un artefacto no es válida.")
        safe_artifact_path(path)
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
        artifact_metadata[path] = {
            "size_bytes": artifact_size,
            "compression": artifact["compression"],
        }
    referenced: set[str] = set()
    numbers: set[int] = set()
    partition_guids: set[str] = set()
    filesystem_uuids: set[str] = set()
    spans: list[tuple[int, int]] = []
    roles: list[str] = []
    expected_filesystems = {
        "esp": ("fat32", "/boot/efi"),
        "boot": ("ext4", "/boot"),
        "root": ("ext4", "/"),
        "swap": ("swap", None),
    }
    for partition in partitions:
        if not isinstance(partition, dict):
            raise ValueError("El manifiesto contiene una partición inválida.")
        number = manifest_int(partition.get("number"), "El número de partición", minimum=1)
        if number > 128 or number in numbers:
            raise ValueError("El GPT contiene números de partición inválidos o repetidos.")
        numbers.add(number)
        role = partition.get("role")
        if role not in expected_filesystems:
            raise ValueError("La partición contiene un rol no admitido.")
        roles.append(role)
        filesystem, mountpoint = expected_filesystems[role]
        if partition.get("filesystem") != filesystem or partition.get("mountpoint") != mountpoint:
            raise ValueError(f"La partición {role} no coincide con su sistema de archivos.")
        partition_guid = manifest_uuid(partition.get("partition_guid"), "El GUID de partición")
        if partition_guid in partition_guids:
            raise ValueError("El GPT contiene GUIDs de partición repetidos.")
        partition_guids.add(partition_guid)
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
            raise ValueError("El GPT contiene UUIDs de filesystem repetidos.")
        filesystem_uuids.add(filesystem_key)
        start = manifest_int(partition.get("start_sector"), "El inicio de partición")
        count = manifest_int(partition.get("size_sectors"), "El tamaño de partición", minimum=1)
        end = start + count - 1
        if start < first or end > last:
            raise ValueError("Una partición queda fuera del rango GPT seguro.")
        spans.append((start, end))
        artifact = partition.get("artifact")
        if role == "swap":
            if artifact is not None:
                raise ValueError("La partición swap no puede tener un artefacto.")
        else:
            if not isinstance(artifact, str):
                raise ValueError(f"La partición {role} no tiene artefacto.")
            safe_artifact_path(artifact)
            referenced.add(artifact)
    ordered_spans = sorted(spans)
    previous_end = first - 1
    for start, end in ordered_spans:
        if start <= previous_end:
            raise ValueError("Las particiones GPT se superponen.")
        previous_end = end
    if (
        roles.count("esp") != 1
        or roles.count("root") != 1
        or roles.count("boot") > 1
        or roles.count("swap") > 1
    ):
        raise ValueError("El GPT no tiene una combinación de roles válida.")
    if referenced != artifact_paths or set(artifact_metadata) != artifact_paths:
        raise ValueError("El manifiesto no tiene referencias de artefactos consistentes.")
    return manifest


def safe_artifact_path(value: str) -> str:
    if (
        not value
        or not value.isascii()
        or "\\" in value
        or value.startswith("/")
        or value.endswith("/")
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", value)
        or not value.startswith("partitions/")
    ):
        raise ValueError("La ruta del artefacto no es segura.")
    return value


def validate_restore_target(
    selected: dict[str, Any], selector: dict[str, Any], manifest: dict[str, Any]
) -> None:
    """Check every target property again immediately before the first write."""

    disk = manifest["disk"]
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
    if not Path("/sys/firmware/efi").is_dir():
        raise ValueError("El agente debe arrancar en firmware UEFI para esta imagen.")


def parse_gpt(device: str, selected: dict[str, Any]) -> dict[str, Any]:
    output = run_command(["sgdisk", "--print", device])
    guid_match = re.search(r"Disk identifier \(GUID\):\s*([0-9A-Fa-f-]{36})", output)
    first_match = re.search(r"First usable sector is\s+(\d+)", output)
    last_match = re.search(r"last usable sector is\s+(\d+)", output, re.IGNORECASE)
    if not guid_match or not first_match or not last_match:
        raise ValueError("No se pudo leer la geometría GPT del disco.")
    partitions: list[dict[str, Any]] = []
    for line in output.splitlines():
        match = re.match(
            r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+.*?\s+([0-9A-Fa-f]{4})\s+(.*)$", line
        )
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
        if code == "EF00":
            role, expected_fs, mountpoint = "esp", "fat32", "/boot/efi"
            if filesystem == "vfat":
                filesystem = "fat32"
        elif code == "8200":
            role, expected_fs, mountpoint = "swap", "swap", None
        elif filesystem == "ext4" and "boot" in partition["name"].lower():
            role, expected_fs, mountpoint = "boot", "ext4", "/boot"
        elif filesystem == "ext4":
            role, expected_fs, mountpoint = "root", "ext4", "/"
        else:
            raise ValueError(f"La partición {number} no pertenece a la matriz Linux admitida.")
        if filesystem != expected_fs or not filesystem_uuid or not part_guid:
            raise ValueError(f"La partición {number} no tiene filesystem y UUID compatibles.")
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
            }
        )
    roles = [partition["role"] for partition in result]
    if roles.count("esp") != 1 or roles.count("root") != 1:
        raise ValueError("El disco debe tener una ESP y una raíz ext4 únicas.")
    return {
        "size_bytes": int(selected["size"]),
        "logical_sector_bytes": int(selected["log-sec"]),
        "sector_count": int(selected["size"]) // int(selected["log-sec"]),
        "gpt_disk_guid": guid_match[1],
        "first_usable_sector": int(first_match[1]),
        "last_usable_sector": int(last_match[1]),
        "partitions": result,
    }


def assert_disk_is_quiescent(partitions: list[dict[str, Any]]) -> None:
    devices = {
        str(partition["device"])
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
        swap_devices = {
            line.split()[0]
            for line in content.splitlines()[1:]
            if line.split()
        }
        if devices & swap_devices:
            raise ValueError("El disco tiene swap activo y no se puede capturar.")


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

    def __init__(self, base: str, task_id: str, token: str, ca_file: str | None, interval: int):
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
    safe = safe_artifact_path(relative)
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
    check_cancel: Any,
) -> None:
    """Stream one published artifact to staging and verify it without buffering the image."""

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


def role_code(role: str) -> str:
    try:
        return {"esp": "EF00", "boot": "8300", "root": "8300", "swap": "8200"}[role]
    except KeyError:
        raise ValueError("La imagen contiene un rol de partición no admitido.") from None


def restore_partition_table(device: str, disk: dict[str, Any]) -> None:
    """Recreate only the manifest layout; the caller must validate the target first."""

    partitions = disk["partitions"]
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


def restore_partition_artifact(
    artifact: Path, compression: str, partition: str, role: str, *, timeout: int = 3600
) -> None:
    command = "partclone.fat" if role == "esp" else "partclone.ext4"
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


def verify_restored_layout(device: str, disk: dict[str, Any]) -> None:
    if shutil.which("partprobe"):
        run_command(["partprobe", device], timeout=60, check=False)
    run_command(["sgdisk", "--verify", device], timeout=120)
    expected_types = {
        "esp": {"vfat", "fat32"},
        "boot": {"ext4"},
        "root": {"ext4"},
        "swap": {"swap"},
    }
    for partition in disk["partitions"]:
        path = partition_device(device, int(partition["number"]))
        values = parse_export(run_command(["blkid", "-o", "export", path]))
        filesystem = values.get("TYPE", "").lower()
        if filesystem not in expected_types[str(partition["role"])]:
            raise ValueError(f"La partición {partition['role']} no tiene el filesystem esperado.")
        if values.get("UUID", "").lower() != str(partition["filesystem_uuid"]).lower():
            raise ValueError(f"El UUID de la partición {partition['role']} no coincide.")


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
    root: Path, device: str, partitions: list[dict[str, Any]]
) -> Iterator[dict[str, Path]]:
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    by_role = {str(partition["role"]): partition for partition in partitions}
    root_device = partition_device(device, int(by_role["root"]["number"]))
    root_mount = root / "root"
    esp_mount = root_mount / "boot/efi"
    boot_mount = root_mount / "boot"
    mounted: list[Path] = []
    try:
        root_mount.mkdir(parents=True, mode=0o755, exist_ok=True)
        run_command(["mount", root_device, str(root_mount)], timeout=60)
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
        esp_mount.mkdir(parents=True, mode=0o755, exist_ok=True)
        run_command(
            ["mount", partition_device(device, int(by_role["esp"]["number"])), str(esp_mount)],
            timeout=60,
        )
        mounted.append(esp_mount)
        yield {"root": root_mount, "esp": esp_mount, "boot": boot_mount}
    finally:
        for mountpoint in reversed(mounted):
            run_command(["umount", "--", str(mountpoint)], timeout=60, check=False)


def ensure_uefi_boot(root: Path, device: str, partitions: list[dict[str, Any]]) -> None:
    with mounted_target(root, device, partitions) as mounts:
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


def restore_claimed_task(
    base: str,
    claim: dict[str, Any],
    task_token: str,
    ca_file: str | None,
    lease: LeaseHeartbeat,
    staging_dir: Path,
) -> bool:
    operation = str(claim.get("operation", ""))
    if operation not in {"restore", "clone"}:
        raise ValueError("La operación recibida no es de restauración.")
    task_id = str(uuid.UUID(str(claim.get("task_id"))))
    image_id = str(uuid.UUID(str(claim.get("image_id"))))
    manifest_value = claim.get("manifest")
    if not isinstance(manifest_value, dict):
        raise ValueError("La tarea no contiene el manifiesto de la imagen.")
    manifest = validate_restore_manifest(manifest_value, image_id)
    target = claim.get("target")
    selector = target.get("disk") if isinstance(target, dict) else claim.get("disk")
    if not isinstance(selector, dict):
        raise ValueError("La tarea no contiene el selector del disco destino.")
    if operation == "clone":
        hostname = str(target.get("hostname", "")) if isinstance(target, dict) else ""
        hostname = validate_clone_hostname(hostname)
        if selector.get("clone_hostname") != hostname:
            raise ValueError("El hostname de la reserva no coincide con el destino del clon.")
    else:
        hostname = ""
    if selector.get("operation") != operation:
        raise ValueError("La operación del selector no coincide con la tarea.")
    document = block_inventory()
    selected = select_disk(document, selector)
    validate_restore_target(selected, selector, manifest)
    assert_target_is_quiescent(selected)
    lease.check()
    disk = manifest["disk"]
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
        message="Destino validado en modo UEFI; todavía no se escribió ningún bloque.",
    )
    artifact_files: dict[str, Path] = {}
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
    device = device_path(selected)
    restore_partition_table(device, disk)
    sequence = post_progress(
        base,
        task_id,
        task_token,
        ca_file,
        sequence,
        phase="restoring",
        processed=0,
        total=total,
        message="Layout GPT creado; restaurando particiones verificadas.",
    )
    processed = 0
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
        restore_partition_artifact(
            artifact_files[path],
            next(str(item["compression"]) for item in artifacts if item["path"] == path),
            partition_device(device, int(partition["number"])),
            role,
        )
        processed += next(int(item["size_bytes"]) for item in artifacts if item["path"] == path)
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
    verify_restored_layout(device, disk)
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
        message="Layout y sistemas de archivos verificados; preparando UEFI.",
    )
    if operation == "clone":
        with mounted_target(staging_dir / f"{task_id}.mount", device, disk["partitions"]) as mounts:
            customize_clone_identity(mounts["root"], hostname)
    ensure_uefi_boot(staging_dir / f"{task_id}.boot", device, disk["partitions"])
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
        message="Arranque UEFI listo; el servidor recibirá la confirmación antes de reiniciar.",
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
        shutil.rmtree(staging_dir / f"{task_id}.boot")
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
            "capabilities": [
                "gpt",
                "partclone.ext4",
                "partclone.fat",
                "restore",
                "clone",
                "identity",
            ],
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
        selector = claim.get("disk")
        if not isinstance(selector, dict):
            raise ValueError("La tarea no contiene un selector de disco.")
        selected = select_disk(document, selector)
        device = device_path(selected)
        geometry = parse_gpt(device, selected)
        assert_disk_is_quiescent(geometry["partitions"])
        total = int(selected["size"])
        sequence = post_progress(
            base,
            task_id,
            task_token,
            ca_file,
            sequence,
            phase="inspecting",
            processed=0,
            total=total,
            message="Disco validado en modo de solo lectura.",
        )
        artifacts: list[dict[str, Any]] = []
        commands: set[str] = set()
        processed_total = 0
        for partition in geometry["partitions"]:
            if partition["role"] == "swap":
                continue
            command = "partclone.fat" if partition["role"] == "esp" else "partclone.ext4"
            commands.add(command)
            path = f"partitions/{partition['number']:02d}-{partition['role']}.partclone.gz"
            processed_partition = 0
            digest = hashlib.sha256()
            for chunk_index, chunk in enumerate(
                capture_stream(
                    [command, "-c", "-s", partition["device"], "-o", "-"],
                    chunk_bytes=min(
                        DEFAULT_CHUNK_BYTES, int(claim.get("chunk_bytes") or DEFAULT_CHUNK_BYTES)
                    ),
                )
            ):
                digest.update(chunk)
                upload_chunk(
                    f"{base}/api/v1/tasks/{task_id}/artifacts/{path}",
                    chunk,
                    token=task_token,
                    ca_file=ca_file,
                    index=chunk_index,
                    offset=processed_partition,
                    chunk_size=int(claim.get("chunk_bytes") or DEFAULT_CHUNK_BYTES),
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
            artifacts.append(
                {
                    "path": path,
                    "size_bytes": processed_partition,
                    "compression": "gzip",
                    "sha256": digest.hexdigest(),
                }
            )
        partition_payload = [
            {
                key: value
                for key, value in partition.items()
                if key != "device" and key != "code" and key != "name"
            }
            for partition in geometry["partitions"]
        ]
        for partition, artifact in zip(
            [part for part in partition_payload if part["role"] != "swap"], artifacts, strict=True
        ):
            partition["artifact"] = artifact["path"]
        for partition in partition_payload:
            partition.setdefault("artifact", None)
        source = claim.get("source")
        system = claim.get("system")
        manifest = {
            "format": "pyfog-disk-image",
            "format_version": 1,
            "image_id": claim["image_id"],
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "checksum_algorithm": "sha256",
            "source": source,
            "system": system,
            "architecture": "x86_64",
            "firmware": {"type": "uefi", "secure_boot": False},
            "disk": {
                **{key: geometry[key] for key in geometry if key != "partitions"},
                "partitions": partition_payload,
            },
            "tool": {
                "name": "partclone",
                "version": command_version(),
                "commands": sorted(commands),
            },
            "artifacts": artifacts,
            "publishable": True,
        }
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
        return True
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
    finally:
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
    if not capture_task(
        args.server, args.host_id, token, args.ca_file, Path(args.staging_dir)
    ):
        raise SystemExit(NO_TASK_EXIT)


if __name__ == "__main__":
    main()
