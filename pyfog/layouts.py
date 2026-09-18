"""Read-only layout contracts for the supported non-partition profiles.

These parsers consume JSON/text emitted by system tools in read-only mode.  They do not run
those tools and they never return a command that contains a secret or an unvalidated device
path.  The agent uses the same contracts immediately before constructing a restore plan.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


class LayoutError(ValueError):
    """A storage layout is incomplete, ambiguous, or outside the supported profile."""


def _safe_identifier(value: object, label: str, *, max_length: int = 128) -> str:
    text = str(value or "").strip()
    if not text or len(text) > max_length or not re.fullmatch(r"[A-Za-z0-9._:+@/-]+", text):
        raise LayoutError(f"{label} no tiene un identificador seguro.")
    return text


@dataclass(frozen=True)
class LvmLinearLayout:
    pv_uuid: str
    vg_name: str
    vg_uuid: str
    lv_name: str
    lv_uuid: str
    size_bytes: int


@dataclass(frozen=True)
class Raid1Layout:
    uuid: str
    metadata: str
    member_ids: tuple[str, ...]
    size_bytes: int


@dataclass(frozen=True)
class Luks2Layout:
    uuid: str
    cipher: str
    sector_size: int


@dataclass(frozen=True)
class BtrfsSubvolume:
    subvolume_id: int
    path: str


def lvm_layout_from_manifest(value: object) -> LvmLinearLayout:
    """Convert one manifest volume into the same bounded layout used by LVM reports."""

    if not isinstance(value, dict) or value.get("type") != "lvm-linear":
        raise LayoutError("El manifiesto no declara un volumen LVM lineal.")
    size = value.get("size_bytes")
    if type(size) is not int or size <= 0:
        raise LayoutError("El volumen LVM no declara un tamaño positivo.")
    return LvmLinearLayout(
        _safe_identifier(value.get("pv_uuid"), "El UUID del PV"),
        _safe_identifier(value.get("vg_name"), "El nombre del VG"),
        _safe_identifier(value.get("vg_uuid"), "El UUID del VG"),
        _safe_identifier(value.get("lv_name"), "El nombre del LV"),
        _safe_identifier(value.get("lv_uuid"), "El UUID del LV"),
        size,
    )


def raid1_layout_from_manifest(value: object) -> Raid1Layout:
    """Convert one manifest RAID1 declaration without accepting device-node order as identity."""

    if not isinstance(value, dict) or value.get("level") != 1:
        raise LayoutError("El manifiesto no declara un array RAID1.")
    metadata = value.get("metadata")
    if metadata not in {"1.0", "1.1", "1.2"}:
        raise LayoutError("La metadata RAID1 no está soportada.")
    raw_members = value.get("member_ids")
    if not isinstance(raw_members, list) or len(raw_members) < 2:
        raise LayoutError("RAID1 debe declarar al menos dos miembros.")
    members = tuple(
        _safe_identifier(item, "La identidad del miembro RAID1") for item in raw_members
    )
    if len(set(members)) != len(members) or any(item.startswith("path:") for item in members):
        raise LayoutError("Los miembros RAID1 deben tener identidades estables y únicas.")
    size = value.get("size_bytes")
    if type(size) is not int or size <= 0:
        raise LayoutError("RAID1 no declara un tamaño positivo.")
    return Raid1Layout(
        _safe_identifier(value.get("uuid"), "El UUID del array"), metadata, members, size
    )


def luks2_layout_from_manifest(value: object) -> Luks2Layout:
    """Convert non-secret LUKS2 manifest metadata into the unlock contract."""

    if not isinstance(value, dict) or value.get("type") != "luks2":
        raise LayoutError("El manifiesto no declara un contenedor LUKS2.")
    raw_sector = value.get("sector_size")
    if type(raw_sector) is not int or raw_sector not in {512, 4096}:
        raise LayoutError("El sector LUKS2 del manifiesto no está soportado.")
    return Luks2Layout(
        _safe_identifier(value.get("uuid"), "El UUID LUKS2"),
        _safe_identifier(value.get("cipher"), "El cipher LUKS2"),
        raw_sector,
    )


def lvm_layout_to_manifest(layout: LvmLinearLayout) -> dict[str, object]:
    """Serialize only the non-secret LVM graph required for a later restore."""

    return {
        "type": "lvm-linear",
        "pv_uuid": layout.pv_uuid,
        "vg_name": layout.vg_name,
        "vg_uuid": layout.vg_uuid,
        "lv_name": layout.lv_name,
        "lv_uuid": layout.lv_uuid,
        "size_bytes": layout.size_bytes,
    }


def luks2_layout_to_manifest(layout: Luks2Layout) -> dict[str, object]:
    """Serialize LUKS2 parameters without keyslots, tokens, or passphrases."""

    return {
        "type": "luks2",
        "uuid": layout.uuid,
        "cipher": layout.cipher,
        "sector_size": layout.sector_size,
    }


def raid1_layout_to_manifest(layout: Raid1Layout) -> dict[str, object]:
    """Serialize RAID1 identity and geometry without member device ordering."""

    return {
        "level": 1,
        "uuid": layout.uuid,
        "metadata": layout.metadata,
        "member_ids": list(layout.member_ids),
        "size_bytes": layout.size_bytes,
    }


def _json_report(value: object, label: str) -> list[dict[str, Any]]:
    if isinstance(value, bytes):
        try:
            value = json.loads(value)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise LayoutError(f"El reporte {label} no es JSON válido.") from None
    elif isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            raise LayoutError(f"El reporte {label} no es JSON válido.") from None
    if not isinstance(value, dict):
        raise LayoutError(f"El reporte {label} no es un objeto JSON.")
    report = value.get("report")
    if not isinstance(report, list) or len(report) != 1 or not isinstance(report[0], dict):
        raise LayoutError(f"El reporte {label} no tiene una sección única.")
    rows = report[0].get(label)
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise LayoutError(f"El reporte {label} no contiene filas válidas.")
    return rows


def _size_bytes(value: object) -> int:
    """Parse LVM's bytes report with or without a decimal suffix."""

    text = str(value or "").strip().removesuffix("B")
    try:
        result = int(float(text))
    except ValueError:
        return 0
    return result


