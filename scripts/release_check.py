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

from scripts.secure_boot_artifacts import (
    SECURE_MANIFEST_NAME,
    SecureBootArtifactError,
    verify_artifacts,
)

ROOT = Path(__file__).resolve().parents[1]
VERSION_PATTERN = re.compile(r'version="([^"]+)"')
CHECKSUM_PATTERN = re.compile(r"^([0-9a-f]{64})  (.+)$")


class ReleaseCheckError(ValueError):
    """A release invariant was not satisfied."""


def _require_bundles(agent_exists: bool, pxe_exists: bool) -> None:
    if not agent_exists or not pxe_exists:
        msg = "Se deben proporcionar ambos bundles: --agent-dir y --pxe-dir."
        raise ReleaseCheckError(msg)


def _require_secure_boot_manifest(pxe_dir: Path) -> None:
    if not (pxe_dir / SECURE_MANIFEST_NAME).is_file():
        msg = "El bundle PXE no contiene firmas Secure Boot requeridas."
        raise ReleaseCheckError(msg)


def _reject_secure_boot_without_bundles(secure_boot_cert: Path | None) -> None:
    if secure_boot_cert is not None:
        msg = "--secure-boot-cert requiere bundles de release."
        raise ReleaseCheckError(msg)


def read_project_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = data.get("project", {}).get("version")
    if not isinstance(version, str) or not version:
        msg = "pyproject.toml no contiene una versión de proyecto válida."
        raise ReleaseCheckError(msg)
    return version


def read_app_version() -> str:
    content = (ROOT / "pyfog/app.py").read_text(encoding="utf-8")
    match = VERSION_PATTERN.search(content)
    if match is None:
        msg = "pyfog/app.py no declara la versión de la aplicación."
        raise ReleaseCheckError(msg)
    return match.group(1)


def read_agent_version() -> str:
    version = (ROOT / "agent/VERSION").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", version):
        msg = "agent/VERSION no contiene una versión válida."
        raise ReleaseCheckError(msg)
    return version


def check_source(expected: str | None) -> str:
    versions = {
        "pyproject.toml": read_project_version(),
        "pyfog/app.py": read_app_version(),
        "agent/VERSION": read_agent_version(),
    }
    if len(set(versions.values())) != 1:
        values = ", ".join(f"{name}={version}" for name, version in versions.items())
        msg = f"Las versiones no coinciden: {values}."
        raise ReleaseCheckError(msg)
    version = next(iter(versions.values()))
    if expected is not None and version != expected:
        msg = f"Se esperaba la versión {expected}, se encontró {version}."
        raise ReleaseCheckError(msg)
    required = (
        "LICENSE",
        "README.md",
        "CHANGELOG.md",
        "docs/provenance.md",
        "docs/release-0.1.0.md",
        "agent/build-agent",
        "pxe/build-pxe",
        "scripts/package_release.py",
        "scripts/secure_boot_artifacts.py",
        "docs/secure-boot.md",
    )
    missing = [path for path in required if not (ROOT / path).is_file()]
    if missing:
        msg = f"Faltan archivos requeridos para release: {', '.join(missing)}."
        raise ReleaseCheckError(msg)
    return version


