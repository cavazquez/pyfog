import contextlib
import copy
from pathlib import Path

import pytest

from scripts import run_image_task as task_agent
from tests.test_image_manifest import valid_bios_manifest, valid_manifest, valid_manifest_v2


def restore_manifest() -> dict[str, object]:
    return copy.deepcopy(valid_manifest())


def target_selector(*, size: int = 1_073_741_824, sector: int = 512) -> dict[str, object]:
    return {
        "stable_id": "path:/dev/vda",
        "size_bytes": size,
        "logical_sector_bytes": sector,
        "operation": "restore",
        "source_size_bytes": 1_073_741_824,
        "required_logical_sector_bytes": 512,
        "model": "QEMU HARDDISK",
        "removable": False,
    }


def selected_disk(*, size: int = 1_073_741_824, sector: int = 512) -> dict[str, object]:
    return {
        "path": "/dev/vda",
        "name": "/dev/vda",
        "type": "disk",
        "size": size,
        "log-sec": sector,
        "model": "QEMU HARDDISK",
        "rm": False,
        "children": [],
    }


def test_agent_restore_manifest_rechecks_geometry_and_references() -> None:
    manifest = restore_manifest()
    image_id = str(manifest["image_id"])
    assert task_agent.validate_restore_manifest(manifest, image_id) is manifest

    malformed = copy.deepcopy(manifest)
    malformed["disk"]["gpt_disk_guid"] = "not-a-guid"  # type: ignore[index]
    with pytest.raises(ValueError, match="GUID GPT"):
        task_agent.validate_restore_manifest(malformed, image_id)

    overlapping = copy.deepcopy(manifest)
    overlapping["disk"]["partitions"][1]["start_sector"] = 100_000  # type: ignore[index]
    with pytest.raises(ValueError, match="superponen"):
        task_agent.validate_restore_manifest(overlapping, image_id)

    unpublishable = copy.deepcopy(manifest)
    unpublishable["publishable"] = False
    with pytest.raises(ValueError, match="autoriza"):
        task_agent.validate_restore_manifest(unpublishable, image_id)


def test_agent_restore_accepts_v2_and_rejects_unsupported_capabilities() -> None:
    manifest = valid_manifest_v2()
    image_id = str(manifest["image_id"])
    assert task_agent.validate_restore_manifest(manifest, image_id) is manifest

    unsupported = copy.deepcopy(manifest)
    unsupported["capabilities"]["volumes"] = "lvm"  # type: ignore[index]
    with pytest.raises(ValueError, match="volúmenes"):
        task_agent.validate_restore_manifest(unsupported, image_id)

    missing = copy.deepcopy(manifest)
    del missing["capabilities"]
    with pytest.raises(ValueError, match="capacidades explícitas"):
        task_agent.validate_restore_manifest(missing, image_id)

    boolean_version = copy.deepcopy(manifest)
    boolean_version["format_version"] = True
    with pytest.raises(ValueError, match="compatible con el agente"):
        task_agent.validate_restore_manifest(boolean_version, image_id)


def test_agent_extended_manifest_validates_all_disks_and_unique_artifacts() -> None:
    manifest = copy.deepcopy(valid_manifest_v2())
    first = copy.deepcopy(manifest["disk"])
    second = copy.deepcopy(first)
    first["disk_id"] = "wwn:disk-a"
    second["disk_id"] = "wwn:disk-b"
    second["gpt_disk_guid"] = "3e93f0c6-c66d-4c21-8c70-7a04ee4c1111"
    second["partitions"][0]["partition_guid"] = "3e93f0c6-c66d-4c21-8c70-7a04ee4c2222"
    second["partitions"][0]["filesystem_uuid"] = "A1B2C3D5"
    second["partitions"][0]["artifact"] = "partitions/disk-b-esp.img"
    second["partitions"][1]["partition_guid"] = "3e93f0c6-c66d-4c21-8c70-7a04ee4c3333"
    second["partitions"][1]["filesystem_uuid"] = "3e93f0c6-c66d-4c21-8c70-7a04ee4c4444"
    second["partitions"][1]["artifact"] = "partitions/disk-b-root.partclone"
    manifest["disk"] = first
    manifest["disks"] = [first, second]
    manifest["capabilities"]["disks"] = 2  # type: ignore[index]
    manifest["artifacts"].extend(  # type: ignore[union-attr]
        [
            {
                "path": "partitions/disk-b-esp.img",
                "size_bytes": 3,
                "compression": "none",
                "sha256": "a" * 64,
            },
            {
                "path": "partitions/disk-b-root.partclone",
                "size_bytes": 4,
                "compression": "zstd",
                "sha256": "b" * 64,
            },
        ]
    )

    parsed = task_agent.validate_restore_manifest(
        manifest, str(manifest["image_id"]), allow_extended=True
    )
    assert parsed["capabilities"]["disks"] == 2  # type: ignore[index]


