import copy

import pytest

from pyfog.reducer import ReductionError, build_reduction_plan, reduction_commands
from tests.test_image_manifest import valid_manifest_v2


def test_reduction_plan_is_ext4_only_and_orders_checks_before_publish(tmp_path):
    source = tmp_path / "source.raw"
    source.write_bytes(b"source")
    manifest = valid_manifest_v2()
    from pyfog.image_manifest import ImageManifest

    parsed = ImageManifest.model_validate(manifest)
    plan = build_reduction_plan(
        parsed,
        source,
        tmp_path / "small.raw",
        900 * 1024 * 1024,
        minimum_filesystem_bytes=100 * 1024 * 1024,
    )
    commands = reduction_commands(plan, tmp_path / "working.raw")
    assert commands[0][0] == "e2fsck"
    assert commands[1][0] == "resize2fs"
    assert commands[-1][:2] == ["sgdisk", "--verify"]


def test_reduction_rejects_non_ext4_and_overwrite(tmp_path):
    source = tmp_path / "source.raw"
    source.write_bytes(b"source")
    destination = tmp_path / "small.raw"
    destination.write_bytes(b"existing")
    from pyfog.image_manifest import ImageManifest

    parsed = ImageManifest.model_validate(valid_manifest_v2())
    with pytest.raises(ReductionError, match="no se sobrescribe"):
        build_reduction_plan(
            parsed,
            source,
            destination,
            900 * 1024 * 1024,
            minimum_filesystem_bytes=100,
        )

    unsupported = copy.deepcopy(valid_manifest_v2())
    unsupported["capabilities"]["volumes"] = "lvm"  # type: ignore[index]
    parsed_unsupported = ImageManifest.model_validate(unsupported)
    destination.unlink()
    with pytest.raises(ReductionError, match="cifrado, LVM"):
        build_reduction_plan(
            parsed_unsupported,
            source,
            destination,
            900 * 1024 * 1024,
            minimum_filesystem_bytes=100,
        )
