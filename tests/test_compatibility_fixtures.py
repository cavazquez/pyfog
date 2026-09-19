import copy
import hashlib
import json
import shutil
import subprocess
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


def test_load_matrix_rejects_unreadable_and_invalid_documents(tmp_path: Path) -> None:
    with pytest.raises(compatibility_fixtures.FixtureError, match="No se pudo leer"):
        compatibility_fixtures.load_matrix(tmp_path / "missing.json")

    invalid_json = tmp_path / "invalid.json"
    invalid_json.write_text("{", encoding="utf-8")
    with pytest.raises(compatibility_fixtures.FixtureError, match="No se pudo leer"):
        compatibility_fixtures.load_matrix(invalid_json)

    cases = [
        ([], "objeto JSON"),
        ({"schema_version": 2}, "schema_version 1"),
        ({"schema_version": 1, "generator": None}, "opciones deterministas"),
        (
            {
                "schema_version": 1,
                "generator": {
                    "format": compatibility_fixtures.QCOW2_FORMAT,
                    "compat": compatibility_fixtures.QCOW2_COMPAT,
                    "cluster_size": compatibility_fixtures.QCOW2_CLUSTER_SIZE,
                    "lazy_refcounts": True,
                },
            },
            "opciones QCOW2",
        ),
        (
            {
                "schema_version": 1,
                "generator": {
                    "format": compatibility_fixtures.QCOW2_FORMAT,
                    "compat": compatibility_fixtures.QCOW2_COMPAT,
                    "cluster_size": compatibility_fixtures.QCOW2_CLUSTER_SIZE,
                    "lazy_refcounts": False,
                },
                "fixtures": [],
            },
            "al menos un fixture",
        ),
    ]
    for payload, expected in cases:
        path = tmp_path / f"{len(str(payload))}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(compatibility_fixtures.FixtureError) as raised:
            compatibility_fixtures.load_matrix(path)
        assert expected in str(raised.value)


def test_load_matrix_rejects_duplicate_fixture(tmp_path: Path, matrix: dict[str, object]) -> None:
    payload = copy.deepcopy(matrix)
    fixtures = payload["fixtures"]
    assert isinstance(fixtures, list)
    fixtures.append(copy.deepcopy(fixtures[0]))
    path = tmp_path / "duplicates.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(compatibility_fixtures.FixtureError, match="repetidos"):
        compatibility_fixtures.load_matrix(path)


def test_validate_spec_rejects_invalid_definitions(matrix: dict[str, object]) -> None:
    valid = copy.deepcopy(fixture_specs(matrix)[0])
    assert isinstance(valid, dict)
    cases: list[tuple[object, str]] = [(None, "objeto JSON")]

    missing = copy.deepcopy(valid)
    missing.pop("id")
    cases.append((missing, "campos requeridos"))

    invalid_id = copy.deepcopy(valid)
    invalid_id["id"] = "INVALID"
    cases.append((invalid_id, "ID o archivo"))

    invalid_size = copy.deepcopy(valid)
    invalid_size["virtual_size_bytes"] = 1
    cases.append((invalid_size, "tamaño virtual"))

    invalid_hash = copy.deepcopy(valid)
    invalid_hash["sha256_parts"] = []
    cases.append((invalid_hash, "SHA-256"))

    invalid_status = copy.deepcopy(valid)
    invalid_status["status"] = "unknown"
    cases.append((invalid_status, "estado"))

    for field in ("checks", "restrictions"):
        invalid_list = copy.deepcopy(valid)
        invalid_list[field] = []
        cases.append((invalid_list, f"{field} de"))

    rejected_without_errors = copy.deepcopy(valid)
    rejected_without_errors["status"] = "rejected"
    cases.append((rejected_without_errors, "errores esperados"))

    invalid_capabilities = copy.deepcopy(valid)
    invalid_capabilities["capabilities"] = None
    cases.append((invalid_capabilities, "no declara capacidades"))

    incomplete_capabilities = copy.deepcopy(valid)
    capabilities = incomplete_capabilities["capabilities"]
    assert isinstance(capabilities, dict)
    capabilities.pop("volumes")
    cases.append((incomplete_capabilities, "están incompletas"))

    invalid_disks = copy.deepcopy(valid)
    capabilities = invalid_disks["capabilities"]
    assert isinstance(capabilities, dict)
    capabilities["disks"] = 0
    cases.append((invalid_disks, "cantidad de discos"))

    invalid_filesystems = copy.deepcopy(valid)
    capabilities = invalid_filesystems["capabilities"]
    assert isinstance(capabilities, dict)
    capabilities["filesystems"] = []
    cases.append((invalid_filesystems, "filesystems"))

    for candidate, expected in cases:
        with pytest.raises(compatibility_fixtures.FixtureError) as raised:
            compatibility_fixtures.validate_spec(candidate)
        assert expected in str(raised.value)