def checksum_entries(path: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        match = CHECKSUM_PATTERN.fullmatch(line)
        if match is None:
            msg = f"Formato inválido en {path}:{line_number}."
            raise ReleaseCheckError(msg)
        digest, relative = match.groups()
        candidate = PurePosixPath(relative)
        if (
            candidate.is_absolute()
            or not relative.isascii()
            or "\\" in relative
            or any(part in {"", ".", ".."} for part in candidate.parts)
        ):
            msg = f"Ruta insegura en {path}:{line_number}."
            raise ReleaseCheckError(msg)
        if relative in entries:
            msg = f"Archivo repetido en {path}: {relative}."
            raise ReleaseCheckError(msg)
        entries[relative] = digest
    if not entries:
        msg = f"El archivo de checksums está vacío: {path}."
        raise ReleaseCheckError(msg)
    return entries


def verify_checksum_bundle(directory: Path) -> dict[str, Any]:
    manifest_path = directory / "manifest.json"
    sums_path = directory / "SHA256SUMS"
    if not directory.is_dir() or not manifest_path.is_file() or not sums_path.is_file():
        msg = f"El bundle está incompleto: {directory}."
        raise ReleaseCheckError(msg)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        msg = f"El manifiesto no es JSON válido: {manifest_path}."
        raise ReleaseCheckError(msg) from error
    if not isinstance(manifest, dict):
        msg = f"El manifiesto no es un objeto JSON: {manifest_path}."
        raise ReleaseCheckError(msg)
    entries = checksum_entries(sums_path)
    for relative, expected in entries.items():
        artifact = directory / PurePosixPath(relative)
        if artifact.is_symlink() or not artifact.is_file():
            msg = f"Falta el artefacto listado: {directory / relative}."
            raise ReleaseCheckError(msg)
        actual = hashlib.sha256(artifact.read_bytes()).hexdigest()
        if actual != expected:
            msg = f"Checksum inválido: {directory / relative}."
            raise ReleaseCheckError(msg)
    return manifest


def check_agent(directory: Path, version: str) -> None:
    manifest = verify_checksum_bundle(directory)
    if manifest.get("schema_version") != 1 or manifest.get("format") != "pyfog-agent-initramfs":
        msg = "El bundle del agente tiene un formato incompatible."
        raise ReleaseCheckError(msg)
    if manifest.get("agent_version") != version:
        msg = "La versión del agente no coincide con el código fuente."
        raise ReleaseCheckError(msg)
    for relative in ("vmlinuz", "initramfs.img", "manifest.json"):
        if relative not in checksum_entries(directory / "SHA256SUMS"):
            msg = f"Falta {relative} en el bundle del agente."
            raise ReleaseCheckError(msg)


def check_secure_boot_manifest(directory: Path) -> None:
    manifest_path = directory / SECURE_MANIFEST_NAME
    if not manifest_path.is_file() or manifest_path.is_symlink():
        msg = f"Falta el manifiesto Secure Boot: {manifest_path}."
        raise ReleaseCheckError(msg)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        msg = f"El manifiesto Secure Boot no es JSON válido: {manifest_path}."
        raise ReleaseCheckError(msg) from error
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or manifest.get("format") != "pyfog-secure-boot-artifacts"
        or not isinstance(manifest.get("certificate_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", manifest["certificate_sha256"])
    ):
        msg = "El manifiesto Secure Boot es incompatible."
        raise ReleaseCheckError(msg)
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        msg = "El manifiesto Secure Boot no declara artefactos."
        raise ReleaseCheckError(msg)
    for item in artifacts:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            msg = "El manifiesto Secure Boot contiene una entrada inválida."
            raise ReleaseCheckError(msg)
        relative = PurePosixPath(item["path"])
        if (
            relative.is_absolute()
            or not str(relative).isascii()
            or any(part in {"", ".", ".."} for part in relative.parts)
            or relative.suffix.lower() != ".efi"
            or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256", "")))
            or item.get("signature") != "embedded-pe-coff-authenticode"
        ):
            msg = "El manifiesto Secure Boot contiene una ruta o firma inválida."
            raise ReleaseCheckError(msg)
        artifact = directory / relative
        if artifact.is_symlink() or not artifact.is_file():
            msg = f"Falta el artefacto EFI firmado: {artifact}."
            raise ReleaseCheckError(msg)
        if hashlib.sha256(artifact.read_bytes()).hexdigest() != item["sha256"]:
            msg = f"Checksum de firma inválido: {artifact}."
            raise ReleaseCheckError(msg)


def check_pxe(directory: Path, version: str, secure_boot_cert: Path | None = None) -> None:
    manifest = verify_checksum_bundle(directory)
    if manifest.get("schema_version") != 1 or manifest.get("format") != "pyfog-pxe-profile":
        msg = "El bundle PXE tiene un formato incompatible."
        raise ReleaseCheckError(msg)
    if manifest.get("profile_version") != version:
        msg = "La versión del perfil PXE no coincide con el agente."
        raise ReleaseCheckError(msg)
    if not isinstance(manifest.get("base_url"), str) or not manifest["base_url"].startswith(
        "https://"
    ):
        msg = "El perfil PXE no tiene una URL HTTPS válida."
        raise ReleaseCheckError(msg)
    secure_manifest = directory / SECURE_MANIFEST_NAME
    if secure_manifest.exists():
        check_secure_boot_manifest(directory)
        if secure_boot_cert is not None:
            try:
                verify_artifacts(directory, secure_boot_cert)
            except (OSError, SecureBootArtifactError) as error:
                msg = f"Firmas Secure Boot inválidas: {error}"
                raise ReleaseCheckError(msg) from error
    elif secure_boot_cert is not None:
        msg = "Se indicó un certificado Secure Boot pero el bundle no está firmado."
        raise ReleaseCheckError(msg)


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
    parser.add_argument(
        "--require-secure-boot",
        action="store_true",
        help="Exigir el manifiesto y las firmas Secure Boot del bundle PXE.",
    )
    parser.add_argument(
        "--secure-boot-cert",
        type=Path,
        help="Certificado público para verificar el bundle PXE firmado.",
    )
    args = parser.parse_args()
    try:
        version = check_source(args.version)
        agent_exists = args.agent_dir.is_dir()
        pxe_exists = args.pxe_dir.is_dir()
        if args.require_artifacts or args.require_secure_boot or agent_exists or pxe_exists:
            _require_bundles(agent_exists, pxe_exists)
            check_agent(args.agent_dir, version)
            check_pxe(args.pxe_dir, version, args.secure_boot_cert)
            if args.require_secure_boot and not (args.pxe_dir / SECURE_MANIFEST_NAME).is_file():
                _require_secure_boot_manifest(args.pxe_dir)
        elif args.secure_boot_cert is not None:
            _reject_secure_boot_without_bundles(args.secure_boot_cert)
    except (OSError, ReleaseCheckError) as error:
        sys.stderr.write(f"release-check: error: {error}\n")
        return 1
    sys.stdout.write(f"Release {version}: metadatos y documentación válidos.\n")
    if args.require_artifacts or args.require_secure_boot or agent_exists or pxe_exists:
        sys.stdout.write("Bundles agent/PXE: checksums y manifiestos válidos.\n")
    else:
        sys.stdout.write(
            "Bundles agent/PXE: omitidos; usar --require-artifacts para una entrega final.\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