def test_agent_restore_accepts_bios_mbr_and_rejects_firmware_table_mismatch() -> None:
    manifest = valid_bios_manifest()
    image_id = str(manifest["image_id"])
    assert task_agent.validate_restore_manifest(manifest, image_id) is manifest

    mismatch = copy.deepcopy(manifest)
    mismatch["firmware"] = {"type": "bios", "secure_boot": False}
    mismatch["capabilities"]["partition_table"] = "gpt"  # type: ignore[index]
    with pytest.raises(ValueError, match="incompatibles"):
        task_agent.validate_restore_manifest(mismatch, image_id)


def test_restore_storage_plans_validate_lvm_luks_and_raid_profiles() -> None:
    base = valid_manifest_v2()
    selected = selected_disk()
    selector = target_selector()
    lvm_manifest = copy.deepcopy(base)
    lvm_manifest["capabilities"]["volumes"] = "lvm-linear"  # type: ignore[index]
    lvm_manifest["volumes"] = [
        {
            "type": "lvm-linear",
            "pv_uuid": "pv-1",
            "vg_name": "ubuntu-vg",
            "vg_uuid": "vg-1",
            "lv_name": "root",
            "lv_uuid": "lv-1",
            "size_bytes": 100,
        }
    ]
    lvm_plan = task_agent.build_restore_storage_plan(
        lvm_manifest,
        [(selected, selector, lvm_manifest["disk"])],  # type: ignore[list-item]
    )
    assert lvm_plan.profile == "lvm-linear"

    luks_manifest = copy.deepcopy(base)
    luks_manifest["capabilities"]["encryption"] = "luks2"  # type: ignore[index]
    luks_manifest["encryption"] = {
        "type": "luks2",
        "uuid": "luks-1",
        "cipher": "aes-xts-plain64",
        "sector_size": 512,
    }
    luks_plan = task_agent.build_restore_storage_plan(
        luks_manifest,
        [(selected, selector, luks_manifest["disk"])],  # type: ignore[list-item]
    )
    assert luks_plan.profile == "luks2"

    raid_manifest = copy.deepcopy(base)
    first = copy.deepcopy(raid_manifest["disk"])
    second = copy.deepcopy(raid_manifest["disk"])
    first["disk_id"] = "wwn:disk-a"
    second["disk_id"] = "wwn:disk-b"
    raid_manifest["disk"] = first
    raid_manifest["disks"] = [first, second]
    raid_manifest["capabilities"]["disks"] = 2  # type: ignore[index]
    raid_manifest["capabilities"]["volumes"] = "raid1"  # type: ignore[index]
    raid_manifest["raid_arrays"] = [
        {
            "level": 1,
            "uuid": "raid-1",
            "metadata": "1.2",
            "member_ids": ["wwn:disk-a", "wwn:disk-b"],
            "size_bytes": 100,
        }
    ]
    raid_plan = task_agent.build_restore_storage_plan(
        raid_manifest,
        [
            (selected, selector, first),
            (selected, selector, second),
        ],
    )
    assert raid_plan.profile == "raid1"


