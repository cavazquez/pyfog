import copy
import urllib.error
from pathlib import Path

import pytest

from scripts import run_image_task as task_agent
from tests.test_image_manifest import valid_manifest
from tests.test_restore import selected_disk, target_selector


def test_source_restore_contract_accepts_larger_target_and_uefi_manifest() -> None:
    manifest = valid_manifest()
    assert task_agent.validate_restore_manifest(manifest, str(manifest["image_id"])) is manifest
    selected = selected_disk(size=2 * 1_073_741_824)
    selector = target_selector(size=2 * 1_073_741_824)
    task_agent.validate_restore_target(selected, selector, manifest)


def test_negative_contract_rejects_corrupt_manifest_and_incompatible_target() -> None:
    manifest = copy.deepcopy(valid_manifest())
    manifest["publishable"] = False
    with pytest.raises(ValueError, match="autoriza"):
        task_agent.validate_restore_manifest(manifest, str(manifest["image_id"]))

    with pytest.raises(ValueError, match="menor"):
        task_agent.validate_restore_target(
            selected_disk(size=512 * 1024**2),
            target_selector(size=512 * 1024**2),
            valid_manifest(),
        )


def test_clone_contract_keeps_identity_changes_scoped_to_target(tmp_path: Path) -> None:
    root = tmp_path / "target-root"
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
    (root / "etc/machine-id").write_text("source-id\n", encoding="utf-8")
    (root / "etc/ssh/ssh_host_rsa_key").write_text("source-key", encoding="utf-8")
    (root / "var/lib/dbus/machine-id").write_text("source-db-id\n", encoding="utf-8")
    (root / "var/lib/systemd/random-seed").write_bytes(b"source-seed")
    (root / "etc/netplan/50-source.yaml").write_text("addresses: []", encoding="utf-8")

    task_agent.customize_clone_identity(root, "target-b.example.test")

    assert (root / "etc/hostname").read_text(encoding="utf-8") == "target-b.example.test\n"
    assert (root / "etc/machine-id").read_text(encoding="utf-8") == ""
    assert not list((root / "etc/ssh").glob("ssh_host_*"))
    assert not (root / "var/lib/systemd/random-seed").exists()
    assert (root / "etc/netplan/99-pyfog-dhcp.yaml").is_file()


def test_network_loss_does_not_allow_a_lease_heartbeat_to_claim_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise urllib.error.URLError("coordinador no disponible")

    monkeypatch.setattr(task_agent, "json_request", unavailable)
    heartbeat = task_agent.LeaseHeartbeat(
        "http://127.0.0.1:1", "task-id", "test-token", None, interval=0
    )
    heartbeat._run()
    with pytest.raises(ValueError, match="renovar la lease"):
        heartbeat.check()
