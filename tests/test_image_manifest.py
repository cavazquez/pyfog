import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pyfog.image_manifest import (
    ImageManifest,
    ensure_supported_extended_image,
    ensure_supported_image,
    image_compatibility_errors,
    parse_image_manifest,
    upgrade_manifest,
    validate_image_manifest,
    verify_image_artifacts,
)


def valid_manifest() -> dict[str, object]:
    return {
        "format": "pyfog-disk-image",
        "format_version": 1,
        "image_id": "a5d5eb5c-1c44-4b02-bb1d-9c0ddf0a2b11",
        "created_at": "2024-01-01T10:00:00+00:00",
        "checksum_algorithm": "sha256",
        "source": {
            "host_id": "ca66ef2d-4fd4-44ab-9b7a-f9e59f8af111",
            "inventory_report_id": "d1a9aa22-5709-48d6-9fef-0bc4c5a2c222",
            "hostname": "reference-linux",
        },
        "system": {"name": "Ubuntu Server 24.04 LTS", "id": "ubuntu", "version": "24.04"},
        "architecture": "x86_64",
        "firmware": {"type": "uefi", "secure_boot": False},
        "disk": {
            "size_bytes": 1_073_741_824,
            "logical_sector_bytes": 512,
            "sector_count": 2_097_152,
            "gpt_disk_guid": "7c1c8a08-13b3-4505-8e1c-9b0d2da93333",
            "first_usable_sector": 2_048,
            "last_usable_sector": 2_095_103,
            "partitions": [
                {
                    "number": 1,
                    "role": "esp",
                    "start_sector": 2_048,
                    "size_sectors": 204_800,
                    "partition_guid": "a8f7b8d4-ccdf-4d9d-8d2d-6da2fb6f4444",
                    "filesystem": "fat32",
                    "filesystem_uuid": "A1B2C3D4",
                    "mountpoint": "/boot/efi",
                    "artifact": "partitions/01-esp.img",
                },
                {
                    "number": 2,
                    "role": "root",
                    "start_sector": 206_848,
                    "size_sectors": 1_888_256,
                    "partition_guid": "bbd0a02f-8c3d-4384-8f3c-3b109b5d5555",
                    "filesystem": "ext4",
                    "filesystem_uuid": "bbd0a02f-8c3d-4384-8f3c-3b109b5d5555",
                    "mountpoint": "/",
                    "artifact": "partitions/02-root.partclone",
                },
            ],
        },
        "tool": {
            "name": "partclone",
            "version": "0.3.45",
            "commands": ["partclone.ext4", "partclone.fat"],
        },
        "artifacts": [
            {
                "path": "partitions/01-esp.img",
                "size_bytes": 3,
                "compression": "none",
                "sha256": hashlib.sha256(b"esp").hexdigest(),
            },
            {
                "path": "partitions/02-root.partclone",
                "size_bytes": 4,
                "compression": "zstd",
                "sha256": hashlib.sha256(b"root").hexdigest(),
            },
        ],
        "publishable": True,
    }


def valid_manifest_v2() -> dict[str, object]:
    payload = valid_manifest()
    payload["format_version"] = 2
    payload["capabilities"] = {
        "firmware": {"type": "uefi", "secure_boot": False},
        "partition_table": "gpt",
        "disks": 1,
        "filesystems": ["fat32", "ext4"],
        "encryption": "none",
        "volumes": "partitions",
    }
    return payload


def valid_bios_manifest() -> dict[str, object]:
    payload = copy.deepcopy(valid_manifest_v2())
    firmware = {"type": "bios", "secure_boot": False}
    payload["firmware"] = firmware
    payload["capabilities"] = {
        "firmware": firmware,
        "partition_table": "mbr",
        "disks": 1,
        "filesystems": ["ext4"],
        "encryption": "none",
        "volumes": "partitions",
    }
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
    for field in ("gpt_disk_guid", "first_usable_sector", "last_usable_sector"):
        disk.pop(field)
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
    return payload


