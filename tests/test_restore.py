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


def test_agent_restore_accepts_bios_mbr_and_rejects_firmware_table_mismatch() -> None:
    manifest = valid_bios_manifest()
    image_id = str(manifest["image_id"])
    assert task_agent.validate_restore_manifest(manifest, image_id) is manifest

    mismatch = copy.deepcopy(manifest)
    mismatch["firmware"] = {"type": "bios", "secure_boot": False}
    mismatch["capabilities"]["partition_table"] = "gpt"  # type: ignore[index]
    with pytest.raises(ValueError, match="incompatibles"):
        task_agent.validate_restore_manifest(mismatch, image_id)


def test_bios_target_requires_legacy_firmware(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "is_dir", lambda self: False)
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

    monkeypatch.setattr(
        task_agent.Path,
        "read_text",
        lambda self, **_kwargs: "Filename\tType\tSize\tUsed\tPriority\n/dev/vda partition 1 0 -2\n",
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