def parse_lvm_linear_reports(pvs: object, vgs: object, lvs: object) -> LvmLinearLayout:
    """Validate exactly one PV/VG/LV linear chain from ``--reportformat json`` output."""

    pv_rows = _json_report(pvs, "pv")
    vg_rows = _json_report(vgs, "vg")
    lv_rows = _json_report(lvs, "lv")
    if len(pv_rows) != 1 or len(vg_rows) != 1 or len(lv_rows) != 1:
        raise LayoutError("Sólo se admite un PV, un VG y un LV lineales por perfil.")
    pv, vg, lv = pv_rows[0], vg_rows[0], lv_rows[0]
    pv_uuid = _safe_identifier(pv.get("pv_uuid"), "El UUID del PV")
    vg_name = _safe_identifier(vg.get("vg_name"), "El nombre del VG")
    vg_uuid = _safe_identifier(vg.get("vg_uuid"), "El UUID del VG")
    lv_name = _safe_identifier(lv.get("lv_name"), "El nombre del LV")
    lv_uuid = _safe_identifier(lv.get("lv_uuid"), "El UUID del LV")
    if str(lv.get("vg_uuid") or "") != vg_uuid or str(lv.get("vg_name") or "") != vg_name:
        raise LayoutError("El LV no pertenece al VG declarado.")
    if str(lv.get("lv_attr") or "").startswith("s") or str(lv.get("lv_attr") or "").startswith("V"):
        raise LayoutError("Los snapshots y thin volumes no están soportados.")
    layout = str(lv.get("lv_layout") or lv.get("segtype") or "linear").lower()
    if layout not in {"linear", "striped"} or layout == "striped":
        raise LayoutError("Sólo se admite un LV lineal sin striping, snapshot ni RAID.")
    raw_size = lv.get("lv_size_bytes", lv.get("lv_size"))
    size = _size_bytes(raw_size)
    if size <= 0:
        raise LayoutError("El LV no declara un tamaño positivo en bytes.")
    return LvmLinearLayout(pv_uuid, vg_name, vg_uuid, lv_name, lv_uuid, size)


def parse_mdraid1_export(value: str) -> Raid1Layout:
    """Parse ``mdadm --detail --export`` without assembling the array."""

    values: dict[str, str] = {}
    for line in value.splitlines():
        key, separator, item = line.partition("=")
        if separator and re.fullmatch(r"[A-Z0-9_]+", key):
            values[key] = item.strip()
    if values.get("MD_LEVEL") != "raid1":
        raise LayoutError("Sólo se admite mdadm RAID1.")
    state = {item.strip() for item in values.get("MD_STATE", "").lower().split(",") if item.strip()}
    if not state or not state.issubset({"clean", "active"}):
        raise LayoutError("El array RAID1 está degradado o no tiene un estado seguro.")
    uuid = _safe_identifier(values.get("MD_UUID"), "El UUID del array")
    metadata = values.get("MD_METADATA", "")
    if metadata not in {"1.0", "1.1", "1.2"}:
        raise LayoutError("La metadata mdadm no está soportada.")
    member_ids = tuple(
        value
        for key, value in sorted(values.items())
        if key.startswith("MD_DEVICE_") and key.endswith("_DEV")
    )
    if len(member_ids) < 2 or len(set(member_ids)) != len(member_ids):
        raise LayoutError("RAID1 debe declarar al menos dos miembros distintos.")
    raw_size = values.get("MD_ARRAY_SIZE")
    try:
        size = int(raw_size or "0")
    except ValueError:
        size = 0
    if size <= 0:
        raise LayoutError("RAID1 no declara un tamaño válido.")
    return Raid1Layout(uuid, metadata, member_ids, size)


