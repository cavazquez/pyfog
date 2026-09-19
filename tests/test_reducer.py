import copy

import pytest

import pyfog.reducer as reducer
from pyfog.image_manifest import ImageManifest
from pyfog.reducer import (
    ReductionError,
    build_reduction_plan,
    reduce_ext4_image,
    reduction_commands,
)
from tests.test_image_manifest import valid_manifest_v2


def test_reduction_plan_is_ext4_only_and_orders_checks_before_publish(tmp_path):
    source = tmp_path / "source.raw"
    source.write_bytes(b"source")
    manifest = valid_manifest_v2()
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


@pytest.mark.parametrize(
    ("target_size_bytes", "minimum_filesystem_bytes", "message"),
    [
        (1_073_741_824, 100, "estrictamente menor"),
        (1_073_741_823, 100, "múltiplo"),
        (2_048 * 512, 100, "geometría GPT"),
        (206_848 * 512, 100, "espacio para la raíz"),
        (900 * 1024 * 1024, 900 * 1024 * 1024, "filesystem ext4"),
    ],
)
def test_reduction_plan_rejects_invalid_target_geometry(
    tmp_path, target_size_bytes, minimum_filesystem_bytes, message
):
    source = tmp_path / "source.raw"
    source.write_bytes(b"source")
    parsed = ImageManifest.model_validate(valid_manifest_v2())

    with pytest.raises(ReductionError, match=message):
        build_reduction_plan(
            parsed,
            source,
            tmp_path / "small.raw",
            target_size_bytes,
            minimum_filesystem_bytes=minimum_filesystem_bytes,
        )


def test_reduction_plan_rejects_source_and_manifest_geometry_errors(tmp_path):
    source = tmp_path / "source.raw"
    source.write_bytes(b"source")
    parsed = ImageManifest.model_validate(valid_manifest_v2())

    with pytest.raises(ReductionError, match="mismo archivo"):
        build_reduction_plan(
            parsed,
            source,
            source,
            900 * 1024 * 1024,
            minimum_filesystem_bytes=100,
        )

    link = tmp_path / "source-link.raw"
    link.symlink_to(source)
    with pytest.raises(ReductionError, match="archivo regular"):
        build_reduction_plan(
            parsed,
            link,
            tmp_path / "small.raw",
            900 * 1024 * 1024,
            minimum_filesystem_bytes=100,
        )

    parsed_without_geometry = ImageManifest.model_validate(valid_manifest_v2())
    parsed_without_geometry.disk.first_usable_sector = None
    with pytest.raises(ReductionError, match="geometría GPT"):
        build_reduction_plan(
            parsed_without_geometry,
            source,
            tmp_path / "small.raw",
            900 * 1024 * 1024,
            minimum_filesystem_bytes=100,
        )


def test_reduction_plan_rejects_unsupported_partition_profiles(tmp_path):
    source = tmp_path / "source.raw"
    source.write_bytes(b"source")

    multidisk = ImageManifest.model_validate(valid_manifest_v2())
    multidisk.capabilities.disks = 2  # type: ignore[union-attr]
    with pytest.raises(ReductionError, match="un disco GPT"):
        build_reduction_plan(
            multidisk,
            source,
            tmp_path / "multidisk.raw",
            900 * 1024 * 1024,
            minimum_filesystem_bytes=100,
        )

    non_ext4 = ImageManifest.model_validate(valid_manifest_v2())
    non_ext4.disk.partitions[1].filesystem = "xfs"
    with pytest.raises(ReductionError, match="raíz ext4"):
        build_reduction_plan(
            non_ext4,
            source,
            tmp_path / "xfs.raw",
            900 * 1024 * 1024,
            minimum_filesystem_bytes=100,
        )

    with_swap = ImageManifest.model_validate(valid_manifest_v2())
    with_swap.disk.partitions[0].role = "swap"
    with pytest.raises(ReductionError, match="partición swap"):
        build_reduction_plan(
            with_swap,
            source,
            tmp_path / "swap.raw",
            900 * 1024 * 1024,
            minimum_filesystem_bytes=100,
        )


