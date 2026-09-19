#!/usr/bin/env python3
"""Recolector Linux de solo lectura. Requiere Python >= 3.10, sin paquetes externos."""

import argparse
import contextlib
import ipaddress
import json
import os
import platform
import re
import secrets
import shlex
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_PAYLOAD_BYTES = 1_000_000
MAX_RESPONSE_BYTES = 1_000_000
MAX_PAIR_INTERVAL_SECONDS = 60
MAX_PAIR_TIMEOUT_SECONDS = 3600
MAX_TCP_PORT = 65535
MAX_TOKEN_LENGTH = 256


@dataclass(frozen=True)
class JsonRequest:
    method: str
    payload: bytes | None
    token: str | None
    ca_file: str | None
    expected_status: set[int]


@dataclass(frozen=True)
class PairingOptions:
    timeout_seconds: int = 900
    interval_seconds: float = 2.0


def _validate_payload_size(payload: bytes) -> None:
    if len(payload) > MAX_PAYLOAD_BYTES:
        msg = "El informe supera el límite permitido."
        raise ValueError(msg)


def read_text(path: Path, warnings: list[str], *, optional: bool = False) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        if not optional:
            warnings.append(f"No se pudo leer {path}; falta información o permiso.")
        return ""


def disk_inventory(warnings: list[str]) -> list[dict[str, Any]]:
    columns = "NAME,TYPE,SIZE,MODEL,SERIAL,WWN,TRAN,LOG-SEC,PTTYPE,RM"
    try:
        executable = shutil.which("lsblk")
        if not executable:
            msg = "lsblk"
            raise FileNotFoundError(msg)
        result = subprocess.run(  # noqa: S603 - fixed arguments and resolved utility, no shell
            [executable, "--json", "--bytes", "--nodeps", "--output", columns],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        devices = json.loads(result.stdout)["blockdevices"]
        disks = []
        for device in devices:
            if device.get("type") != "disk" or int(device.get("size") or 0) <= 0:
                continue
            wwn = (device.get("wwn") or "").strip()[:200]
            serial = (device.get("serial") or "").strip()[:200]
            name = str(device.get("name") or "")
            stable_id = (
                f"wwn:{wwn}" if wwn else f"serial:{serial}" if serial else f"path:/dev/{name}"
            )
            disks.append(
                {
                    "name": f"/dev/{name}",
                    "stable_id": stable_id,
                    "size_bytes": int(device["size"]),
                    "model": (device.get("model") or "").strip()[:200],
                    "serial_number": serial,
                    "wwn": wwn,
                    "transport": (device.get("tran") or "")[:200],
                    "logical_sector_bytes": (
                        int(device["log-sec"]) if device.get("log-sec") else None
                    ),
                    "partition_table": (
                        "mbr"
                        if str(device.get("pttype") or "").lower() in {"dos", "mbr"}
                        else "gpt"
                        if str(device.get("pttype") or "").lower() == "gpt"
                        else None
                    ),
                    "removable": str(device.get("rm", False)).lower() in {"true", "1"},
                }
            )
        return disks[:128]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        warnings.append("No se pudo consultar lsblk. El informe no incluye discos.")
        return []


def collect(proc: Path = Path("/proc"), sysfs: Path = Path("/sys")) -> dict[str, Any]:
    warnings: list[str] = []
    if platform.system() != "Linux":
        msg = "Este recolector solo admite Linux."
        raise ValueError(msg)
    os_release: dict[str, str] = {}
    for line in read_text(Path("/etc/os-release"), warnings).splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        try:
            os_release[key] = " ".join(shlex.split(value))
        except ValueError:
            continue
    cpu_model = ""
    for line in read_text(proc / "cpuinfo", warnings).splitlines():
        if line.partition(":")[0].strip() in {"model name", "Hardware"}:
            cpu_model = line.partition(":")[2].strip()[:200]
            break
    memory = read_text(proc / "meminfo", warnings)
    match = re.search(r"^MemTotal:\s+(\d+)\s+kB", memory, re.MULTILINE)
    total_bytes = int(match[1]) * 1024 if match else None
    interfaces = []
    network_path = sysfs / "class/net"
    try:
        paths = sorted(network_path.iterdir())
    except OSError:
        paths = []
        warnings.append("No se pudieron consultar las interfaces de red.")
    for path in paths:
        mac = read_text(path / "address", warnings).lower()
        if (
            path.name == "lo"
            or not re.fullmatch(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}", mac)
            or mac == "00:00:00:00:00:00"
            or int(mac[:2], 16) & 1
        ):
            continue
        interfaces.append(
            {
                "name": path.name[:100],
                "mac_address": mac,
                "state": read_text(path / "operstate", warnings, optional=True) or "unknown",
            }
        )
    if not interfaces:
        msg = "No se encontró una interfaz con MAC válida para identificar este equipo."
        raise ValueError(msg)
    dmi = sysfs / "class/dmi/id"
    system = {
        key: read_text(dmi / filename, warnings)[:200]
        for key, filename in {
            "manufacturer": "sys_vendor",
            "model": "product_name",
            "serial_number": "product_serial",
        }.items()
    }
    disks = disk_inventory(warnings)
    return {
        "schema_version": 1,
        "report_id": str(uuid.uuid4()),
        "collected_at": datetime.now(timezone.utc).isoformat(),  # noqa: UP017 (Python 3.10 client)
        "hostname": socket.gethostname()[:253],
        "firmware": "uefi" if (sysfs / "firmware/efi").is_dir() else "bios",
        "os": {
            "name": os_release.get("PRETTY_NAME", "Linux")[:200],
            "id": os_release.get("ID", "linux")[:200],
            "version": os_release.get("VERSION_ID", "")[:200],
        },
        "kernel": platform.release()[:200],
        "architecture": platform.machine()[:200],
        "cpu": {"model": cpu_model, "logical_cores": os.cpu_count()},
        "memory": {"total_bytes": total_bytes},
        "system": system,
        "disks": disks,
        "interfaces": interfaces[:128],
        "warnings": warnings[:64],
    }


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward a bearer credential to an unexpected destination."""

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
        msg = "Usá una URL base HTTPS sin credenciales. HTTP solo se admite en loopback."
        raise ValueError(msg) from None
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
        or (port is not None and not 1 <= port <= MAX_TCP_PORT)
    ):
        msg = "Usá una URL base HTTPS sin credenciales. HTTP solo se admite en loopback."
        raise ValueError(msg)
    return url


def validate_token(token: str, *, message: str) -> str:
    if not token or len(token) > MAX_TOKEN_LENGTH or "\n" in token or "\r" in token:
        raise ValueError(message)
    return token


def json_request(endpoint: str, request_spec: JsonRequest) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if request_spec.payload is not None:
        headers["Content-Type"] = "application/json"
    if request_spec.token is not None:
        validate_token(request_spec.token, message="La capacidad de emparejamiento no es válida.")
        headers["Authorization"] = f"Bearer {request_spec.token}"
    request = urllib.request.Request(  # noqa: S310 - server URL was validated by the caller
        endpoint, data=request_spec.payload, headers=headers, method=request_spec.method
    )
    context = ssl.create_default_context(cafile=request_spec.ca_file)
    # Pairing credentials must never be sent through a proxy or followed to another host.
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=context),
        NoRedirect(),
    )
    try:
        with opener.open(request, timeout=30) as response:
            if response.status not in request_spec.expected_status:
                msg = f"El servidor devolvió HTTP {response.status}."
                raise ValueError(msg)
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        msg = f"El servidor respondió HTTP {error.code} durante el emparejamiento."
        raise ValueError(msg) from None
    except urllib.error.URLError:
        msg = "No se pudo conectar al servidor. Revisá URL, red y certificado."
        raise ValueError(msg) from None
    if len(body) > MAX_RESPONSE_BYTES:
        msg = "La respuesta del servidor supera el límite permitido."
        raise ValueError(msg)
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        msg = "El servidor devolvió una respuesta JSON inválida."
        raise ValueError(msg) from None
    if not isinstance(value, dict):
        msg = "El servidor devolvió una respuesta JSON inesperada."
        raise TypeError(msg)
    return value


def required_uuid(value: object, *, field: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        msg = f"La respuesta del servidor no incluye un {field} válido."
        raise ValueError(msg) from None


def inventory_mac(document: object) -> str:
    if not isinstance(document, dict):
        msg = "El informe debe ser un objeto JSON."
        raise TypeError(msg)
    interfaces = document.get("interfaces")
    if not isinstance(interfaces, list):
        msg = "El informe no incluye interfaces de red."
        raise TypeError(msg)
    for interface in interfaces:
        if not isinstance(interface, dict):
            continue
        mac = interface.get("mac_address")
        if isinstance(mac, str) and re.fullmatch(r"[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}", mac):
            return mac.lower()
    msg = "El informe no incluye una MAC válida para solicitar el registro."
    raise ValueError(msg)


def send_inventory(
    payload: bytes, server: str, host_id: str, token: str, ca_file: str | None
) -> None:
    validate_server(server)
    validate_token(token, message="Definí PYFOG_INVENTORY_TOKEN con la credencial del equipo.")
    valid_id = str(uuid.UUID(host_id))
    endpoint = f"{server.rstrip('/')}/api/v1/hosts/{valid_id}/inventory"
    request = urllib.request.Request(  # noqa: S310 - HTTPS/loopback schemes validated above
        endpoint,
        data=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    context = ssl.create_default_context(cafile=ca_file)
    # Do not send inventory credentials through an inherited environment proxy.
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=context),
        NoRedirect(),
    )
    try:
        with opener.open(request, timeout=30) as response:
            if response.status not in {200, 201}:
                msg = f"El servidor devolvió HTTP {response.status}."
                raise ValueError(msg)
    except urllib.error.HTTPError as error:
        msg = f"El servidor rechazó el informe (HTTP {error.code}). Revisá token, equipo y formato."
        raise ValueError(msg) from None
    except urllib.error.URLError:
        msg = "No se pudo conectar al servidor. Revisá URL, red y certificado."
        raise ValueError(msg) from None


def pair_inventory(
    payload: bytes,
    document: object,
    server: str,
    ca_file: str | None,
    options: PairingOptions | None = None,
) -> None:
    """Request administrator approval, then send one inventory report with its capability."""

    options = options or PairingOptions()
    validate_server(server)
    if options.timeout_seconds <= 0 or options.interval_seconds <= 0:
        msg = "El timeout y el intervalo de emparejamiento deben ser positivos."
        raise ValueError(msg)
    mac = inventory_mac(document)
    session_id = str(uuid.uuid4())
    challenge = secrets.token_urlsafe(18)
    print(f"Solicitud PXE creada. Desafío: {challenge}", file=sys.stderr, flush=True)
    base = server.rstrip("/")
    created = json_request(
        f"{base}/api/v1/pairing/requests",
        JsonRequest(
            method="POST",
            payload=json.dumps(
                {"session_id": session_id, "mac_address": mac, "challenge": challenge},
                separators=(",", ":"),
            ).encode("utf-8"),
            token=None,
            ca_file=ca_file,
            expected_status={202},
        ),
    )
    request_id = required_uuid(created.get("request_id"), field="request_id")
    poll_token = created.get("poll_token")
    if not isinstance(poll_token, str):
        msg = "La respuesta del servidor no incluye una capacidad de emparejamiento."
        raise TypeError(msg)
    validate_token(poll_token, message="La capacidad de emparejamiento no es válida.")
    deadline = time.monotonic() + options.timeout_seconds
    while True:
        status = json_request(
            f"{base}/api/v1/pairing/requests/{request_id}",
            JsonRequest(
                method="GET",
                payload=None,
                token=poll_token,
                ca_file=ca_file,
                expected_status={200},
            ),
        )
        state = status.get("status")
        if state == "approved":
            host_id = required_uuid(status.get("host_id"), field="host_id")
            inventory_token = status.get("inventory_token")
            if not isinstance(inventory_token, str):
                msg = "La solicitud fue aprobada sin una capacidad de inventario."
                raise ValueError(msg)
            validate_token(inventory_token, message="La capacidad de inventario no es válida.")
            result = json_request(
                f"{base}/api/v1/pairing/requests/{request_id}/inventory",
                JsonRequest(
                    method="POST",
                    payload=payload,
                    token=inventory_token,
                    ca_file=ca_file,
                    expected_status={200, 201},
                ),
            )
            if result.get("host_id") != host_id:
                msg = "El servidor asoció el inventario a otro equipo."
                raise ValueError(msg)
            print("Emparejamiento aprobado; inventario recibido por PyFog.", file=sys.stderr)
            return
        if state == "rejected":
            reason = status.get("reason")
            detail = f": {reason}" if isinstance(reason, str) and reason else "."
            msg = f"La solicitud PXE fue rechazada{detail}"
            raise ValueError(msg)
        if state == "expired":
            msg = "La solicitud PXE venció antes de ser aprobada."
            raise ValueError(msg)
        if state != "pending":
            msg = "El servidor devolvió un estado de emparejamiento desconocido."
            raise ValueError(msg)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            msg = "Se agotó el tiempo de espera para aprobar la solicitud PXE."
            raise ValueError(msg)
        time.sleep(min(options.interval_seconds, remaining))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Guardar JSON con permisos privados")
    parser.add_argument(
        "--input", type=Path, help="Reenviar un informe existente sin recolectar otro"
    )
    parser.add_argument("--server", help="URL base HTTPS de PyFog")
    parser.add_argument("--host-id", help="UUID del equipo registrado")
    parser.add_argument(
        "--pair", action="store_true", help="Solicitar aprobación para registrar este equipo"
    )
    parser.add_argument(
        "--pair-timeout", type=int, default=900, help="Segundos máximos para esperar aprobación"
    )
    parser.add_argument(
        "--pair-interval", type=float, default=2.0, help="Segundos entre consultas de estado"
    )
    parser.add_argument("--ca-file", help="CA pública local para validar HTTPS")
    args = parser.parse_args()
    if args.pair:
        if not args.server or args.host_id:
            parser.error("--pair requiere --server y no se combina con --host-id.")
        if args.input:
            parser.error("--pair recolecta un informe nuevo y no se combina con --input.")
        if args.pair_timeout <= 0 or args.pair_timeout > MAX_PAIR_TIMEOUT_SECONDS:
            parser.error("--pair-timeout debe estar entre 1 y 3600 segundos.")
        if args.pair_interval <= 0 or args.pair_interval > MAX_PAIR_INTERVAL_SECONDS:
            parser.error("--pair-interval debe ser mayor que 0 y como máximo 60 segundos.")
    elif bool(args.server) != bool(args.host_id):
        parser.error("--server y --host-id deben indicarse juntos.")
    if args.input and not args.server:
        parser.error("--input se utiliza junto con --server y --host-id.")
    try:
        if args.input:
            with args.input.open("rb") as source:
                payload = source.read(1_048_577)
            document = json.loads(payload)
        else:
            document = collect()
            payload = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        _validate_payload_size(payload)
        if args.output:
            # Refuse existing files/symlinks, including device paths.
            descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as target:
                target.write(payload)
        if args.pair:
            pair_inventory(
                payload,
                document,
                args.server,
                args.ca_file,
                PairingOptions(
                    timeout_seconds=args.pair_timeout,
                    interval_seconds=args.pair_interval,
                ),
            )
        elif args.server:
            send_inventory(
                payload,
                args.server,
                args.host_id,
                os.environ.get("PYFOG_INVENTORY_TOKEN", ""),
                args.ca_file,
            )
            print("Inventario recibido por PyFog.", file=sys.stderr)
        elif not args.output:
            sys.stdout.buffer.write(payload)
    except (OSError, ValueError, TimeoutError) as error:
        parser.exit(1, f"Error: {error}\n")


if __name__ == "__main__":
    main()
