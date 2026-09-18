import pytest

from pyfog.disk_mapping import DiskMappingError, plan_multi_disk_restore, stable_disk_id


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
