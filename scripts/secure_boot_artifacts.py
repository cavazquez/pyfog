#!/usr/bin/env python3
"""Sign and verify EFI PE/COFF artifacts for the PyFog Secure Boot release path."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_DATE_EPOCH = 1_704_067_200
SECURE_MANIFEST_NAME = "secure-boot-manifest.json"


class SecureBootArtifactError(ValueError):
    """A Secure Boot artifact invariant was not satisfied."""


def _tool(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        package = "sbsigntool" if name == "sbverify" else "osslsigncode"
        raise SecureBootArtifactError(
            f"Falta {name}; instalá {package} (en Ubuntu: sudo apt-get install {package})."
        )
    return executable


def _safe_file(path: Path, label: str, *, private: bool = False) -> Path:
    if path.is_symlink() or not path.is_file():
        raise SecureBootArtifactError(f"{label} no es un archivo regular: {path}")
    resolved = path.resolve()
    if private and (resolved == ROOT or ROOT in resolved.parents):
        raise SecureBootArtifactError(
            "La clave privada debe permanecer fuera del checkout de PyFog."
        )
    if private and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise SecureBootArtifactError(
            f"La clave privada debe tener permisos 0600 o más restrictivos: {path}"
        )
    return resolved


def _run(
    executable: str, arguments: list[str], *, source_date_epoch: int | None = None
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    if source_date_epoch is not None:
        environment["SOURCE_DATE_EPOCH"] = str(source_date_epoch)
        environment["TZ"] = "UTC"
        environment["LC_ALL"] = "C"
    result = subprocess.run(  # noqa: S603 - executable is resolved from PATH and args are explicit.
        [executable, *arguments],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "sin diagnóstico"
        raise SecureBootArtifactError(f"{Path(executable).name} falló: {detail}")
    return result


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _is_pe_image(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            header = stream.read(0x40)
            if len(header) < 0x40 or header[:2] != b"MZ":
                return False
            pe_offset = int.from_bytes(header[0x3C:0x40], "little")
            if pe_offset < 0 or pe_offset > 1024 * 1024:
                return False
            stream.seek(pe_offset)
            return stream.read(4) == b"PE\0\0"
    except OSError:
        return False


def _relative(root: Path, path: Path) -> str:
    relative = PurePosixPath(path.relative_to(root).as_posix())
    if (
        relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
        or not str(relative).isascii()
    ):
        raise SecureBootArtifactError(f"Ruta de artefacto insegura: {relative}")
    return str(relative)


def _pe_images(root: Path) -> list[Path]:
    if not root.is_dir() or root.is_symlink():
        raise SecureBootArtifactError(f"No existe un directorio de artefactos seguro: {root}")
    images: list[Path] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise SecureBootArtifactError(
                f"La publicación no puede contener enlaces simbólicos: {path}"
            )
        if path.is_file() and path.suffix.lower() == ".efi" and _is_pe_image(path):
            images.append(path)
    return images


def _copy_tree(source: Path, destination: Path) -> None:
    source = source.resolve()
    destination = destination.resolve()
    if not source.is_dir() or source.is_symlink():
        raise SecureBootArtifactError(f"No existe un directorio fuente seguro: {source}")
    if destination == source or source in destination.parents:
        raise SecureBootArtifactError(
            "El directorio de salida no puede estar dentro del de entrada."
        )
    if destination.exists():
        if destination.is_symlink() or not destination.is_dir() or any(destination.iterdir()):
            raise SecureBootArtifactError(f"El directorio de salida no está vacío: {destination}")
    else:
        destination.mkdir(parents=True)
    for path in sorted(source.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(source)
        target = destination / relative
        if path.is_symlink():
            raise SecureBootArtifactError(
                f"La publicación no puede contener enlaces simbólicos: {path}"
            )
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def _certificate_digest(certificate: Path) -> str:
    return _digest(certificate)


def _write_json(path: Path, value: dict[str, Any], source_date_epoch: int) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.utime(path, (source_date_epoch, source_date_epoch))


def _secure_manifest(root: Path, certificate: Path, source_date_epoch: int) -> dict[str, Any]:
    artifacts = [
        {
            "path": _relative(root, path),
            "sha256": _digest(path),
            "size": path.stat().st_size,
            "signature": "embedded-pe-coff-authenticode",
        }
        for path in _pe_images(root)
    ]
    if not artifacts:
        raise SecureBootArtifactError(
            "La publicación no contiene ningún artefacto PE/COFF para firmar."
        )
    return {
        "schema_version": 1,
        "format": "pyfog-secure-boot-artifacts",
        "source_date_epoch": source_date_epoch,
        "certificate_sha256": _certificate_digest(certificate),
        "artifacts": artifacts,
    }


def sign_artifacts(
    input_dir: Path,
    output_dir: Path,
    key: Path,
    certificate: Path,
    *,
    source_date_epoch: int = DEFAULT_SOURCE_DATE_EPOCH,
) -> dict[str, Any]:
    """Copy a publication and sign every `.efi` PE/COFF image in the copy."""

    if source_date_epoch < 0:
        raise SecureBootArtifactError("source_date_epoch debe ser no negativo.")
    key = _safe_file(key, "La clave privada", private=True)
    certificate = _safe_file(certificate, "El certificado público")
    _copy_tree(input_dir, output_dir)
    signer = _tool("osslsigncode")
    images = _pe_images(output_dir)
    if not images:
        raise SecureBootArtifactError(
            "La publicación no contiene ningún artefacto PE/COFF para firmar."
        )
    for image in images:
        temporary = image.with_name(f".{image.name}.signed")
        _run(
            signer,
            [
                "sign",
                "-certs",
                str(certificate),
                "-key",
                str(key),
                "-h",
                "sha256",
                "-time",
                str(source_date_epoch),
                "-in",
                str(image),
                "-out",
                str(temporary),
            ],
            source_date_epoch=source_date_epoch,
        )
        mode = stat.S_IMODE(image.stat().st_mode)
        temporary.replace(image)
        image.chmod(mode)
        os.utime(image, (source_date_epoch, source_date_epoch))
    manifest = _secure_manifest(output_dir, certificate, source_date_epoch)
    _write_json(output_dir / SECURE_MANIFEST_NAME, manifest, source_date_epoch)
    return manifest


def _load_manifest(root: Path) -> dict[str, Any]:
    path = root / SECURE_MANIFEST_NAME
    if not path.is_file() or path.is_symlink():
        raise SecureBootArtifactError(f"Falta {path}; el bundle no está firmado.")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SecureBootArtifactError(
            f"El manifiesto Secure Boot no es JSON válido: {path}"
        ) from error
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise SecureBootArtifactError("El manifiesto Secure Boot tiene un schema incompatible.")
    if value.get("format") != "pyfog-secure-boot-artifacts":
        raise SecureBootArtifactError("El manifiesto Secure Boot tiene un formato incompatible.")
    if not isinstance(value.get("artifacts"), list) or not value["artifacts"]:
        raise SecureBootArtifactError("El manifiesto Secure Boot no declara artefactos.")
    return value


def verify_artifacts(root: Path, certificate: Path) -> dict[str, Any]:
    """Verify hashes and embedded Authenticode signatures against one trusted certificate."""

    certificate = _safe_file(certificate, "El certificado público")
    manifest = _load_manifest(root)
    expected_certificate = manifest.get("certificate_sha256")
    if expected_certificate != _certificate_digest(certificate):
        raise SecureBootArtifactError(
            "El certificado no coincide con el fingerprint del manifiesto."
        )
    entries: list[dict[str, Any]] = []
    for item in manifest["artifacts"]:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise SecureBootArtifactError(
                "El manifiesto Secure Boot contiene una entrada inválida."
            )
        relative = PurePosixPath(item["path"])
        if (
            relative.is_absolute()
            or not str(relative).isascii()
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise SecureBootArtifactError("El manifiesto Secure Boot contiene una ruta insegura.")
        artifact = root / relative
        if artifact.is_symlink() or not artifact.is_file() or not _is_pe_image(artifact):
            raise SecureBootArtifactError(
                f"Artefacto Secure Boot faltante o no PE/COFF: {relative}"
            )
        if item.get("sha256") != _digest(artifact):
            raise SecureBootArtifactError(f"Checksum de firma inválido: {relative}")
        entries.append({"path": str(relative), "artifact": artifact})

    listed = {entry["path"] for entry in entries}
    discovered = {_relative(root, path) for path in _pe_images(root)}
    if listed != discovered:
        missing = sorted(discovered - listed)
        extra = sorted(listed - discovered)
        detail = ", ".join([f"faltan: {', '.join(missing)}"] if missing else [])
        if extra:
            detail = (
                f"{detail}; sobran: {', '.join(extra)}" if detail else f"sobran: {', '.join(extra)}"
            )
        raise SecureBootArtifactError(f"El manifiesto no cubre todos los PE/COFF ({detail}).")

    verifier = _tool("sbverify")
    for entry in entries:
        _run(verifier, ["--cert", str(certificate), str(entry["artifact"])])
    return manifest


def _create_keypair(directory: Path, name: str) -> tuple[Path, Path]:
    openssl = shutil.which("openssl")
    if openssl is None:
        raise SecureBootArtifactError("Falta openssl para la prueba de firma Secure Boot.")
    key = directory / f"{name}.key.pem"
    certificate = directory / f"{name}.cert.pem"
    _run(
        openssl,
        [
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
            "-sha256",
            "-days",
            "1",
            "-subj",
            f"/CN=PyFog ephemeral Secure Boot {name}",
        ],
    )
    key.chmod(0o600)
    return key, certificate


def _fixture_path(value: Path | None) -> Path:
    candidates = [
        value,
        Path("/usr/lib/shim/shimx64.efi.signed.latest"),
        Path("/usr/lib/shim/shimx64.efi.dualsigned"),
        Path("/usr/lib/grub/x86_64-efi-signed/grubx64.efi.signed"),
    ]
    for candidate in candidates:
        if (
            candidate is not None
            and candidate.is_file()
            and not candidate.is_symlink()
            and _is_pe_image(candidate)
        ):
            return candidate
    raise SecureBootArtifactError(
        "No se encontró un EFI PE/COFF para el fixture; instalá shim-signed o "
        "indicá --fixture PATH."
    )


def run_artifact_checks(work_dir: Path, fixture: Path | None = None) -> dict[str, bool]:
    """Exercise valid, reproducible, tampered, unsigned, and untrusted artifact outcomes."""

    work_dir.mkdir(parents=True, exist_ok=True)
    source = work_dir / "unsigned"
    source.mkdir()
    fixture_path = _fixture_path(fixture)
    shutil.copy2(fixture_path, source / "BOOTX64.EFI")
    trusted_key, trusted_certificate = _create_keypair(work_dir, "trusted")
    untrusted_key, untrusted_certificate = _create_keypair(work_dir, "untrusted")
    try:
        trusted_a = work_dir / "trusted-a"
        trusted_b = work_dir / "trusted-b"
        untrusted = work_dir / "untrusted-key"
        sign_artifacts(source, trusted_a, trusted_key, trusted_certificate)
        time.sleep(2)
        sign_artifacts(source, trusted_b, trusted_key, trusted_certificate)
        sign_artifacts(source, untrusted, untrusted_key, untrusted_certificate)

        valid = True
        verify_artifacts(trusted_a, trusted_certificate)
        reproducible = (trusted_a / "BOOTX64.EFI").read_bytes() == (
            trusted_b / "BOOTX64.EFI"
        ).read_bytes()

        tampered = work_dir / "tampered"
        shutil.copytree(trusted_a, tampered)
        tampered_image = tampered / "BOOTX64.EFI"
        tampered_bytes = bytearray(tampered_image.read_bytes())
        tampered_bytes[0] ^= 1
        tampered_image.write_bytes(tampered_bytes)
        try:
            verify_artifacts(tampered, trusted_certificate)
        except SecureBootArtifactError:
            tampered_rejected = True
        else:
            tampered_rejected = False

        try:
            verify_artifacts(source, trusted_certificate)
        except SecureBootArtifactError:
            unsigned_rejected = True
        else:
            unsigned_rejected = False

        try:
            verify_artifacts(untrusted, trusted_certificate)
        except SecureBootArtifactError:
            untrusted_rejected = True
        else:
            untrusted_rejected = False
        return {
            "valid_signature": valid,
            "reproducible_signature": reproducible,
            "tampered_artifact_rejected": tampered_rejected,
            "unsigned_artifact_rejected": unsigned_rejected,
            "untrusted_key_rejected": untrusted_rejected,
        }
    finally:
        trusted_key.unlink(missing_ok=True)
        trusted_certificate.unlink(missing_ok=True)
        untrusted_key.unlink(missing_ok=True)
        untrusted_certificate.unlink(missing_ok=True)


def _source_date_epoch(value: str) -> int:
    if not value.isdigit():
        raise argparse.ArgumentTypeError("source-date-epoch debe ser un entero no negativo")
    return int(value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    sign_parser = subparsers.add_parser("sign", help="firmar una publicación PE/COFF")
    sign_parser.add_argument("--input-dir", type=Path, required=True)
    sign_parser.add_argument("--output-dir", type=Path, required=True)
    sign_parser.add_argument("--key", type=Path, required=True, help="ruta externa a la privada")
    sign_parser.add_argument("--cert", type=Path, required=True, help="certificado público")
    sign_parser.add_argument(
        "--source-date-epoch", type=_source_date_epoch, default=DEFAULT_SOURCE_DATE_EPOCH
    )

    verify_parser = subparsers.add_parser("verify", help="verificar una publicación firmada")
    verify_parser.add_argument("--input-dir", type=Path, required=True)
    verify_parser.add_argument("--cert", type=Path, required=True)

    check_parser = subparsers.add_parser("check", help="ejecutar negativos de Secure Boot")
    check_parser.add_argument("--work-dir", type=Path)
    check_parser.add_argument("--fixture", type=Path)

    args = parser.parse_args()
    try:
        if args.command == "sign":
            sign_artifacts(
                args.input_dir,
                args.output_dir,
                args.key,
                args.cert,
                source_date_epoch=args.source_date_epoch,
            )
            sys.stdout.write(f"Secure Boot: artefactos firmados en {args.output_dir}\n")
        elif args.command == "verify":
            manifest = verify_artifacts(args.input_dir, args.cert)
            sys.stdout.write(f"Secure Boot: {len(manifest['artifacts'])} artefactos verificados\n")
        else:
            if args.work_dir is not None:
                outcomes = run_artifact_checks(args.work_dir, args.fixture)
            else:
                with tempfile.TemporaryDirectory(prefix="pyfog-secure-artifacts-") as temporary:
                    outcomes = run_artifact_checks(Path(temporary), args.fixture)
            failed = [name for name, passed in outcomes.items() if not passed]
            if failed:
                sys.stderr.write("Secure Boot: fallaron " + ", ".join(failed) + "\n")
                return 1
            sys.stdout.write("Secure Boot artifacts: firma válida, reproducible y negativos PASS\n")
    except (OSError, SecureBootArtifactError) as error:
        sys.stderr.write(f"secure-boot-artifacts: error: {error}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
