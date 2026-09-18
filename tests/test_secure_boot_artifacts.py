import shutil
from pathlib import Path

import pytest

from scripts.secure_boot_artifacts import run_artifact_checks


def test_secure_boot_artifact_signatures_are_reproducible_and_fail_closed(tmp_path: Path) -> None:
    required = ("openssl", "sbsign", "sbverify")
    if any(shutil.which(command) is None for command in required):
        pytest.skip("faltan herramientas Authenticode para el fixture Secure Boot")
    fixtures = (
        Path("/usr/lib/shim/shimx64.efi.signed.latest"),
        Path("/usr/lib/shim/shimx64.efi.dualsigned"),
        Path("/usr/lib/grub/x86_64-efi-signed/grubx64.efi.signed"),
    )
    if not any(path.is_file() for path in fixtures):
        pytest.skip("no hay un EFI del sistema para el fixture Secure Boot")

    assert run_artifact_checks(tmp_path) == {
        "valid_signature": True,
        "reproducible_signature": True,
        "tampered_artifact_rejected": True,
        "unsigned_artifact_rejected": True,
        "untrusted_key_rejected": True,
    }