def test_valid_manifest_preserves_boot_layout_and_artifact_metadata():
    manifest = ImageManifest.model_validate(valid_manifest())
    assert manifest.disk.logical_sector_bytes == 512
    assert [part.role for part in manifest.disk.partitions] == ["esp", "root"]
    assert manifest.disk.partitions[0].filesystem_uuid == "A1B2C3D4"
    assert manifest.artifacts[1].compression == "zstd"


def test_valid_bios_mbr_manifest_preserves_boot_sector_and_is_supported():
    manifest = ImageManifest.model_validate(valid_bios_manifest())
    assert manifest.firmware.type == "bios"
    assert manifest.disk.mbr_disk_signature == "1a2b3c4d"
    assert manifest.disk.boot_sector is not None
    ensure_supported_image(manifest)


@pytest.mark.parametrize(
    "path",
    ["../outside", "/absolute.img", "partitions/../root.img", "partitions//root.img"],
)
def test_manifest_rejects_unsafe_artifact_paths(path):
    payload = valid_manifest()
    payload["artifacts"] = [*payload["artifacts"]]  # type: ignore[index]
    payload["artifacts"][0]["path"] = path  # type: ignore[index]
    with pytest.raises(ValueError, match="ruta"):
        ImageManifest.model_validate(payload)


@pytest.mark.parametrize("version", [3, "1", True])
def test_manifest_rejects_unknown_format_versions(version):
    payload = valid_manifest()
    payload["format_version"] = version
    with pytest.raises(ValueError, match="format_version"):
        ImageManifest.model_validate(payload)


def test_v2_declares_capabilities_and_v1_has_a_lossless_adapter():
    manifest = ImageManifest.model_validate(valid_manifest_v2())
    assert manifest.capabilities is not None
    assert manifest.capabilities.partition_table == "gpt"
    ensure_supported_image(manifest)

    upgraded = upgrade_manifest(valid_manifest())
    assert upgraded.format_version == 2
    assert upgraded.capabilities is not None
    assert upgraded.capabilities.model_dump(mode="json") == manifest.capabilities.model_dump(
        mode="json"
    )


def test_v2_requires_capabilities_and_catalog_rejects_unsupported_profile():
    missing = valid_manifest()
    missing["format_version"] = 2
    with pytest.raises(ValueError, match="capacidades explícitas"):
        ImageManifest.model_validate(missing)

    unsupported = valid_manifest_v2()
    unsupported["capabilities"] = {
        **unsupported["capabilities"],  # type: ignore[index]
        "encryption": "luks2",
    }
    manifest = ImageManifest.model_validate(unsupported)
    errors = image_compatibility_errors(manifest)
    assert any("cifrado" in error for error in errors)
    with pytest.raises(ValueError, match="cifrado"):
        ensure_supported_image(manifest)


def test_manifest_rejects_geometry_and_overlapping_partitions():
    payload = valid_manifest()
    payload["disk"] = copy.deepcopy(payload["disk"])
    payload["disk"]["size_bytes"] = 512  # type: ignore[index]
    with pytest.raises(ValueError, match="capacidad"):
        ImageManifest.model_validate(payload)

    overlapping = valid_manifest()
    overlapping["disk"] = copy.deepcopy(overlapping["disk"])
    overlapping["disk"]["partitions"] = [*overlapping["disk"]["partitions"]]  # type: ignore[index]
    overlapping["disk"]["partitions"][1]["start_sector"] = 100_000  # type: ignore[index]
    with pytest.raises(ValueError, match="superponen"):
        ImageManifest.model_validate(overlapping)


def test_swap_is_explicit_but_has_no_data_artifact():
    payload = valid_manifest()
    payload["disk"] = copy.deepcopy(payload["disk"])
    payload["disk"]["partitions"] = [*payload["disk"]["partitions"]]  # type: ignore[index]
    root = payload["disk"]["partitions"][1]  # type: ignore[index]
    root["size_sectors"] = 1_693_153
    payload["disk"]["partitions"].append(  # type: ignore[index]
        {
            "number": 3,
            "role": "swap",
            "start_sector": 1_900_001,
            "size_sectors": 195_103,
            "partition_guid": "c13eb601-8c76-4f0c-86be-6da2fb6f6666",
            "filesystem": "swap",
            "filesystem_uuid": "c13eb601-8c76-4f0c-86be-6da2fb6f6666",
            "mountpoint": None,
            "artifact": None,
        }
    )
    manifest = ImageManifest.model_validate(payload)
    assert manifest.disk.partitions[-1].role == "swap"
    assert manifest.disk.partitions[-1].artifact is None


