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


def test_platform_contract_rejects_invalid_payloads_and_profiles():
    with pytest.raises(PlatformInventoryError, match="JSON válido"):
        validate_windows_inventory(b"{")
    with pytest.raises(PlatformInventoryError, match="objeto JSON"):
        validate_windows_inventory("[]")

    invalid_model = load("windows-inventory.json")
    invalid_model.pop("hostname")
    with pytest.raises(PlatformInventoryError, match="Inventario de windows inválido"):
        validate_windows_inventory(invalid_model)

    wrong_platform = load("windows-inventory.json")
    wrong_platform["os"]["id"] = "macos"  # type: ignore[index]
    with pytest.raises(PlatformInventoryError, match="plataforma windows"):
        validate_windows_inventory(wrong_platform)

    unsupported_architecture = load("windows-inventory.json")
    unsupported_architecture["architecture"] = "sparc"
    with pytest.raises(PlatformInventoryError, match="arquitectura"):
        validate_windows_inventory(unsupported_architecture)

    empty_stable_id = load("windows-inventory.json")
    empty_stable_id["disks"][0]["stable_id"] = ""  # type: ignore[index]
    with pytest.raises(PlatformInventoryError, match="identidad"):
        validate_windows_inventory(empty_stable_id)


def test_platform_contract_enforces_windows_requirements_and_records_warning():
    missing_firmware = load("windows-inventory.json")
    missing_firmware["firmware"] = None
    with pytest.raises(PlatformInventoryError, match="firmware"):
        validate_windows_inventory(missing_firmware)

    missing_version = load("windows-inventory.json")
    missing_version["os"]["version"] = ""  # type: ignore[index]
    with pytest.raises(PlatformInventoryError, match="versión"):
        validate_windows_inventory(missing_version)

    missing_serial = load("windows-inventory.json")
    missing_serial["system"]["serial_number"] = ""  # type: ignore[index]
    inventory = validate_windows_inventory(missing_serial)
    assert "system_serial_unavailable" in inventory.warnings


def test_macos_evaluation_requires_version():
    missing_version = load("macos-inventory.json")
    missing_version["os"]["version"] = ""  # type: ignore[index]
    with pytest.raises(PlatformInventoryError, match=r"macOS.*versión"):
        validate_macos_evaluation_inventory(missing_version)