def test_bios_target_requires_legacy_firmware(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "is_dir", lambda _self: False)
    manifest = valid_bios_manifest()
    selector = target_selector()
    task_agent.validate_restore_target(selected_disk(), selector, manifest)
    monkeypatch.setattr(Path, "is_dir", lambda self: str(self) == "/sys/firmware/efi")
    with pytest.raises(ValueError, match="firmware UEFI"):
        task_agent.validate_restore_target(selected_disk(), selector, manifest)


def test_agent_restore_target_accepts_larger_disk_but_rejects_changed_or_busy_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "is_dir", lambda self: str(self) == "/sys/firmware/efi")
    manifest = restore_manifest()
    selected = selected_disk(size=2 * 1_073_741_824)
    selector = target_selector(size=2 * 1_073_741_824)
    task_agent.validate_restore_target(selected, selector, manifest)

    with pytest.raises(ValueError, match="menor"):
        task_agent.validate_restore_target(
            selected_disk(size=512 * 1024 * 1024),
            target_selector(size=512 * 1024 * 1024),
            manifest,
        )
    with pytest.raises(ValueError, match="sector"):
        task_agent.validate_restore_target(
            selected_disk(size=2 * 1_073_741_824, sector=4096),
            target_selector(size=2 * 1_073_741_824, sector=4096),
            manifest,
        )
    busy = selected_disk(size=2 * 1_073_741_824)
    busy["children"] = [{"path": "/dev/vda1", "mountpoints": ["/mnt"]}]
    with pytest.raises(ValueError, match="montada"):
        task_agent.validate_restore_target(busy, selector, manifest)


def test_target_quiescence_checks_whole_disk_and_swap_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        task_agent.Path,
        "read_text",
        lambda self, **_kwargs: (
            "36 29 0:32 / / rw - ext4 /dev/vda rw\n"
            if str(self) == "/proc/self/mountinfo"
            else "Filename\tType\tSize\tUsed\tPriority\n/dev/vda2 partition 1 0 -2\n"
        ),
    )
    monkeypatch.setattr(task_agent.Path, "is_file", lambda self: str(self) == "/proc/swaps")
    with pytest.raises(ValueError, match="partición montada"):
        task_agent.assert_target_is_quiescent(selected_disk())


def test_hot_capture_thaws_in_reverse_order_even_after_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        task_agent,
        "run_command",
        lambda arguments, **_kwargs: calls.append(arguments) or "",
    )
    frozen = task_agent.freeze_filesystems(["/", "/home"])
    assert frozen == ["/", "/home"]
    task_agent.thaw_filesystems(frozen)
    assert calls[-2:] == [
        ["fsfreeze", "--unfreeze", "/home"],
        ["fsfreeze", "--unfreeze", "/"],
    ]


def test_hot_capture_releases_freeze_when_body_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        task_agent,
        "mounted_filesystems",
        lambda _partitions: ["/", "/home"],
    )
    monkeypatch.setattr(
        task_agent,
        "run_command",
        lambda arguments, **_kwargs: calls.append(arguments) or "",
    )
    with pytest.raises(RuntimeError), task_agent.hot_capture_scope([{"device": "/dev/vda2"}]):
        raise RuntimeError("interrumpido")
    assert calls[-2:] == [
        ["fsfreeze", "--unfreeze", "/home"],
        ["fsfreeze", "--unfreeze", "/"],
    ]

    monkeypatch.setattr(
        task_agent.Path,
        "read_text",
        lambda _self, **_kwargs: (
            "Filename\tType\tSize\tUsed\tPriority\n/dev/vda partition 1 0 -2\n"
        ),
    )
    with pytest.raises(ValueError, match="swap activo"):
        task_agent.assert_target_is_quiescent(selected_disk())


def test_restore_partition_table_moves_secondary_gpt_header_on_larger_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(arguments: list[str], **_kwargs: object) -> str:
        calls.append(arguments)
        return ""

    monkeypatch.setattr(task_agent, "run_command", fake_run)
    monkeypatch.setattr(task_agent.shutil, "which", lambda name: "/usr/bin/" + name)
    task_agent.restore_partition_table(
        "/dev/vda",
        restore_manifest()["disk"],  # type: ignore[arg-type]
    )
    assert ["sgdisk", "--move-second-header", "/dev/vda"] in calls
    assert ["sgdisk", "--verify", "/dev/vda"] in calls
    assert any(argument.startswith("--partition-guid=1:") for call in calls for argument in call)


