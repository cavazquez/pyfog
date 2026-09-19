import json
from pathlib import Path

import pytest

from pyfog.platform_inventory import (
    PlatformInventoryError,
    platform_transport_contract,
    validate_macos_evaluation_inventory,
    validate_windows_inventory,
)

FIXTURES = Path(__file__).parent / "fixtures" / "platform"


def load(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_windows_contract_is_versioned_and_requires_stable_disk_identity():
    inventory = validate_windows_inventory(load("windows-inventory.json"))
    assert inventory.os.id == "windows"
    assert inventory.disks[0].stable_id == "serial:WIN-DISK-001"
    assert platform_transport_contract("windows")["imaging"] == "out-of-scope"

    invalid = load("windows-inventory.json")
    invalid["disks"][0]["stable_id"] = "path:PhysicalDrive0"  # type: ignore[index]
    with pytest.raises(PlatformInventoryError, match="identidad"):
        validate_windows_inventory(invalid)


def test_macos_fixture_is_non_destructive_evaluation_only():
    inventory = validate_macos_evaluation_inventory(load("macos-inventory.json"))
    assert inventory.os.id == "macos"
    assert platform_transport_contract("macos")["imaging"] == "out-of-scope"
