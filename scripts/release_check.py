"""Validate the source metadata and optional release artifact bundles."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
VERSION_PATTERN = re.compile(r'version="([^"]+)"')
CHECKSUM_PATTERN = re.compile(r"^([0-9a-f]{64})  (.+)$")


class ReleaseCheckError(ValueError):
    """A release invariant was not satisfied."""


def read_project_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = data.get("project", {}).get("version")
    if not isinstance(version, str) or not version:
        raise ReleaseCheckError("pyproject.toml no contiene una versión de proyecto válida.")
    return version


def read_app_version() -> str:
    content = (ROOT / "pyfog/app.py").read_text(encoding="utf-8")
    match = VERSION_PATTERN.search(content)
    if match is None:
        raise ReleaseCheckError("pyfog/app.py no declara la versión de la aplicación.")
    return match.group(1)


def read_agent_version() -> str:
    version = (ROOT / "agent/VERSION").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", version):
        raise ReleaseCheckError("agent/VERSION no contiene una versión válida.")
    return version


def check_source(expected: str | None) -> str:
    versions = {
        "pyproject.toml": read_project_version(),
        "pyfog/app.py": read_app_version(),
        "agent/VERSION": read_agent_version(),
    }
    if len(set(versions.values())) != 1:
        values = ", ".join(f"{name}={version}" for name, version in versions.items())
        raise ReleaseCheckError(f"Las versiones no coinciden: {values}.")
    version = next(iter(versions.values()))
    if expected is not None and version != expected:
        raise ReleaseCheckError(f"Se esperaba la versión {expected}, se encontró {version}.")
    required = (
        "LICENSE",
        "README.md",
        "CHANGELOG.md",
        "docs/provenance.md",
        "docs/release-0.1.0.md",
        "agent/build-agent",
        "pxe/build-pxe",
        "scripts/package_release.py",
    )
    missing = [path for path in required if not (ROOT / path).is_file()]
    if missing:
        raise ReleaseCheckError(f"Faltan archivos requeridos para release: {', '.join(missing)}.")
    return version


def checksum_entries(path: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        match = CHECKSUM_PATTERN.fullmatch(line)
        if match is None:
            raise ReleaseCheckError(f"Formato inválido en {path}:{line_number}.")
        digest, relative = match.groups()
        candidate = PurePosixPath(relative)
        if (
            candidate.is_absolute()
            or not relative.isascii()
            or "\\" in relative
            or any(part in {"", ".", ".."} for part in candidate.parts)
        ):
            raise ReleaseCheckError(f"Ruta insegura en {path}:{line_number}.")
        if relative in entries:
            raise ReleaseCheckError(f"Archivo repetido en {path}: {relative}.")
        entries[relative] = digest
    if not entries:
        raise ReleaseCheckError(f"El archivo de checksums está vacío: {path}.")
    return entries


def verify_checksum_bundle(directory: Path) -> dict[str, Any]:
    manifest_path = directory / "manifest.json"
    sums_path = directory / "SHA256SUMS"
    if not directory.is_dir() or not manifest_path.is_file() or not sums_path.is_file():
        raise ReleaseCheckError(f"El bundle está incompleto: {directory}.")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReleaseCheckError(f"El manifiesto no es JSON válido: {manifest_path}.") from error
    if not isinstance(manifest, dict):
        raise ReleaseCheckError(f"El manifiesto no es un objeto JSON: {manifest_path}.")
    entries = checksum_entries(sums_path)
    for relative, expected in entries.items():
        artifact = directory / PurePosixPath(relative)
        if artifact.is_symlink() or not artifact.is_file():
            raise ReleaseCheckError(f"Falta el artefacto listado: {directory / relative}.")
        actual = hashlib.sha256(artifact.read_bytes()).hexdigest()
        if actual != expected:
            raise ReleaseCheckError(f"Checksum inválido: {directory / relative}.")
    return manifest


def check_agent(directory: Path, version: str) -> None:
    manifest = verify_checksum_bundle(directory)
    if manifest.get("schema_version") != 1 or manifest.get("format") != "pyfog-agent-initramfs":
        raise ReleaseCheckError("El bundle del agente tiene un formato incompatible.")
    if manifest.get("agent_version") != version:
        raise ReleaseCheckError("La versión del agente no coincide con el código fuente.")
    for relative in ("vmlinuz", "initramfs.img", "manifest.json"):
        if relative not in checksum_entries(directory / "SHA256SUMS"):
            raise ReleaseCheckError(f"Falta {relative} en el bundle del agente.")


def check_pxe(directory: Path, version: str) -> None:
    manifest = verify_checksum_bundle(directory)
    if manifest.get("schema_version") != 1 or manifest.get("format") != "pyfog-pxe-profile":
        raise ReleaseCheckError("El bundle PXE tiene un formato incompatible.")
    if manifest.get("profile_version") != version:
        raise ReleaseCheckError("La versión del perfil PXE no coincide con el agente.")
    if not isinstance(manifest.get("base_url"), str) or not manifest["base_url"].startswith(
        "https://"
    ):
        raise ReleaseCheckError("El perfil PXE no tiene una URL HTTPS válida.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", help="Versión esperada; por defecto, la del proyecto.")
    parser.add_argument("--agent-dir", type=Path, default=ROOT / "dist/agent")
    parser.add_argument("--pxe-dir", type=Path, default=ROOT / "dist/pxe")
    parser.add_argument(
        "--require-artifacts",
        action="store_true",
        help="Exigir y verificar los bundles del agente y PXE.",
    )
    args = parser.parse_args()
    try:
        version = check_source(args.version)
        agent_exists = args.agent_dir.is_dir()
        pxe_exists = args.pxe_dir.is_dir()
        if args.require_artifacts or agent_exists or pxe_exists:
            if not agent_exists or not pxe_exists:
                raise ReleaseCheckError(
                    "Se deben proporcionar ambos bundles: --agent-dir y --pxe-dir."
                )
            check_agent(args.agent_dir, version)
            check_pxe(args.pxe_dir, version)
    except (OSError, ReleaseCheckError) as error:
        sys.stderr.write(f"release-check: error: {error}\n")
        return 1
    sys.stdout.write(f"Release {version}: metadatos y documentación válidos.\n")
    if args.require_artifacts or agent_exists or pxe_exists:
        sys.stdout.write("Bundles agent/PXE: checksums y manifiestos válidos.\n")
    else:
        sys.stdout.write(
            "Bundles agent/PXE: omitidos; usar --require-artifacts para una entrega final.\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
