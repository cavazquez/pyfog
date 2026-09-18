import copy
import hashlib
import shutil
from pathlib import Path

import pytest

from pyfog.image_manifest import (
    ImageCapabilities,
    ImageManifest,
    ensure_supported_image,
    image_compatibility_errors,
)
from scripts import compatibility_fixtures
from scripts import run_image_task as task_agent
from tests.test_image_manifest import valid_manifest_v2


@pytest.fixture(scope="module")
def matrix() -> dict[str, object]:
    return compatibility_fixtures.load_matrix()


def fixture_specs(matrix: dict[str, object]) -> list[dict[str, object]]:
    fixtures = matrix["fixtures"]
    assert isinstance(fixtures, list)
    return [fixture for fixture in fixtures if isinstance(fixture, dict)]


def fixture_manifest(fixture: dict[str, object]) -> ImageManifest:
    payload = valid_manifest_v2()
    capabilities = copy.deepcopy(fixture["capabilities"])
    assert isinstance(capabilities, dict)
    payload["firmware"] = copy.deepcopy(capabilities["firmware"])
    payload["capabilities"] = capabilities
    if capabilities["partition_table"] == "mbr":
        disk = payload["disk"]
        assert isinstance(disk, dict)
        root = copy.deepcopy(disk["partitions"][1])
        root.update(
            {
                "number": 1,
                "start_sector": 2_048,
                "size_sectors": disk["sector_count"] - 2_048,
                "partition_guid": None,
                "artifact": "partitions/01-root.partclone",
            }
        )
        disk.pop("gpt_disk_guid")
        disk.pop("first_usable_sector")
        disk.pop("last_usable_sector")
        disk["mbr_disk_signature"] = "1a2b3c4d"
        disk["partitions"] = [root]
        disk["boot_sector"] = {
            "path": "boot-sector.bin",
            "size_bytes": 446,
            "compression": "none",
            "sha256": hashlib.sha256(b"b" * 446).hexdigest(),
        }
        payload["tool"] = {
            "name": "partclone",
            "version": "0.3.45",
            "commands": ["mbr", "partclone.ext4"],
        }
        payload["artifacts"] = [
            {
                "path": "partitions/01-root.partclone",
                "size_bytes": 4,
                "compression": "zstd",
                "sha256": hashlib.sha256(b"root").hexdigest(),
            },
            disk["boot_sector"],
        ]
    return ImageManifest.model_validate(payload)


def test_matrix_has_one_supported_profile_and_explicit_restrictions(
    matrix: dict[str, object],
) -> None:
    specs = fixture_specs(matrix)
    assert {fixture["id"] for fixture in specs} == {
        "uefi-ext4",
        "bios-mbr",
        "uefi-secure-boot",
        "xfs",
        "lvm",
        "luks2",
        "raid1",
        "multiple-disks",
    }
    supported = [fixture for fixture in specs if fixture["status"] == "supported"]
    assert [fixture["id"] for fixture in supported] == ["uefi-ext4", "bios-mbr"]
    for fixture in specs:
        ImageCapabilities.model_validate(fixture["capabilities"])
        assert fixture["checks"]
        assert fixture["restrictions"]
        if fixture["status"] == "rejected":
            assert fixture["expected_errors"]
            assert "reject" in fixture["checks"]
            assert not {"capture", "restore", "boot"}.intersection(fixture["checks"])


@pytest.mark.parametrize(
    "fixture_id",
    [
        "uefi-ext4",
        "bios-mbr",
        "uefi-secure-boot",
        "xfs",
        "lvm",
        "luks2",
        "raid1",
        "multiple-disks",
    ],
)
def test_matrix_profiles_are_rejected_or_supported_before_restore_target(
    matrix: dict[str, object], fixture_id: str
) -> None:
    fixture = next(item for item in fixture_specs(matrix) if item["id"] == fixture_id)
    manifest = fixture_manifest(fixture)
    if fixture["status"] == "supported":
        ensure_supported_image(manifest)
        assert {"capture", "restore", "boot"}.issubset(fixture["checks"])
        assert task_agent.validate_restore_manifest(
            manifest.model_dump(mode="json"), str(manifest.image_id)
        )
        return

    errors = image_compatibility_errors(manifest)
    assert all(
        any(expected.lower() in error.lower() for error in errors)
        for expected in fixture["expected_errors"]
    )
    with pytest.raises(ValueError, match=r"(La|El)"):
        ensure_supported_image(manifest)
    with pytest.raises(ValueError, match=r"(La|El)"):
        task_agent.validate_restore_manifest(
            manifest.model_dump(mode="json"), str(manifest.image_id)
        )


def test_qcow2_fixtures_are_reproducible_and_verified(tmp_path: Path) -> None:
    if shutil.which("qemu-img") is None:
        pytest.skip("qemu-img no está instalado; el job de fixtures instala qemu-utils")
    matrix = compatibility_fixtures.load_matrix()
    first = compatibility_fixtures.generate_and_verify(tmp_path / "first", matrix)
    second = compatibility_fixtures.generate_and_verify(tmp_path / "second", matrix)
    assert [path.name for path in first] == [path.name for path in second]
    for first_path, second_path in zip(first, second, strict=True):
        assert first_path.read_bytes() == second_path.read_bytes()
