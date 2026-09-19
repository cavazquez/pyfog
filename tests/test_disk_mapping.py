import pytest

from pyfog.disk_mapping import (
    DiskMappingError,
    plan_multi_disk_restore,
    require_non_path_identity,
    stable_disk_id,
)


def disk(identity: str, size: int, path: str) -> dict[str, object]:
    kind, value = identity.split(":", 1)
    return {
        kind: value,
        "size": size,
        "log-sec": 512,
        "path": path,
        "rm": False,
    }


def test_multi_disk_plan_is_independent_of_detection_order():
    source = [disk("wwn:source-a", 100, "/dev/vda"), disk("wwn:source-b", 200, "/dev/vdb")]
    target = [disk("wwn:source-b", 250, "/dev/nvme1n1"), disk("wwn:source-a", 100, "/dev/nvme0n1")]

    plans = plan_multi_disk_restore(source, target)

    assert [(plan.source_id, plan.target_path) for plan in plans] == [
        ("wwn:source-a", "/dev/nvme0n1"),
        ("wwn:source-b", "/dev/nvme1n1"),
    ]


def test_multi_disk_plan_rejects_missing_or_ambiguous_identity():
    source = [disk("serial:source-a", 100, "/dev/vda"), disk("serial:source-b", 200, "/dev/vdb")]
    with pytest.raises(DiskMappingError, match="No se encontró"):
        plan_multi_disk_restore(
            source,
            [disk("serial:source-a", 100, "/dev/sda"), disk("serial:other", 200, "/dev/sdb")],
        )

    with pytest.raises(DiskMappingError, match="orden /dev"):
        stable_disk_id({"name": "/dev/vda"})

    with pytest.raises(DiskMappingError, match="identidad"):
        plan_multi_disk_restore(
            [{"name": "/dev/vda", "size": 100, "log-sec": 512}],
            [{"name": "/dev/sda", "size": 100, "log-sec": 512}],
        )


def test_stable_disk_id_prefers_explicit_and_supports_legacy_fields():
    assert stable_disk_id({"stable_id": "wwn:explicit", "wwn": "fallback"}) == "wwn:explicit"
    assert stable_disk_id({"wwn": " 0x123 "}) == "wwn:0x123"
    assert stable_disk_id({"serial": "serial-a"}) == "serial:serial-a"
    assert stable_disk_id({"serial_number": "serial-b"}) == "serial:serial-b"
    with pytest.raises(DiskMappingError, match="identidad estable"):
        stable_disk_id({"path": "/dev/vda"})
    with pytest.raises(DiskMappingError, match="no puede depender"):
        require_non_path_identity({"stable_id": "path:/dev/vda"})


def test_multi_disk_plan_rejects_inventory_and_capacity_errors():
    source = [disk("wwn:source-a", 100, "/dev/vda")]
    target = [disk("wwn:source-a", 100, "/dev/sda")]

    with pytest.raises(DiskMappingError, match="cantidad"):
        plan_multi_disk_restore([], [])
    with pytest.raises(DiskMappingError, match="cantidad"):
        plan_multi_disk_restore(source, [])

    duplicate_source = [disk("wwn:source-a", 100, "/dev/vda")] * 2
    with pytest.raises(DiskMappingError, match="repetidas"):
        plan_multi_disk_restore(duplicate_source, [*target, disk("wwn:source-a", 100, "/dev/sdb")])

    duplicate_target = [disk("wwn:source-a", 100, "/dev/sda")] * 2
    with pytest.raises(DiskMappingError, match="repetidas"):
        plan_multi_disk_restore(
            [disk("wwn:source-a", 100, "/dev/vda"), disk("wwn:source-b", 100, "/dev/vdb")],
            duplicate_target,
        )

    small_target = [disk("wwn:source-a", 99, "/dev/sda")]
    with pytest.raises(DiskMappingError, match="capacidad"):
        plan_multi_disk_restore(source, small_target)
    larger_target = [disk("wwn:source-a", 101, "/dev/sda")]
    with pytest.raises(DiskMappingError, match="capacidad"):
        plan_multi_disk_restore(source, larger_target, allow_larger_targets=False)


def test_multi_disk_plan_rejects_target_safety_mismatches():
    source = [disk("wwn:source-a", 100, "/dev/vda")]

    removable = [disk("wwn:source-a", 100, "/dev/sda")]
    removable[0]["rm"] = "true"
    with pytest.raises(DiskMappingError, match="removible"):
        plan_multi_disk_restore(source, removable)

    wrong_sector = [disk("wwn:source-a", 100, "/dev/sda")]
    wrong_sector[0]["log-sec"] = 4096
    with pytest.raises(DiskMappingError, match="sector"):
        plan_multi_disk_restore(source, wrong_sector)

    invalid_path = [disk("wwn:source-a", 100, "sda")]
    with pytest.raises(DiskMappingError, match="ruta"):
        plan_multi_disk_restore(source, invalid_path)

    invalid_size = [disk("wwn:source-a", 0, "/dev/sda")]
    with pytest.raises(DiskMappingError, match="tamaño"):
        plan_multi_disk_restore(source, invalid_size)

    invalid_sector = [disk("wwn:source-a", 100, "/dev/sda")]
    invalid_sector[0]["log-sec"] = 1024
    with pytest.raises(DiskMappingError, match="sector"):
        plan_multi_disk_restore(source, invalid_sector)