def test_reduce_ext4_image_publishes_after_runner_and_cleans_temporary_file(tmp_path):
    source = tmp_path / "source.raw"
    destination = tmp_path / "small.raw"
    source.write_bytes(b"source")
    plan = build_reduction_plan(
        ImageManifest.model_validate(valid_manifest_v2()),
        source,
        destination,
        900 * 1024 * 1024,
        minimum_filesystem_bytes=100,
    )
    commands = []

    result = reduce_ext4_image(plan, runner=commands.append)

    assert result == destination
    with destination.open("rb") as published:
        assert published.read(len(b"source")) == b"source"
    assert [command[:2] for command in commands[:2]] == [["e2fsck", "-f"], ["resize2fs", "-M"]]
    assert commands[-1][:2] == ["sgdisk", "--verify"]
    assert not list(tmp_path.glob(f".{destination.name}.*.part"))


def test_reduce_ext4_image_wraps_runner_errors_and_cleans_temporary_file(tmp_path):
    source = tmp_path / "source.raw"
    destination = tmp_path / "small.raw"
    source.write_bytes(b"source")
    plan = build_reduction_plan(
        ImageManifest.model_validate(valid_manifest_v2()),
        source,
        destination,
        900 * 1024 * 1024,
        minimum_filesystem_bytes=100,
    )

    def fail(_command):
        raise ValueError("simulated tool failure")

    with pytest.raises(ReductionError, match="no se publicó"):
        reduce_ext4_image(plan, runner=fail)

    assert not destination.exists()
    assert not list(tmp_path.glob(f".{destination.name}.*.part"))


def test_reduce_ext4_image_default_runner_rewrites_gpt_after_detaching_loop(monkeypatch, tmp_path):
    source = tmp_path / "source.raw"
    destination = tmp_path / "small.raw"
    source.write_bytes(b"source")
    plan = build_reduction_plan(
        ImageManifest.model_validate(valid_manifest_v2()),
        source,
        destination,
        900 * 1024 * 1024,
        minimum_filesystem_bytes=100,
    )
    commands = []
    monkeypatch.setattr(reducer, "_run_output", lambda _arguments: "/dev/loop7")
    monkeypatch.setattr(reducer, "_run_command", commands.append)

    reduce_ext4_image(plan)

    with destination.open("rb") as published:
        assert published.read(len(b"source")) == b"source"
    assert destination.stat().st_size == plan.target_size_bytes
    assert commands[:3] == [
        ["e2fsck", "-f", "-y", "/dev/loop7p2"],
        ["resize2fs", "-M", "/dev/loop7p2"],
        ["losetup", "--detach", "/dev/loop7"],
    ]
    assert commands[-1][:2] == ["sgdisk", "--verify"]


def test_reduce_ext4_image_rejects_unexpected_loop_device(monkeypatch, tmp_path):
    source = tmp_path / "source.raw"
    destination = tmp_path / "small.raw"
    source.write_bytes(b"source")
    plan = build_reduction_plan(
        ImageManifest.model_validate(valid_manifest_v2()),
        source,
        destination,
        900 * 1024 * 1024,
        minimum_filesystem_bytes=100,
    )
    commands = []
    monkeypatch.setattr(reducer, "_run_output", lambda _arguments: "/dev/sda")
    monkeypatch.setattr(reducer, "_run_command", commands.append)

    with pytest.raises(ReductionError, match="inesperado"):
        reduce_ext4_image(plan)

    assert commands == [["losetup", "--detach", "/dev/sda"]]
    assert not destination.exists()


def test_reducer_tool_wrappers_fail_closed_and_strip_output(monkeypatch):
    monkeypatch.setattr(reducer.shutil, "which", lambda _name: None)
    with pytest.raises(ReductionError, match="Falta la herramienta sgdisk"):
        reducer._run_command(["sgdisk", "--verify"])
    with pytest.raises(ReductionError, match="Falta la herramienta losetup"):
        reducer._run_output(["losetup", "--show"])

    monkeypatch.setattr(reducer.shutil, "which", lambda _name: "/usr/bin/losetup")
    monkeypatch.setattr(
        reducer.subprocess,
        "run",
        lambda *_args, **_kwargs: type("Result", (), {"stdout": " /dev/loop7 \n"})(),
    )
    reducer._run_command(["sgdisk", "--verify"])
    assert reducer._run_output(["losetup", "--show"]) == "/dev/loop7"
