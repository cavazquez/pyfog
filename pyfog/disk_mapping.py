"""Stable disk identity and all-or-nothing multi-disk target planning."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class DiskMappingError(ValueError):
    """A source disk cannot be mapped to exactly one safe target."""


def stable_disk_id(device: dict[str, Any]) -> str:
    """Return the strongest available identity, never preferring ``/dev`` order."""

    explicit = device.get("stable_id")
    if isinstance(explicit, str) and explicit:
        return explicit
    wwn = str(device.get("wwn") or "").strip()
    if wwn:
        return f"wwn:{wwn}"
    serial = str(device.get("serial") or device.get("serial_number") or "").strip()
    if serial:
        return f"serial:{serial}"
    # A device node is useful for a single-disk legacy operation, but it is not a
    # stable identity for a multi-disk mapping.  Keep this helper deliberately
    # strict so callers cannot accidentally turn detection order into identity.
    msg = "El disco no tiene una identidad estable utilizable; no use el orden /dev."
    raise DiskMappingError(msg)


def require_non_path_identity(device: dict[str, Any]) -> str:
    """Require a WWN or serial for a multi-disk declaration."""

    identity = stable_disk_id(device)
    if identity.startswith("path:"):
        msg = "La imagen multidisco no puede depender del orden /dev."
        raise DiskMappingError(msg)
    return identity


def _device_path(device: dict[str, Any]) -> str:
    value = device.get("path") or device.get("name")
    if not isinstance(value, str) or not value.startswith("/dev/"):
        msg = "El inventario no contiene una ruta de dispositivo válida."
        raise DiskMappingError(msg)
    return value


def _size(device: dict[str, Any]) -> int:
    value = device.get("size_bytes", device.get("size"))
    if type(value) is not int or value <= 0:
        msg = "El inventario no contiene un tamaño de disco válido."
        raise DiskMappingError(msg)
    return value


def _sector(device: dict[str, Any]) -> int:
    value = device.get("logical_sector_bytes", device.get("log-sec"))
    if type(value) is not int or value not in {512, 4096}:
        msg = "El inventario no contiene un sector lógico admitido."
        raise DiskMappingError(msg)
    return value


@dataclass(frozen=True)
class DiskRestorePlan:
    """A validated source-to-target mapping; no device has been modified."""

    source_id: str
    source_size_bytes: int
    target_id: str
    target_path: str
    target_size_bytes: int
    logical_sector_bytes: int


def plan_multi_disk_restore(
    source_disks: list[dict[str, Any]],
    target_disks: list[dict[str, Any]],
    *,
    allow_larger_targets: bool = True,
) -> list[DiskRestorePlan]:
    """Validate every disk before returning a plan that can be written atomically.

    The matching key is the source stable identity.  A target may be larger when explicitly
    allowed, but it may never have a different logical sector size.  Duplicate identities,
    missing targets, ambiguous candidates, and removable targets are all rejected before the
    caller gets a plan.
    """

    if not source_disks or len(source_disks) != len(target_disks):
        msg = "La cantidad de discos de origen y destino no coincide."
        raise DiskMappingError(msg)
    source_ids = [require_non_path_identity(disk) for disk in source_disks]
    if len(set(source_ids)) != len(source_ids):
        msg = "La captura contiene identidades de disco repetidas."
        raise DiskMappingError(msg)
    target_ids = [stable_disk_id(disk) for disk in target_disks]
    if len(set(target_ids)) != len(target_ids):
        msg = "El destino contiene identidades de disco repetidas."
        raise DiskMappingError(msg)
    by_id = dict(zip(target_ids, target_disks, strict=True))
    plans: list[DiskRestorePlan] = []
    for source, source_id in zip(source_disks, source_ids, strict=True):
        target = by_id.get(source_id)
        if target is None:
            msg = f"No se encontró el disco destino {source_id}."
            raise DiskMappingError(msg)
        if str(target.get("rm", target.get("removable", False))).lower() in {"true", "1"}:
            msg = f"El destino {source_id} es removible."
            raise DiskMappingError(msg)
        source_size = _size(source)
        target_size = _size(target)
        if target_size < source_size or (not allow_larger_targets and target_size != source_size):
            msg = f"El destino {source_id} no tiene capacidad suficiente."
            raise DiskMappingError(msg)
        source_sector = _sector(source)
        if _sector(target) != source_sector:
            msg = f"El sector lógico de {source_id} no coincide."
            raise DiskMappingError(msg)
        plans.append(
            DiskRestorePlan(
                source_id=source_id,
                source_size_bytes=source_size,
                target_id=source_id,
                target_path=_device_path(target),
                target_size_bytes=target_size,
                logical_sector_bytes=source_sector,
            )
        )
    return plans
