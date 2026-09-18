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


class FixtureError(ValueError):
    """A fixture definition or generated disk failed its safety checks."""


def load_matrix(path: Path = MATRIX_PATH) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FixtureError(f"No se pudo leer la matriz de fixtures: {error}") from None
    if not isinstance(value, dict):
        raise FixtureError("La matriz de fixtures debe ser un objeto JSON.")
    if value.get("schema_version") != 1:
        raise FixtureError("La matriz de fixtures requiere schema_version 1.")
    generator = value.get("generator")
    if not isinstance(generator, dict):
        raise FixtureError("La matriz no declara opciones deterministas de generación.")
    if (
        generator.get("format") != QCOW2_FORMAT
        or generator.get("compat") != QCOW2_COMPAT
        or generator.get("cluster_size") != QCOW2_CLUSTER_SIZE
        or generator.get("lazy_refcounts") is not False
    ):
        raise FixtureError("La matriz no usa las opciones QCOW2 deterministas requeridas.")
    fixtures = value.get("fixtures")
    if not isinstance(fixtures, list) or not fixtures:
        raise FixtureError("La matriz debe declarar al menos un fixture.")
    ids: set[str] = set()
    files: set[str] = set()
    for fixture in fixtures:
        validate_spec(fixture)
        fixture_id = fixture["id"]
        filename = fixture["file"]
        if fixture_id in ids or filename in files:
            raise FixtureError("La matriz contiene IDs o archivos repetidos.")
        ids.add(fixture_id)
        files.add(filename)
    return value


def validate_spec(fixture: object) -> None:
    if not isinstance(fixture, dict):
        raise FixtureError("Cada fixture debe ser un objeto JSON.")
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
        raise FixtureError(f"El fixture no declara campos requeridos: {missing}.")
    fixture_id = fixture["id"]
    filename = fixture["file"]
    if (
        not isinstance(fixture_id, str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", fixture_id)
        or not isinstance(filename, str)
        or filename != f"{fixture_id}.qcow2"
    ):
        raise FixtureError("El ID o archivo del fixture no es seguro.")
    virtual_size = fixture["virtual_size_bytes"]
    if type(virtual_size) is not int or virtual_size <= 0 or virtual_size % 4096:
        raise FixtureError(f"El tamaño virtual de {fixture_id} no es válido.")
    sha256_parts = fixture["sha256_parts"]
    if (
        not isinstance(sha256_parts, list)
        or len(sha256_parts) != 8
        or any(
            not isinstance(part, str) or len(part) != 8 or not re.fullmatch(r"[0-9a-f]{8}", part)
            for part in sha256_parts
        )
    ):
        raise FixtureError(f"Las partes SHA-256 de {fixture_id} no son válidas.")
    if fixture["status"] not in {"supported", "rejected"}:
        raise FixtureError(f"El estado de {fixture_id} no es válido.")
    for field in ("checks", "restrictions"):
        value = fixture[field]
        if (
            not isinstance(value, list)
            or not value
            or any(not isinstance(item, str) or not item for item in value)
        ):
            raise FixtureError(f"{field} de {fixture_id} no es una lista válida.")
    if fixture["status"] == "rejected":
        errors = fixture.get("expected_errors")
        if (
            not isinstance(errors, list)
            or not errors
            or any(not isinstance(item, str) or not item for item in errors)
        ):
            raise FixtureError(f"{fixture_id} debe declarar errores esperados.")
    capabilities = fixture["capabilities"]
    if not isinstance(capabilities, dict):
        raise FixtureError(f"{fixture_id} no declara capacidades.")
    required_capabilities = {
        "firmware",
        "partition_table",
        "disks",
        "filesystems",
        "encryption",
        "volumes",
    }
    if not required_capabilities.issubset(capabilities):
        raise FixtureError(f"Las capacidades de {fixture_id} están incompletas.")
    if type(capabilities["disks"]) is not int or capabilities["disks"] < 1:
        raise FixtureError(f"La cantidad de discos de {fixture_id} no es válida.")
    filesystems = capabilities["filesystems"]
    if (
        not isinstance(filesystems, list)
        or not filesystems
        or any(not isinstance(item, str) or not item for item in filesystems)
    ):
        raise FixtureError(f"Los filesystems de {fixture_id} no son válidos.")


def fixture_specs(matrix: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    payload = matrix if matrix is not None else load_matrix()
    fixtures = payload["fixtures"]
    if not isinstance(fixtures, list):
        raise FixtureError("La matriz no contiene una lista de fixtures.")
    return [fixture for fixture in fixtures if isinstance(fixture, dict)]


def _qemu_img() -> str:
    path = shutil.which("qemu-img")
    if path is None:
        raise FixtureError("Falta qemu-img; instalá el paquete qemu-utils.")
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
        raise FixtureError("Falta qemu-img; instalá el paquete qemu-utils.") from None
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip() or error.stdout.strip() or "sin diagnóstico"
        raise FixtureError(f"qemu-img falló: {detail}") from None
    return result.stdout


def generate_fixture(fixture: dict[str, Any], output_dir: Path) -> Path:
    validate_spec(fixture)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / str(fixture["file"])
    if path.exists() or path.is_symlink():
        raise FixtureError(f"No se sobrescribe el fixture existente: {path}")
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
        raise FixtureError("El fixture no contiene partes SHA-256 válidas.")
    return "".join(parts)


def verify_fixture(fixture: dict[str, Any], path: Path) -> None:
    validate_spec(fixture)
    if not path.is_file() or path.is_symlink():
        raise FixtureError(f"El fixture no es un archivo regular: {path}")
    try:
        info = json.loads(_run_qemu(["info", "--output=json", str(path)]))
    except json.JSONDecodeError:
        raise FixtureError(f"qemu-img no devolvió JSON para {path.name}.") from None
    if (
        not isinstance(info, dict)
        or info.get("format") != QCOW2_FORMAT
        or info.get("virtual-size") != fixture["virtual_size_bytes"]
        or info.get("cluster-size") != QCOW2_CLUSTER_SIZE
    ):
        raise FixtureError(f"El formato o tamaño de {path.name} no coincide con la matriz.")
    format_specific = info.get("format-specific")
    data = format_specific.get("data") if isinstance(format_specific, dict) else None
    if (
        not isinstance(data, dict)
        or data.get("compat") != QCOW2_COMPAT
        or data.get("lazy-refcounts") is not False
    ):
        raise FixtureError(f"Las opciones QCOW2 de {path.name} no son deterministas.")
    actual_sha256 = sha256_file(path)
    expected = expected_sha256(fixture)
    if actual_sha256 != expected:
        raise FixtureError(f"El SHA-256 de {path.name} no coincide: {actual_sha256} != {expected}")


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