def test_larger_gpt_target_expands_only_final_ext4_root(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        task_agent,
        "run_command",
        lambda arguments, **_kwargs: (
            calls.append(arguments)
            or (
                "Disk identifier (GUID): x\nlast usable sector is 4194238"
                if arguments[:2] == ["sgdisk", "--print"]
                else ""
            )
        ),
    )
    monkeypatch.setattr(task_agent.shutil, "which", lambda name: "/usr/bin/" + name)
    manifest = restore_manifest()
    selected = selected_disk(size=2 * 1_073_741_824)
    task_agent.expand_gpt_root_partition(
        "/dev/vda",
        selected,
        manifest["disk"],  # type: ignore[arg-type]
    )
    assert ["sgdisk", "--move-second-header", "/dev/vda"] in calls
    assert ["e2fsck", "-f", "-p", "/dev/vda2"] in calls
    assert ["resize2fs", "/dev/vda2"] in calls
    assert ["sgdisk", "--verify", "/dev/vda"] in calls

    unsupported = copy.deepcopy(manifest["disk"])
    unsupported["partitions"][1]["filesystem"] = "xfs"  # type: ignore[index]
    with pytest.raises(ValueError, match="ext4"):
        task_agent.validate_target_expansion(selected, unsupported)


def test_optional_swap_is_initialized_with_manifest_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        task_agent,
        "run_command",
        lambda arguments, **_kwargs: calls.append(arguments) or "",
    )
    swap = {
        "number": 3,
        "role": "swap",
        "filesystem_uuid": "c13eb601-8c76-4f0c-86be-6da2fb6f6666",
    }
    task_agent.initialize_swap(swap, "/dev/vda")
    assert calls == [
        [
            "mkswap",
            "-U",
            swap["filesystem_uuid"],
            "/dev/vda3",
        ]
    ]


def test_parse_gpt_normalizes_linux_vfat_esp() -> None:
    output = """
Disk identifier (GUID): 7c1c8a08-13b3-4505-8e1c-9b0d2da93333
First usable sector is 2048, last usable sector is 2095103
Number  Start (sector)    End (sector)  Size       Code  Name
   1            2048          206847   100.0 MiB  EF00  EFI System
   2          206848         2095103   920.0 MiB  8300  Linux root
"""
    selected = selected_disk()
    selected["children"] = [
        {
            "name": "/dev/vda1",
            "path": "/dev/vda1",
            "fstype": "vfat",
            "uuid": "A1B2C3D4",
            "partuuid": "a8f7b8d4-ccdf-4d9d-8d2d-6da2fb6f4444",
        },
        {
            "name": "/dev/vda2",
            "path": "/dev/vda2",
            "fstype": "ext4",
            "uuid": "bbd0a02f-8c3d-4384-8f3c-3b109b5d5555",
            "partuuid": "bbd0a02f-8c3d-4384-8f3c-3b109b5d5555",
        },
    ]
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(task_agent, "run_command", lambda *_args, **_kwargs: output)
        geometry = task_agent.parse_gpt("/dev/vda", selected)
    finally:
        monkeypatch.undo()
    assert geometry["partitions"][0]["filesystem"] == "fat32"


