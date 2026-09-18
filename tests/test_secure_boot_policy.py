import shutil
from pathlib import Path

import pytest

from scripts.secure_boot_policy import run_policy_checks


def test_secure_boot_policy_rejects_invalid_and_untrusted_signatures(tmp_path: Path) -> None:
    if shutil.which("openssl") is None:
        pytest.skip("openssl no está instalado")
    assert run_policy_checks(tmp_path) == {
        "valid_signature": True,
        "tampered_payload_rejected": True,
        "invalid_signature_rejected": True,
        "untrusted_key_rejected": True,
    }
