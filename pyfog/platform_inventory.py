"""Cross-platform inventory contracts that never imply imaging support."""

from __future__ import annotations

import json
from typing import Any, Literal

from pyfog.schemas import Inventory

WINDOWS_INVENTORY_VERSION = 1
MACOS_EVALUATION_VERSION = 1


class PlatformInventoryError(ValueError):
    """A platform inventory does not satisfy the versioned contract."""


def _decode(value: bytes | str | dict[str, Any]) -> object:
    if isinstance(value, (bytes, str)):
        try:
            value = json.loads(value)
        except (UnicodeDecodeError, json.JSONDecodeError):
            msg = "El inventario de plataforma no es JSON válido."
            raise PlatformInventoryError(msg) from None
    return value


def _parse(value: bytes | str | dict[str, Any], platform: Literal["windows", "macos"]) -> Inventory:
    decoded = _decode(value)
    if not isinstance(decoded, dict):
        msg = "El inventario de plataforma debe ser un objeto JSON."
        raise PlatformInventoryError(msg)
    raw_disks = decoded.get("disks")
    if isinstance(raw_disks, list) and any(
        isinstance(disk, dict)
        and isinstance(disk.get("stable_id"), str)
        and disk["stable_id"].startswith("path:")
        for disk in raw_disks
    ):
        msg = "Cada disco multiplataforma debe tener identidad WWN o serial."
        raise PlatformInventoryError(msg)
    try:
        inventory = Inventory.model_validate(decoded)
    except ValueError as error:
        msg = f"Inventario de {platform} inválido: {error}"
        raise PlatformInventoryError(msg) from None
    if inventory.os.id.casefold() != platform:
        msg = f"El inventario no declara la plataforma {platform}."
        raise PlatformInventoryError(msg)
    if inventory.architecture.casefold() not in {"x86_64", "amd64", "aarch64", "arm64"}:
        msg = "La arquitectura de plataforma no está soportada."
        raise PlatformInventoryError(msg)
    if any(not disk.stable_id or disk.stable_id.startswith("path:") for disk in inventory.disks):
        msg = "Cada disco multiplataforma debe tener identidad WWN o serial."
        raise PlatformInventoryError(msg)
    return inventory


def validate_windows_inventory(value: bytes | str | dict[str, Any]) -> Inventory:
    """Validate the Windows v1 contract without requiring a Windows host."""

    inventory = _parse(value, "windows")
    if inventory.firmware is None:
        msg = "Windows debe declarar firmware UEFI o BIOS."
        raise PlatformInventoryError(msg)
    if not inventory.os.version:
        msg = "Windows debe declarar la versión del sistema."
        raise PlatformInventoryError(msg)
    if not inventory.system.serial_number:
        inventory.warnings.append("system_serial_unavailable")
    return inventory


def validate_macos_evaluation_inventory(value: bytes | str | dict[str, Any]) -> Inventory:
    """Validate the synthetic/non-destructive macOS evaluation fixture."""

    inventory = _parse(value, "macos")
    if inventory.firmware is not None and inventory.firmware not in {"uefi", "bios"}:
        msg = "El firmware macOS no tiene un valor válido."
        raise PlatformInventoryError(msg)
    if not inventory.os.version:
        msg = "macOS debe declarar la versión del sistema."
        raise PlatformInventoryError(msg)
    return inventory


def platform_transport_contract(platform: Literal["windows", "macos"]) -> dict[str, Any]:
    """Return the public, secret-free transport and capability boundary."""

    return {
        "platform": platform,
        "inventory_schema_version": 1,
        "transport": "HTTPS POST /api/v1/hosts/{host_id}/inventory",
        "authentication": "rotating host bearer credential; hash-only storage",
        "minimum_permissions": "read-only CIM/WMI or system_profiler access",
        "revocation": "revoke the host credential server-side before decommissioning",
        "imaging": "out-of-scope",
        "secret_policy": (  # pragma: allowlist secret
            "no credentials in JSON, command arguments, artifacts, or logs"
        ),
    }