def test_parse_gpt_finds_assembled_raid_from_complete_lsblk_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = """
Disk identifier (GUID): 7c1c8a08-13b3-4505-8e1c-9b0d2da93333
First usable sector is 2048, last usable sector is 2095103
Number  Start (sector)    End (sector)  Size       Code  Name
   1            2048          206847   100.0 MiB  EF00  EFI System
   2          206848         2095103   920.0 MiB  FD00  Linux RAID
"""
    md_report = "\n".join(
        [
            "MD_LEVEL=raid1",
            "MD_STATE=clean,active",
            "MD_UUID=5d5d5d5d-1111-2222-3333-444444444444",
            "MD_METADATA=1.2",
            "MD_DEVICE_0_DEV=/dev/vda2",
            "MD_DEVICE_1_DEV=/dev/vdb2",
            "MD_ARRAY_SIZE=966367232",
        ]
    )
    selected = selected_disk()
    selected["stable_id"] = "wwn:disk-a"
    selected["children"] = [
        {
            "name": "/dev/vda1",
            "path": "/dev/vda1",
            "fstype": "vfat",
            "uuid": "A1B2C3D4",
            "partuuid": "a8f7b8d4-ccdf-4d9d-8d2d-6da2fb6f4444",
        },
        {
            "name": "/dev/vda2",
            "path": "/dev/vda2",
            "fstype": "linux_raid_member",
            "uuid": "5d5d5d5d-aaaa-bbbb-cccc-444444444444",
            "partuuid": "bbd0a02f-8c3d-4384-8f3c-3b109b5d5555",
        },
    ]
    inventory = {
        "blockdevices": [
            selected,
            {
                "name": "/dev/md0",
                "path": "/dev/md0",
                "type": "raid1",
                "children": [{"name": "/dev/vda2", "path": "/dev/vda2"}],
            },
        ]
    }

    def fake_run(arguments: list[str], **_kwargs: object) -> str:
        if arguments[0] == "sgdisk":
            return table
        if arguments[0] == "mdadm":
            return md_report
        if arguments[0] == "blkid":
            return "TYPE=ext4\nUUID=bbd0a02f-8c3d-4384-8f3c-3b109b5d5555\n"
        raise AssertionError(arguments)

    monkeypatch.setattr(task_agent, "run_command", fake_run)
    geometry = task_agent.parse_gpt("/dev/vda", selected, inventory=inventory)

    root = next(partition for partition in geometry["partitions"] if partition["role"] == "root")
    assert geometry["storage"]["profile"] == "raid1"
    assert geometry["storage"]["raid_device"] == "/dev/md0"
    assert geometry["storage"]["raid_array"]["member_ids"] == ["wwn:disk-a"]
    assert root["capture_device"] == "/dev/md0"


def test_parse_mbr_reads_signature_and_grub_embedding_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    selected = selected_disk()
    selected["pttype"] = "dos"
    selected["children"] = [
        {
            "name": "/dev/vda1",
            "path": "/dev/vda1",
            "fstype": "ext4",
            "uuid": "bbd0a02f-8c3d-4384-8f3c-3b109b5d5555",
            "mountpoints": ["/"],
        }
    ]
    table = (
        '{"partitiontable":{"label":"dos","id":"0x1a2b3c4d",'
        '"partitions":[{"node":"/dev/vda1","start":2048,"size":2095104,'
        '"type":"0x83","bootable":true}]}}'
    )
    monkeypatch.setattr(task_agent, "run_command", lambda *_args, **_kwargs: table)
    geometry = task_agent.parse_mbr("/dev/vda", selected)
    assert geometry["mbr_disk_signature"] == "1a2b3c4d"
    assert geometry["partitions"][0]["role"] == "root"
    assert geometry["partitions"][0]["start_sector"] == 2048


def test_local_agent_capabilities_follow_installed_tools_and_key_plugin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    available = {
        "partclone.xfs",
        "partclone.btrfs",
        "fsfreeze",
        "findmnt",
        "e2fsck",
        "resize2fs",
        "pvs",
        "vgs",
        "lvs",
        "pvcreate",
        "vgcreate",
        "lvcreate",
        "lvchange",
        "mdadm",
        "cryptsetup",
    }
    monkeypatch.setattr(
        task_agent.shutil,
        "which",
        lambda executable: executable if executable in available else None,
    )
    monkeypatch.setenv("PYFOG_PLUGIN_DIR", str(tmp_path))
    monkeypatch.setenv("PYFOG_LUKS_KEY_PLUGIN", "test-provider")
    monkeypatch.setenv("PYFOG_LUKS_KEY_REF", "secret/ref")

    capabilities = task_agent.local_agent_capabilities()

    assert {
        "capture.hot",
        "expand.ext4",
        "lvm-linear",
        "raid1",
        "luks2",
        "key-provider",
        "partclone.xfs",
        "partclone.btrfs",
    }.issubset(capabilities)

    monkeypatch.delenv("PYFOG_LUKS_KEY_PLUGIN")
    assert "key-provider" not in task_agent.local_agent_capabilities()


