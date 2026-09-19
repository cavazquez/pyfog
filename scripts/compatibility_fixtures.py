#!/usr/bin/env python3
"""Generate and verify the deterministic QCOW2 compatibility fixture matrix."""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MATRIX_PATH = PROJECT_ROOT / "tests" / "fixtures" / "compatibility" / "matrix.json"
QCOW2_FORMAT = "qcow2"
QCOW2_CLUSTER_SIZE = 65536
QCOW2_COMPAT = "1.1"
QCOW2_LAZY_REFCOUNTS = "off"
FIXTURE_ALIGNMENT_BYTES = 4096
SHA256_PART_COUNT = 8
SHA256_PART_LENGTH = 8


class FixtureError(ValueError):
    """A fixture definition or generated disk failed its safety checks."""


def load_matrix(path: Path = MATRIX_PATH) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        msg = f"No se pudo leer la matriz de fixtures: {error}"
        raise FixtureError(msg) from None
    if not isinstance(value, dict):
        msg = "La matriz de fixtures debe ser un objeto JSON."
        raise FixtureError(msg)
    if value.get("schema_version") != 1:
        msg = "La matriz de fixtures requiere schema_version 1."
        raise FixtureError(msg)
    generator = value.get("generator")
    if not isinstance(generator, dict):
        msg = "La matriz no declara opciones deterministas de generación."
        raise FixtureError(msg)
    if (
        generator.get("format") != QCOW2_FORMAT
        or generator.get("compat") != QCOW2_COMPAT
        or generator.get("cluster_size") != QCOW2_CLUSTER_SIZE
        or generator.get("lazy_refcounts") is not False
    ):
        msg = "La matriz no usa las opciones QCOW2 deterministas requeridas."
        raise FixtureError(msg)
    fixtures = value.get("fixtures")
    if not isinstance(fixtures, list) or not fixtures:
        msg = "La matriz debe declarar al menos un fixture."
        raise FixtureError(msg)
    ids: set[str] = set()
    files: set[str] = set()
    for fixture in fixtures:
        validate_spec(fixture)
        fixture_id = fixture["id"]
        filename = fixture["file"]
        if fixture_id in ids or filename in files:
            msg = "La matriz contiene IDs o archivos repetidos."
            raise FixtureError(msg)
        ids.add(fixture_id)
        files.add(filename)
    return value


