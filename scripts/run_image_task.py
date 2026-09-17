"""Claim and execute one PyFog Linux capture task from the ephemeral agent."""

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
from pathlib import Path
from typing import Any

MAX_RESPONSE_BYTES = 1_000_000
DEFAULT_CHUNK_BYTES = 512 * 1024
NO_TASK_EXIT = 3


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


def parse_gpt(device: str, selected: dict[str, Any]) -> dict[str, Any]:
    output = run_command(["sgdisk", "--print", device])
    guid_match = re.search(r"Disk identifier \(GUID\):\s*([0-9A-Fa-f-]{36})", output)
    first_match = re.search(r"First usable sector is\s+(\d+)", output)
    last_match = re.search(r"Last usable sector is\s+(\d+)", output)
    if not guid_match or not first_match or not last_match:
        raise ValueError("No se pudo leer la geometría GPT del disco.")
    partitions: list[dict[str, Any]] = []
    for line in output.splitlines():
        match = re.match(r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+\S+\s+([0-9A-Fa-f]+)\s+(.*)$", line)
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
    try:
        mounted = Path("/proc/self/mountinfo").read_text(encoding="utf-8", errors="replace")
    except OSError:
        mounted = ""
    if mounted and any(partition["device"] in mounted for partition in partitions):
        raise ValueError("El disco tiene una partición montada y no se puede capturar.")
    swaps = Path("/proc/swaps")
    if swaps.is_file():
        content = swaps.read_text(encoding="utf-8", errors="replace")
        if any(partition["device"] in content for partition in partitions):
            raise ValueError("El disco tiene swap activo y no se puede capturar.")


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
        self._thread = threading.Thread(target=self._run, name="pyfog-lease", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                json_request(
                    self._endpoint,
                    method="POST",
                    payload={"message": "Lease renovada por el agente."},
                    token=self._token,
                    ca_file=self._ca_file,
                    expected_status={200},
                )
            except (OSError, ValueError):
                self._failed.set()
                return

    def check(self) -> None:
        if self._failed.is_set():
            raise ValueError("No se pudo renovar la lease de la tarea.")

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)


def capture_task(server: str, host_id: str, token: str, ca_file: str | None) -> bool:
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
            "capabilities": ["gpt", "partclone.ext4", "partclone.fat"],
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
                payload={"sequence": sequence, "success": False, "error": str(error)[:500]},
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
    args = parser.parse_args()
    token = os.environ.get("PYFOG_INVENTORY_TOKEN", "")
    validate_token(token)
    if not capture_task(args.server, args.host_id, token, args.ca_file):
        raise SystemExit(NO_TASK_EXIT)


if __name__ == "__main__":
    main()
