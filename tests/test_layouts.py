import json

import pytest

from pyfog.layouts import (
    LayoutError,
    filesystem_tool,
    lvm_restore_commands,
    parse_btrfs_subvolumes,
    parse_luks2_metadata,
    parse_lvm_linear_reports,
    parse_mdraid1_export,
    raid1_restore_commands,
    validate_luks2_match,
)


def report(name: str, rows: list[dict[str, object]]) -> str:
    return json.dumps({"report": [{name: rows}]})


def test_lvm_parser_accepts_one_linear_chain_and_rejects_snapshots():
    pvs = report("pv", [{"pv_uuid": "pv-1"}])
    vgs = report("vg", [{"vg_name": "ubuntu-vg", "vg_uuid": "vg-1"}])
    lvs = report(
        "lv",
        [
            {
                "lv_name": "root",
                "lv_uuid": "lv-1",
                "vg_name": "ubuntu-vg",
                "vg_uuid": "vg-1",
                "lv_layout": "linear",
                "lv_size_bytes": 100,
            }
        ],
    )
    layout = parse_lvm_linear_reports(pvs, vgs, lvs)
    assert layout.lv_name == "root"

    with pytest.raises(LayoutError, match="snapshot"):
        parse_lvm_linear_reports(pvs, vgs, lvs.replace('"linear"', '"snapshot"'))


def test_raid_and_luks_parsers_are_metadata_only():
    raid = parse_mdraid1_export(
        "\n".join(
            [
                "MD_LEVEL=raid1",
                "MD_STATE=clean",
                "MD_UUID=raid-1",
                "MD_METADATA=1.2",
                "MD_ARRAY_SIZE=1024",
                "MD_DEVICE_0_DEV=/dev/vda1",
                "MD_DEVICE_1_DEV=/dev/vdb1",
            ]
        )
    )
    assert raid.member_ids == ("/dev/vda1", "/dev/vdb1")
    luks = parse_luks2_metadata(
        {"version": 2, "uuid": "luks-1", "cipher": "aes-xts-plain64", "sector-size": 4096}
    )
    assert luks.uuid == "luks-1"
    assert "passphrase" not in luks.__dict__


def test_btrfs_parser_requires_known_root_and_tools_are_fixed():
    result = parse_btrfs_subvolumes(
        "ID 256 gen 10 top level 5 path @\nID 257 gen 10 top level 5 path @home"
    )
    assert [item.path for item in result] == ["@", "@home"]
    with pytest.raises(LayoutError, match="snapshot"):
        parse_btrfs_subvolumes("ID 256 gen 10 top level 5 path snapshots/base")
    assert filesystem_tool("xfs") == "partclone.xfs"


def test_restore_planners_keep_devices_and_secrets_explicit():
    lvm = parse_lvm_linear_reports(
        report("pv", [{"pv_uuid": "pv-1"}]),
        report("vg", [{"vg_name": "vg", "vg_uuid": "vg-1"}]),
        report(
            "lv",
            [
                {
                    "lv_name": "root",
                    "lv_uuid": "lv-1",
                    "vg_name": "vg",
                    "vg_uuid": "vg-1",
                    "lv_layout": "linear",
                    "lv_size_bytes": 100,
                }
            ],
        ),
    )
    assert lvm_restore_commands(lvm, "/dev/vda1")[0][-1] == "/dev/vda1"
    raid = parse_mdraid1_export(
        "\n".join(
            [
                "MD_LEVEL=raid1",
                "MD_STATE=clean",
                "MD_UUID=raid-1",
                "MD_METADATA=1.2",
                "MD_ARRAY_SIZE=1024",
                "MD_DEVICE_0_DEV=/dev/vda1",
                "MD_DEVICE_1_DEV=/dev/vdb1",
            ]
        )
    )
    assert "--uuid=raid-1" in raid1_restore_commands(raid, ["/dev/vda1", "/dev/vdb1"])
    first = parse_luks2_metadata(
        {"version": 2, "uuid": "luks-1", "cipher": "aes", "sector-size": 512}
    )
    validate_luks2_match(first, first)
    with pytest.raises(LayoutError):
        validate_luks2_match(
            first,
            parse_luks2_metadata(
                {"version": 2, "uuid": "other", "cipher": "aes", "sector-size": 512}
            ),
        )


def test_raid_restore_requires_the_declared_stable_member_order():
    raid = parse_mdraid1_export(
        "\n".join(
            [
                "MD_LEVEL=raid1",
                "MD_STATE=clean,active",
                "MD_UUID=raid-1",
                "MD_METADATA=1.2",
                "MD_ARRAY_SIZE=1024",
                "MD_DEVICE_0_DEV=/dev/vda1",
                "MD_DEVICE_1_DEV=/dev/vdb1",
            ]
        )
    )
    with pytest.raises(LayoutError, match="identidades"):
        raid1_restore_commands(
            raid,
            ["/dev/vda1", "/dev/vdb1"],
            member_ids=("wwn:disk-b", "wwn:disk-a"),
        )