def test_clone_identity_removes_source_identity_and_enables_first_boot_dhcp(tmp_path: Path) -> None:
    root = tmp_path / "root"
    for relative in (
        "etc/ssh",
        "etc/netplan",
        "etc/systemd/network",
        "etc/NetworkManager/system-connections",
        "etc/udev/rules.d",
        "var/lib/dbus",
        "var/lib/systemd",
    ):
        (root / relative).mkdir(parents=True)
    (root / "etc/machine-id").symlink_to("/run/machine-id")
    (root / "var/lib/dbus/machine-id").write_text("source-db-id\n", encoding="utf-8")
    (root / "var/lib/systemd/random-seed").write_bytes(b"source-seed")
    (root / "etc/ssh/ssh_host_rsa_key").write_text("source-key", encoding="utf-8")
    (root / "etc/netplan/50-source.yaml").write_text("addresses: []", encoding="utf-8")
    (root / "etc/systemd/network/20-source.network").write_text(
        "Address=10.0.0.1", encoding="utf-8"
    )
    (root / "etc/NetworkManager/system-connections/source.nmconnection").write_text(
        "[connection]", encoding="utf-8"
    )
    (root / "etc/udev/rules.d/70-persistent-net.rules").write_text("source", encoding="utf-8")

    task_agent.customize_clone_identity(root, "clone-01.example.test")

    assert (root / "etc/hostname").read_text(encoding="utf-8") == "clone-01.example.test\n"
    assert (root / "etc/machine-id").read_text(encoding="utf-8") == ""
    assert (root / "var/lib/dbus/machine-id").read_text(encoding="utf-8") == ""
    assert not (root / "var/lib/systemd/random-seed").exists()
    assert not list((root / "etc/ssh").glob("ssh_host_*"))
    assert (root / "etc/netplan/99-pyfog-dhcp.yaml").is_file()
    assert not (root / "etc/netplan/50-source.yaml").exists()
    assert not (root / "etc/systemd/network/20-source.network").exists()
    assert not (root / "etc/NetworkManager/system-connections/source.nmconnection").exists()
    assert not (root / "etc/udev/rules.d/70-persistent-net.rules").exists()
    assert (root / "etc/pyfog/clone-identity").read_text(encoding="utf-8") == "pyfog-clone-v1\n"
    assert (root / "usr/local/sbin/pyfog-first-boot-identity").stat().st_mode & 0o111
    assert (
        root / "etc/systemd/system/multi-user.target.wants/pyfog-first-boot-identity.service"
    ).is_symlink()


def test_uefi_boot_uses_fallback_without_nvram(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    esp = tmp_path / "esp"
    (esp / "EFI/BOOT").mkdir(parents=True)
    (esp / "EFI/BOOT/BOOTX64.EFI").write_bytes(b"grub")
    calls: list[list[str]] = []

    @contextlib.contextmanager
    def fake_mounts(*_args: object, **_kwargs: object):
        yield {"root": tmp_path / "root", "esp": esp, "boot": tmp_path / "boot"}

    monkeypatch.setattr(task_agent, "mounted_target", fake_mounts)
    monkeypatch.setattr(
        task_agent,
        "run_command",
        lambda arguments, **_kwargs: calls.append(arguments) or "",
    )
    monkeypatch.setattr(task_agent.shutil, "which", lambda name: "/usr/bin/" + name)
    task_agent.ensure_uefi_boot(
        tmp_path / "mount",
        "/dev/vda",
        restore_manifest()["disk"],  # type: ignore[arg-type]
    )
    grub_call = next(call for call in calls if call[0] == "grub-install")
    assert "--removable" in grub_call
    assert "--no-nvram" in grub_call
    assert not any("--bootloader-id" in argument for argument in grub_call)