def test_fixture_specs_rejects_non_list_payload() -> None:
    with pytest.raises(compatibility_fixtures.FixtureError, match="lista de fixtures"):
        compatibility_fixtures.fixture_specs({"fixtures": None})


def test_qemu_errors_are_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(compatibility_fixtures.shutil, "which", lambda _: None)
    with pytest.raises(compatibility_fixtures.FixtureError, match="Falta qemu-img"):
        compatibility_fixtures._qemu_img()

    monkeypatch.setattr(compatibility_fixtures, "_qemu_img", lambda: "qemu-img")

    def missing_qemu(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(compatibility_fixtures.subprocess, "run", missing_qemu)
    with pytest.raises(compatibility_fixtures.FixtureError, match="Falta qemu-img"):
        compatibility_fixtures._run_qemu(["info"])

    def failed_qemu(*args: object, **kwargs: object) -> None:
        raise subprocess.CalledProcessError(1, "qemu-img", stderr="falló")

    monkeypatch.setattr(compatibility_fixtures.subprocess, "run", failed_qemu)
    with pytest.raises(compatibility_fixtures.FixtureError, match="qemu-img falló"):
        compatibility_fixtures._run_qemu(["info"])


def test_fixture_generation_and_hash_validation_errors(
    tmp_path: Path, matrix: dict[str, object]
) -> None:
    fixture = copy.deepcopy(fixture_specs(matrix)[0])
    assert isinstance(fixture, dict)
    output = tmp_path / "output"
    output.mkdir()
    (output / str(fixture["file"])).write_bytes(b"existing")
    with pytest.raises(compatibility_fixtures.FixtureError, match="No se sobrescribe"):
        compatibility_fixtures.generate_fixture(fixture, output)

    invalid_hash = copy.deepcopy(fixture)
    invalid_hash["sha256_parts"] = [1]
    with pytest.raises(compatibility_fixtures.FixtureError, match="partes SHA-256"):
        compatibility_fixtures.expected_sha256(invalid_hash)


def test_verify_fixture_reports_invalid_outputs(
    tmp_path: Path, matrix: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = copy.deepcopy(fixture_specs(matrix)[0])
    assert isinstance(fixture, dict)
    missing = tmp_path / "missing.qcow2"
    with pytest.raises(compatibility_fixtures.FixtureError, match="archivo regular"):
        compatibility_fixtures.verify_fixture(fixture, missing)

    path = tmp_path / "fixture.qcow2"
    path.write_bytes(b"fixture")
    monkeypatch.setattr(compatibility_fixtures, "_run_qemu", lambda _: "not-json")
    with pytest.raises(compatibility_fixtures.FixtureError, match="no devolvió JSON"):
        compatibility_fixtures.verify_fixture(fixture, path)

    monkeypatch.setattr(compatibility_fixtures, "_run_qemu", lambda _: "{}")
    with pytest.raises(compatibility_fixtures.FixtureError, match="formato o tamaño"):
        compatibility_fixtures.verify_fixture(fixture, path)

    base_info = {
        "format": compatibility_fixtures.QCOW2_FORMAT,
        "virtual-size": fixture["virtual_size_bytes"],
        "cluster-size": compatibility_fixtures.QCOW2_CLUSTER_SIZE,
    }
    monkeypatch.setattr(
        compatibility_fixtures,
        "_run_qemu",
        lambda _: json.dumps({**base_info, "format-specific": {"data": {}}}),
    )
    with pytest.raises(compatibility_fixtures.FixtureError, match="no son deterministas"):
        compatibility_fixtures.verify_fixture(fixture, path)

    good_info = {
        **base_info,
        "format-specific": {
            "data": {
                "compat": compatibility_fixtures.QCOW2_COMPAT,
                "lazy-refcounts": False,
            }
        },
    }
    monkeypatch.setattr(compatibility_fixtures, "_run_qemu", lambda _: json.dumps(good_info))
    monkeypatch.setattr(compatibility_fixtures, "sha256_file", lambda _: "bad")
    with pytest.raises(compatibility_fixtures.FixtureError, match="SHA-256"):
        compatibility_fixtures.verify_fixture(fixture, path)
