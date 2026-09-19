#!/usr/bin/env python3
"""Exercise the Secure Boot policy's valid, tampered, and untrusted signatures."""

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


class SecureBootPolicyError(ValueError):
    """The local signature-policy fixture could not be executed safely."""


def _openssl() -> str:
    path = shutil.which("openssl")
    if path is None:
        msg = "Falta openssl; instalá el paquete openssl."
        raise SecureBootPolicyError(msg)
    return path


def _run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(  # noqa: S603 - openssl is resolved from PATH intentionally.
            [_openssl(), *args],
            check=check,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        msg = "Falta openssl; instalá el paquete openssl."
        raise SecureBootPolicyError(msg) from None
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip() or error.stdout.strip() or "sin diagnóstico"
        msg = f"openssl falló: {detail}"
        raise SecureBootPolicyError(msg) from None
    return result


def _create_keypair(directory: Path, name: str) -> tuple[Path, Path]:
    private_key = directory / f"{name}.key.pem"
    certificate = directory / f"{name}.cert.pem"
    _run(
        [
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(private_key),
            "-out",
            str(certificate),
            "-sha256",
            "-days",
            "1",
            "-subj",
            f"/CN=PyFog ephemeral {name}",
        ]
    )
    return private_key, certificate


def _public_key(certificate: Path, destination: Path) -> None:
    result = _run(["x509", "-in", str(certificate), "-pubkey", "-noout"])
    destination.write_text(result.stdout, encoding="ascii")


def _verify(public_key: Path, signature: Path, payload: Path) -> bool:
    result = _run(
        [
            "dgst",
            "-sha256",
            "-verify",
            str(public_key),
            "-signature",
            str(signature),
            str(payload),
        ],
        check=False,
    )
    return result.returncode == 0


def run_policy_checks(directory: Path) -> dict[str, bool]:
    """Return the expected outcomes without retaining any private material."""

    directory.mkdir(parents=True, exist_ok=True)
    private_keys = (directory / "trusted.key.pem", directory / "untrusted.key.pem")
    try:
        trusted_key, trusted_certificate = _create_keypair(directory, "trusted")
        _untrusted_key, untrusted_certificate = _create_keypair(directory, "untrusted")
        trusted_public = directory / "trusted.pub.pem"
        untrusted_public = directory / "untrusted.pub.pem"
        _public_key(trusted_certificate, trusted_public)
        _public_key(untrusted_certificate, untrusted_public)

        payload = directory / "efi-payload.bin"
        payload.write_bytes(b"pyfog-secure-boot-policy-fixture-v1\n")
        signature = directory / "efi-payload.sig"
        _run(["dgst", "-sha256", "-sign", str(trusted_key), "-out", str(signature), str(payload)])

        tampered_payload = directory / "efi-payload-tampered.bin"
        tampered_payload.write_bytes(b"pyfog-secure-boot-policy-fixture-tampered\n")
        invalid_signature = directory / "efi-payload-invalid.sig"
        signature_bytes = signature.read_bytes()
        invalid_signature.write_bytes(bytes([signature_bytes[0] ^ 1]) + signature_bytes[1:])

        return {
            "valid_signature": _verify(trusted_public, signature, payload),
            "tampered_payload_rejected": not _verify(trusted_public, signature, tampered_payload),
            "invalid_signature_rejected": not _verify(trusted_public, invalid_signature, payload),
            "untrusted_key_rejected": not _verify(untrusted_public, signature, payload),
        }
    finally:
        for private_key in private_keys:
            private_key.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, help="Directorio temporal para el diagnóstico")
    args = parser.parse_args()
    try:
        if args.work_dir is not None:
            outcomes = run_policy_checks(args.work_dir)
        else:
            with tempfile.TemporaryDirectory(prefix="pyfog-secure-boot-") as temporary:
                outcomes = run_policy_checks(Path(temporary))
    except SecureBootPolicyError as error:
        parser.exit(1, f"Error: {error}\n")
    failed = [name for name, passed in outcomes.items() if not passed]
    if failed:
        parser.exit(1, "Error: fallaron las comprobaciones: " + ", ".join(failed) + "\n")
    sys.stdout.write("Secure Boot policy: firma válida y negativos de payload/firma/clave PASS\n")


if __name__ == "__main__":
    main()