def test_artifact_files_are_verified_and_tampering_is_rejected(tmp_path: Path):
    root = tmp_path / "artifacts"
    partition_dir = root / "partitions"
    partition_dir.mkdir(parents=True)
    (partition_dir / "01-esp.img").write_bytes(b"esp")
    (partition_dir / "02-root.partclone").write_bytes(b"root")
    manifest = parse_image_manifest(json.dumps(valid_manifest()))
    verify_image_artifacts(manifest, root)
    (partition_dir / "02-root.partclone").write_bytes(b"ROOT")
    with pytest.raises(ValueError, match="SHA-256"):
        verify_image_artifacts(manifest, root)


def test_validator_rejects_missing_artifact_and_truncated_json(tmp_path: Path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(valid_manifest()), encoding="utf-8")
    with pytest.raises(ValueError, match="Falta el artefacto"):
        validate_image_manifest(manifest_path, tmp_path)
    with pytest.raises(ValueError, match="JSON válido"):
        parse_image_manifest(b'{"format":')


def test_manifest_rejects_future_dates_and_non_ubuntu_system():
    future = valid_manifest()
    future["created_at"] = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    with pytest.raises(ValueError, match="futuro"):
        ImageManifest.model_validate(future)
    other = valid_manifest()
    other["system"] = {"name": "Fedora", "id": "fedora", "version": "41"}
    with pytest.raises(ValueError, match="Ubuntu"):
        ImageManifest.model_validate(other)


def test_v2_multidisk_manifest_matches_stable_identities_before_restore():
    payload = valid_manifest_v2()
    first = copy.deepcopy(payload["disk"])
    first["disk_id"] = "wwn:disk-a"
    for partition in first["partitions"]:
        if partition.get("artifact"):
            partition["artifact"] = partition["artifact"].replace(
                "partitions/", "partitions/disk-a-"
            )
    second = copy.deepcopy(payload["disk"])
    second["disk_id"] = "wwn:disk-b"
    for partition in second["partitions"]:
        if partition.get("artifact"):
            partition["artifact"] = partition["artifact"].replace(
                "partitions/", "partitions/disk-b-"
            )
    payload["disk"] = first
    payload["disks"] = [first, second]
    payload["capabilities"] = {
        **payload["capabilities"],
        "disks": 2,
    }
    artifacts = copy.deepcopy(payload["artifacts"])
    for artifact in artifacts:
        artifact["path"] = artifact["path"].replace("partitions/", "partitions/disk-a-")
    payload["artifacts"] = artifacts + [
        {
            **artifact,
            "path": artifact["path"].replace("disk-a-", "disk-b-"),
        }
        for artifact in artifacts
    ]
    manifest = ImageManifest.model_validate(payload)
    assert len(manifest.disks or []) == 2
    ensure_supported_extended_image(manifest)


def test_multidisk_and_raid_manifests_reject_path_order_identities():
    payload = valid_manifest_v2()
    first = copy.deepcopy(payload["disk"])
    second = copy.deepcopy(payload["disk"])
    first["disk_id"] = "path:/dev/vda"
    second["disk_id"] = "wwn:disk-b"
    payload["disk"] = first
    payload["disks"] = [first, second]
    payload["capabilities"] = {**payload["capabilities"], "disks": 2}
    with pytest.raises(ValueError, match="orden /dev"):
        ImageManifest.model_validate(payload)

    raid = {
        "level": 1,
        "uuid": "11111111-1111-4111-8111-111111111111",
        "metadata": "1.2",
        "member_ids": ["path:/dev/vda", "wwn:disk-b"],
        "size_bytes": 1024,
    }
    payload = valid_manifest_v2()
    payload["raid_arrays"] = [raid]
    with pytest.raises(ValueError, match="identidades estables"):
        ImageManifest.model_validate(payload)