def validate_spec(fixture: object) -> None:
    if not isinstance(fixture, dict):
        msg = "Cada fixture debe ser un objeto JSON."
        raise FixtureError(msg)
    required = {
        "id",
        "file",
        "virtual_size_bytes",
        "sha256_parts",
        "status",
        "checks",
        "restrictions",
        "capabilities",
    }
    if not required.issubset(fixture):
        missing = ", ".join(sorted(required - set(fixture)))
        msg = f"El fixture no declara campos requeridos: {missing}."
        raise FixtureError(msg)
    fixture_id = fixture["id"]
    filename = fixture["file"]
    if (
        not isinstance(fixture_id, str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", fixture_id)
        or not isinstance(filename, str)
        or filename != f"{fixture_id}.qcow2"
    ):
        msg = "El ID o archivo del fixture no es seguro."
        raise FixtureError(msg)
    virtual_size = fixture["virtual_size_bytes"]
    if type(virtual_size) is not int or virtual_size <= 0 or virtual_size % FIXTURE_ALIGNMENT_BYTES:
        msg = f"El tamaño virtual de {fixture_id} no es válido."
        raise FixtureError(msg)
    sha256_parts = fixture["sha256_parts"]
    if (
        not isinstance(sha256_parts, list)
        or len(sha256_parts) != SHA256_PART_COUNT
        or any(
            not isinstance(part, str)
            or len(part) != SHA256_PART_LENGTH
            or not re.fullmatch(r"[0-9a-f]{8}", part)
            for part in sha256_parts
        )
    ):
        msg = f"Las partes SHA-256 de {fixture_id} no son válidas."
        raise FixtureError(msg)
    if fixture["status"] not in {"supported", "rejected"}:
        msg = f"El estado de {fixture_id} no es válido."
        raise FixtureError(msg)
    for field in ("checks", "restrictions"):
        value = fixture[field]
        if (
            not isinstance(value, list)
            or not value
            or any(not isinstance(item, str) or not item for item in value)
        ):
            msg = f"{field} de {fixture_id} no es una lista válida."
            raise FixtureError(msg)
    if fixture["status"] == "rejected":
        errors = fixture.get("expected_errors")
        if (
            not isinstance(errors, list)
            or not errors
            or any(not isinstance(item, str) or not item for item in errors)
        ):
            msg = f"{fixture_id} debe declarar errores esperados."
            raise FixtureError(msg)
    capabilities = fixture["capabilities"]
    if not isinstance(capabilities, dict):
        msg = f"{fixture_id} no declara capacidades."
        raise FixtureError(msg)
    required_capabilities = {
        "firmware",
        "partition_table",
        "disks",
        "filesystems",
        "encryption",
        "volumes",
    }
    if not required_capabilities.issubset(capabilities):
        msg = f"Las capacidades de {fixture_id} están incompletas."
        raise FixtureError(msg)
    if type(capabilities["disks"]) is not int or capabilities["disks"] < 1:
        msg = f"La cantidad de discos de {fixture_id} no es válida."
        raise FixtureError(msg)
    filesystems = capabilities["filesystems"]
    if (
        not isinstance(filesystems, list)
        or not filesystems
        or any(not isinstance(item, str) or not item for item in filesystems)
    ):
        msg = f"Los filesystems de {fixture_id} no son válidos."
        raise FixtureError(msg)


def fixture_specs(matrix: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    payload = matrix if matrix is not None else load_matrix()
    fixtures = payload["fixtures"]
    if not isinstance(fixtures, list):
        msg = "La matriz no contiene una lista de fixtures."
        raise FixtureError(msg)
    return [fixture for fixture in fixtures if isinstance(fixture, dict)]


def _qemu_img() -> str:
    path = shutil.which("qemu-img")
    if path is None:
        msg = "Falta qemu-img; instalá el paquete qemu-utils."
        raise FixtureError(msg)
    return path


def _run_qemu(args: list[str]) -> str:
    try:
        result = subprocess.run(  # noqa: S603 - qemu-img is resolved from PATH intentionally.
            [_qemu_img(), *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        msg = "Falta qemu-img; instalá el paquete qemu-utils."
        raise FixtureError(msg) from None
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip() or error.stdout.strip() or "sin diagnóstico"
        msg = f"qemu-img falló: {detail}"
        raise FixtureError(msg) from None
    return result.stdout


def generate_fixture(fixture: dict[str, Any], output_dir: Path) -> Path:
    validate_spec(fixture)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / str(fixture["file"])
    if path.exists() or path.is_symlink():
        msg = f"No se sobrescribe el fixture existente: {path}"
        raise FixtureError(msg)
    if fixture["capabilities"]["partition_table"] == "mbr":
        raw_path = output_dir / f".{fixture['id']}.raw"
        _run_qemu(["create", "-f", "raw", str(raw_path), str(fixture["virtual_size_bytes"])])
        try:
            raw = bytearray(fixture["virtual_size_bytes"])
            raw[0:446] = b"\x90" * 446
            raw[440:444] = bytes.fromhex("1a2b3c4d")
            entry = bytearray(16)
            entry[0] = 0x80
            entry[4] = 0x83
            entry[8:12] = (2_048).to_bytes(4, "little")
            entry[12:16] = (fixture["virtual_size_bytes"] // 512 - 2_048).to_bytes(4, "little")
            raw[446:462] = entry
            raw[510:512] = b"\x55\xaa"
            raw_path.write_bytes(raw)
            _run_qemu(
                [
                    "convert",
                    "-f",
                    "raw",
                    "-O",
                    QCOW2_FORMAT,
                    "-o",
                    f"compat={QCOW2_COMPAT},lazy_refcounts={QCOW2_LAZY_REFCOUNTS},"
                    f"cluster_size={QCOW2_CLUSTER_SIZE}",
                    str(raw_path),
                    str(path),
                ]
            )
        finally:
            raw_path.unlink(missing_ok=True)
        return path
    _run_qemu(
        [
            "create",
            "-f",
            QCOW2_FORMAT,
            "-o",
            f"compat={QCOW2_COMPAT},lazy_refcounts={QCOW2_LAZY_REFCOUNTS},"
            f"cluster_size={QCOW2_CLUSTER_SIZE}",
            str(path),
            str(fixture["virtual_size_bytes"]),
        ]
    )
    return path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def expected_sha256(fixture: dict[str, Any]) -> str:
    parts = fixture["sha256_parts"]
    if not isinstance(parts, list) or any(not isinstance(part, str) for part in parts):
        msg = "El fixture no contiene partes SHA-256 válidas."
        raise FixtureError(msg)
    return "".join(parts)


def verify_fixture(fixture: dict[str, Any], path: Path) -> None:
    validate_spec(fixture)
    if not path.is_file() or path.is_symlink():
        msg = f"El fixture no es un archivo regular: {path}"
        raise FixtureError(msg)
    try:
        info = json.loads(_run_qemu(["info", "--output=json", str(path)]))
    except json.JSONDecodeError:
        msg = f"qemu-img no devolvió JSON para {path.name}."
        raise FixtureError(msg) from None
    if (
        not isinstance(info, dict)
        or info.get("format") != QCOW2_FORMAT
        or info.get("virtual-size") != fixture["virtual_size_bytes"]
        or info.get("cluster-size") != QCOW2_CLUSTER_SIZE
    ):
        msg = f"El formato o tamaño de {path.name} no coincide con la matriz."
        raise FixtureError(msg)
    format_specific = info.get("format-specific")
    data = format_specific.get("data") if isinstance(format_specific, dict) else None
    if (
        not isinstance(data, dict)
        or data.get("compat") != QCOW2_COMPAT
        or data.get("lazy-refcounts") is not False
    ):
        msg = f"Las opciones QCOW2 de {path.name} no son deterministas."
        raise FixtureError(msg)
    actual_sha256 = sha256_file(path)
    expected = expected_sha256(fixture)
    if actual_sha256 != expected:
        msg = f"El SHA-256 de {path.name} no coincide: {actual_sha256} != {expected}"
        raise FixtureError(msg)


def generate_and_verify(output_dir: Path, matrix: dict[str, Any] | None = None) -> list[Path]:
    paths = []
    for fixture in fixture_specs(matrix):
        path = generate_fixture(fixture, output_dir)
        verify_fixture(fixture, path)
        paths.append(path)
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        matrix = load_matrix()
        paths = generate_and_verify(args.output_dir, matrix)
    except FixtureError as error:
        parser.exit(1, f"Error: {error}\n")
    for path in paths:
        sys.stdout.write(f"{path}\n")


if __name__ == "__main__":
    main()