def parse_luks2_metadata(value: object) -> Luks2Layout:
    """Extract only non-secret LUKS2 metadata from cryptsetup JSON output."""

    if isinstance(value, (bytes, str)):
        try:
            value = json.loads(value)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise LayoutError("La metadata LUKS2 no es JSON válido.") from None
    if not isinstance(value, dict):
        raise LayoutError("La metadata LUKS2 no es un objeto JSON.")
    if value.get("version") != 2:
        raise LayoutError("El contenedor no es LUKS2.")
    uuid = _safe_identifier(value.get("uuid"), "El UUID LUKS2")
    cipher = _safe_identifier(value.get("cipher", ""), "El cipher LUKS2")
    raw_sector_size = value.get("sector-size", value.get("sector_size", 0))
    try:
        sector_size = int(raw_sector_size) if isinstance(raw_sector_size, (int, float, str)) else 0
    except (TypeError, ValueError):
        sector_size = 0
    if sector_size not in {512, 4096}:
        raise LayoutError("El sector LUKS2 no está soportado.")
    return Luks2Layout(uuid, cipher, sector_size)


def parse_btrfs_subvolumes(value: str) -> list[BtrfsSubvolume]:
    """Parse ``btrfs subvolume list`` and reject snapshot-like or ambiguous paths."""

    result: list[BtrfsSubvolume] = []
    for line in value.splitlines():
        match = re.search(r"ID\s+(\d+)\s+gen\s+\d+\s+top level\s+\d+\s+path\s+(.+)$", line)
        if not match:
            continue
        path = match[2].strip()
        if not re.fullmatch(r"[A-Za-z0-9._/@+-]+", path) or "snapshot" in path.casefold():
            raise LayoutError("El layout Btrfs contiene un snapshot o una ruta ambigua.")
        result.append(BtrfsSubvolume(int(match[1]), path))
    if not result or len({item.subvolume_id for item in result}) != len(result):
        raise LayoutError("No se pudo identificar un conjunto válido de subvolúmenes Btrfs.")
    if not any(item.path in {"@", "@/", "."} for item in result):
        raise LayoutError("El layout Btrfs no declara un subvolumen raíz conocido.")
    return result


def filesystem_tool(filesystem: str) -> str:
    """Map a validated filesystem name to a fixed Partclone executable."""

    try:
        return {
            "fat32": "partclone.fat",
            "ext4": "partclone.ext4",
            "xfs": "partclone.xfs",
            "btrfs": "partclone.btrfs",
        }[filesystem]
    except KeyError:
        raise LayoutError(
            f"El filesystem {filesystem} no pertenece a la matriz soportada."
        ) from None


def _safe_device(value: str) -> str:
    if not re.fullmatch(r"/dev/[A-Za-z0-9._+-]+", value):
        raise LayoutError("El dispositivo de almacenamiento no es seguro.")
    return value


def lvm_restore_commands(layout: LvmLinearLayout, device: str) -> list[list[str]]:
    """Return fixed, auditable commands for a previously validated linear LVM layout."""

    target = _safe_device(device)
    return [
        ["pvcreate", "--yes", "--uuid", layout.pv_uuid, target],
        ["vgcreate", "--uuid", layout.vg_uuid, layout.vg_name, target],
        [
            "lvcreate",
            "--yes",
            "--name",
            layout.lv_name,
            "--uuid",
            layout.lv_uuid,
            "--size",
            f"{layout.size_bytes}B",
            layout.vg_name,
        ],
        ["lvchange", "--yes", "--setactivationskip", "n", layout.vg_name + "/" + layout.lv_name],
    ]


def raid1_restore_commands(
    layout: Raid1Layout,
    members: list[str],
    *,
    member_ids: tuple[str, ...] | None = None,
) -> list[str]:
    """Return one mdadm create command after all members have been validated."""

    expected_ids = member_ids or layout.member_ids
    if len(members) != len(expected_ids):
        raise LayoutError("Los miembros destino no coinciden con el array RAID1.")
    if member_ids is not None and tuple(member_ids) != layout.member_ids:
        raise LayoutError("Las identidades de miembros no coinciden con el manifiesto RAID1.")
    for member in members:
        _safe_device(member)
    if member_ids is None and any(
        not expected.startswith("/dev/") or _safe_device(member) != expected
        for member, expected in zip(members, expected_ids, strict=True)
    ):
        raise LayoutError("Los miembros destino no coinciden con el array RAID1.")
    return [
        "mdadm",
        "--create",
        _safe_device(f"/dev/md-pyfog-{layout.uuid}"),
        "--run",
        "--level=1",
        f"--raid-devices={len(members)}",
        f"--metadata={layout.metadata}",
        f"--uuid={layout.uuid}",
        *(_safe_device(member) for member in members),
    ]


def validate_luks2_match(expected: Luks2Layout, actual: Luks2Layout) -> None:
    """Compare non-secret LUKS metadata before accepting a key from a provider."""

    if expected != actual:
        raise LayoutError("La metadata LUKS2 del destino no coincide con la imagen.")
