import hashlib
import json
import subprocess
import urllib.error
from pathlib import Path

import pytest

from pyfog.layouts import Luks2Layout, LvmLinearLayout, Raid1Layout
from scripts import run_image_task as task_agent


class FakeResponse:
    def __init__(self, payload: bytes, *, status: int = 200) -> None:
        self.payload = payload
        self.status = status

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            return self.payload
        value, self.payload = self.payload[:size], self.payload[size:]
        return value


class FakeOpener:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.requests = []

    def open(self, request, *, timeout: int):
        self.requests.append((request, timeout))
        return self.response


def test_transport_validators_and_json_response_cover_rejection_paths() -> None:
    assert (
        task_agent.validate_server("https://coordinator.example/").hostname == "coordinator.example"
    )
    assert task_agent.validate_server("http://127.0.0.1:8080").port == 8080
    assert task_agent.validate_server("http://[::1]").hostname == "::1"
    assert task_agent.validate_token("agent-token") == "agent-token"

    for server in (
        "ftp://coordinator.example",
        "http://coordinator.example",
        "https://user:pass@coordinator.example",  # pragma: allowlist secret
        "https://coordinator.example/path",
        "https://coordinator.example?debug=1",
        "https://coordinator.example:65536",
    ):
        with pytest.raises(ValueError, match="URL base HTTPS"):
            task_agent.validate_server(server)
    for token in ("", "x" * 257, "bad\nvalue", "bad\rvalue"):
        with pytest.raises(ValueError, match="capacidad"):
            task_agent.validate_token(token)

    assert task_agent.response_json(FakeResponse(b'{"status":"ok"}')) == {"status": "ok"}
    for payload in (b"[]", b"not-json"):
        with pytest.raises((TypeError, ValueError)):
            task_agent.response_json(FakeResponse(payload))
    with pytest.raises(ValueError, match="límite"):
        task_agent.response_json(FakeResponse(b"x" * (task_agent.MAX_RESPONSE_BYTES + 1)))


def test_json_request_and_upload_chunk_build_authenticated_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opener = FakeOpener(FakeResponse(b'{"accepted":true}'))
    monkeypatch.setattr(task_agent, "opener", lambda _ca_file: opener)

    result = task_agent.json_request(
        "https://coordinator.example/api",
        method="POST",
        payload={"message": "hola"},
        token="secret-token",
        ca_file=None,
        expected_status={200},
    )
    assert result == {"accepted": True}
    request, timeout = opener.requests[-1]
    assert request.method == "POST"
    assert request.get_header("Authorization") == "Bearer secret-token"
    assert json.loads(request.data) == {"message": "hola"}
    assert timeout == 30

    opener.response = FakeResponse(b'{"accepted":true}')
    upload = task_agent.upload_chunk(
        "https://coordinator.example/artifact",
        b"payload",
        token="secret-token",
        ca_file=None,
        index=2,
        offset=4,
        chunk_size=32,
    )
    assert upload == {"accepted": True}
    request, timeout = opener.requests[-1]
    assert request.get_header("X-pyfog-chunk-index") == "2"
    assert request.get_header("X-pyfog-chunk-offset") == "4"
    assert request.get_header("X-pyfog-chunk-sha256") == hashlib.sha256(b"payload").hexdigest()
    assert timeout == 60
    with pytest.raises(ValueError, match="negociado"):
        task_agent.upload_chunk(
            "https://coordinator.example/artifact",
            b"too-large",
            token="secret-token",
            ca_file=None,
            index=0,
            offset=0,
            chunk_size=2,
        )


@pytest.mark.parametrize(
    ("exception", "message"),
    [
        (urllib.error.HTTPError("https://example.test", 503, "", {}, None), "HTTP 503"),
        (urllib.error.URLError("offline"), "conectar"),
    ],
)
def test_json_request_translates_transport_errors(
    monkeypatch: pytest.MonkeyPatch, exception: Exception, message: str
) -> None:
    class BrokenOpener:
        def open(self, *_args: object, **_kwargs: object):
            raise exception

    monkeypatch.setattr(task_agent, "opener", lambda _ca_file: BrokenOpener())
    with pytest.raises(ValueError, match=message):
        task_agent.json_request(
            "https://coordinator.example/api",
            method="GET",
            payload=None,
            token="token",
            ca_file=None,
            expected_status={200},
        )


def test_run_command_and_inventory_selection_are_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    monkeypatch.setattr(task_agent.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(
        task_agent.subprocess,
        "run",
        lambda arguments, **kwargs: (
            calls.append((arguments, kwargs))
            or subprocess.CompletedProcess(arguments, 0, stdout="output", stderr="")
        ),
    )
    assert task_agent.run_command(["lsblk", "--json"]) == "output"
    assert calls[0][0] == ["/usr/bin/lsblk", "--json"]
    assert calls[0][1]["timeout"] == 30
    monkeypatch.setattr(
        task_agent.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, stdout="", stderr="error"),
    )
    with pytest.raises(ValueError, match="rechazó"):
        task_agent.run_command(["lsblk"])
    monkeypatch.setattr(task_agent.shutil, "which", lambda _name: None)
    with pytest.raises(ValueError, match="Falta"):
        task_agent.run_command(["lsblk"])

    document = {
        "blockdevices": [
            {
                "type": "disk",
                "path": "/dev/vda",
                "wwn": "disk-wwn",
                "serial": "disk-serial",
                "size": 100,
                "model": "QEMU",
                "log-sec": 512,
                "rm": False,
            }
        ]
    }
    selector = {"stable_id": "wwn:disk-wwn", "size_bytes": 100, "model": "QEMU"}
    assert task_agent.select_disk(document, selector)["path"] == "/dev/vda"
    selector["stable_id"] = "serial:disk-serial"
    assert task_agent.select_disk(document, selector)["path"] == "/dev/vda"
    selector["stable_id"] = "path:/dev/vda"
    assert task_agent.select_disk(document, selector)["path"] == "/dev/vda"
    for invalid in (
        {"stable_id": "unknown:disk", "size_bytes": 100},
        {"stable_id": "path:/dev/vda", "size_bytes": 99},
    ):
        with pytest.raises(ValueError, match="disco"):
            task_agent.select_disk(document, invalid)

    monkeypatch.setattr(
        task_agent,
        "run_command",
        lambda *_args, **_kwargs: '{"blockdevices": []}',
    )
    assert task_agent.block_inventory() == {"blockdevices": []}
    monkeypatch.setattr(task_agent, "run_command", lambda *_args, **_kwargs: "no-json")
    with pytest.raises(ValueError, match="inventario"):
        task_agent.block_inventory()


def test_manifest_and_path_helpers_enforce_bounded_inputs(tmp_path: Path) -> None:
    assert task_agent.parse_export("TYPE=ext4\nUUID=abc=def\nignored") == {
        "TYPE": "ext4",
        "UUID": "abc=def",
    }
    assert task_agent.partition_device("/dev/vda", 2) == "/dev/vda2"
    assert task_agent.partition_device("/dev/nvme0n1", 2) == "/dev/nvme0n1p2"
    assert task_agent.role_code("esp") == "EF00"
    assert task_agent.role_code("root", "mbr") == "83"
    assert task_agent.manifest_int(4, "tamaño", minimum=1) == 4
    assert task_agent.manifest_uuid("3e93f0c6-c66d-4c21-8c70-7a04ee4c1111", "id")
    with pytest.raises(ValueError, match="partición"):
        task_agent.partition_device("/dev/vda", 0)
    with pytest.raises(ValueError, match="rol"):
        task_agent.role_code("unknown")
    with pytest.raises(ValueError, match="entero"):
        task_agent.manifest_int(True, "tamaño")
    with pytest.raises(ValueError, match="UUID"):
        task_agent.manifest_uuid("not-uuid", "id")

    assert task_agent.safe_artifact_path("partitions/root.img") == "partitions/root.img"
    assert task_agent.safe_artifact_path("boot-sector.bin", allow_boot_sector=True)
    for value in ("../secret", "partitions/../secret", "boot-sector.bin"):
        with pytest.raises(ValueError, match="artefacto"):
            task_agent.safe_artifact_path(value)
    staging = task_agent.staging_path(tmp_path / "staging", "partitions/root.img")
    assert staging == tmp_path / "staging/partitions/root.img"
    staging.write_bytes(b"old")
    staging.unlink()
    outside = tmp_path / "outside"
    outside.write_text("outside", encoding="utf-8")
    (tmp_path / "staging/partitions/link.img").symlink_to(outside)
    with pytest.raises(ValueError, match="enlace"):
        task_agent.staging_path(tmp_path / "staging", "partitions/link.img")


def test_layout_inspection_uses_read_only_tool_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    lvm = LvmLinearLayout("pv-1", "vg0", "vg-1", "root", "lv-1", 100)
    report = {
        "pvs": '{"report":[{"pv":[{"pv_uuid":"pv-1","pv_name":"/dev/vda2","pv_size":"200B"}]}]}',
        "vgs": '{"report":[{"vg":[{"vg_name":"vg0","vg_uuid":"vg-1"}]}]}',
        "lvs": (
            '{"report":[{"lv":[{"lv_name":"root","lv_uuid":"lv-1",'
            '"vg_name":"vg0","vg_uuid":"vg-1","lv_layout":"linear",'
            '"lv_size":"100B"}]}]}'
        ),
    }

    def fake_run(arguments: list[str], **_kwargs: object) -> str:
        if arguments[0] == "pvs":
            return report["pvs"]
        if arguments[0] == "vgs":
            return report["vgs"]
        if arguments[0] == "lvs":
            return report["lvs"]
        if arguments[0] == "blkid":
            return "TYPE=ext4\nUUID=root-uuid\n"
        raise AssertionError(arguments)

    monkeypatch.setattr(task_agent, "run_command", fake_run)
    assert task_agent.inspect_lvm_layout("/dev/vda2") == lvm
    assert task_agent.inspect_lvm_filesystem("/dev/vda2") == (lvm, "ext4", "root-uuid", None)
    with pytest.raises(ValueError, match="dispositivo seguro"):
        task_agent.inspect_lvm_layout("device/source")

    raid = Raid1Layout("raid-1", "1.2", ("/dev/vda2", "/dev/vdb2"), 100)
    raid_output = "\n".join(
        [
            "MD_LEVEL=raid1",
            "MD_STATE=clean,active",
            "MD_UUID=raid-1",
            "MD_METADATA=1.2",
            "MD_DEVICE_0_DEV=/dev/vda2",
            "MD_DEVICE_1_DEV=/dev/vdb2",
            "MD_ARRAY_SIZE=100",
        ]
    )
    monkeypatch.setattr(
        task_agent,
        "run_command",
        lambda arguments, **_kwargs: (
            raid_output if arguments[0] == "mdadm" else "TYPE=xfs\nUUID=raid-root\n"
        ),
    )
    assert task_agent.inspect_raid1_layout("/dev/md0") == raid
    assert task_agent.inspect_raid1_filesystem("/dev/md0") == (raid, "xfs", "raid-root", None)
    with pytest.raises(ValueError, match="mdadm"):
        task_agent.inspect_raid1_layout("/dev/mapper/root")


def test_luks2_unlock_and_close_keep_keys_out_of_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = Luks2Layout("luks-1", "aes-xts-plain64", 512)
    calls = []
    monkeypatch.setattr(task_agent, "inspect_luks2_layout", lambda _device: expected)
    monkeypatch.setattr(task_agent.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(
        task_agent.subprocess,
        "run",
        lambda arguments, **kwargs: (
            calls.append((arguments, kwargs))
            or subprocess.CompletedProcess(arguments, 0, stdout=b"", stderr=b"")
        ),
    )
    assert task_agent.unlock_luks2("/dev/vda2", "mapping", expected, lambda: b"key") == (
        "/dev/mapper/mapping"
    )
    mapping, name = task_agent.format_and_unlock_luks2("/dev/vda2", expected, lambda: b"key")
    assert (mapping, name) == ("/dev/mapper/pyfog-luks1", "pyfog-luks1")
    assert all("key" not in arguments for arguments, _kwargs in calls)
    assert calls[0][1]["input"] == b"key"
    monkeypatch.setattr(
        task_agent,
        "run_command",
        lambda arguments, **_kwargs: calls.append(arguments) or "",
    )
    task_agent.close_luks2("mapping")
    assert calls[-1] == ["cryptsetup", "close", "mapping"]
    with pytest.raises(ValueError, match="mapping"):
        task_agent.unlock_luks2("/dev/vda2", "bad/name", expected, lambda: b"key")
    with pytest.raises(ValueError, match="clave"):
        task_agent._unlock_luks2_with_key("/dev/vda2", "mapping", b"")
    with pytest.raises(ValueError, match="mapping"):
        task_agent.close_luks2("bad/name")


def test_download_artifact_streams_and_reuses_verified_staging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = b"artifact-content"
    opener = FakeOpener(FakeResponse(payload))
    monkeypatch.setattr(task_agent, "opener", lambda _ca_file: opener)
    destination = tmp_path / "artifact.bin"
    checks = []
    task_agent.download_artifact(
        "https://coordinator.example/artifact",
        destination,
        token="token",
        ca_file=None,
        expected_size=len(payload),
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        chunk_bytes=4,
        check_cancel=lambda: checks.append(True),
    )
    assert destination.read_bytes() == payload
    assert checks
    count = len(checks)
    task_agent.download_artifact(
        "https://coordinator.example/artifact",
        destination,
        token="token",
        ca_file=None,
        expected_size=len(payload),
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        chunk_bytes=4,
        check_cancel=lambda: checks.append(True),
    )
    assert len(checks) == count

    opener.response = FakeResponse(payload + b"too much")
    with pytest.raises(ValueError, match="supera su tamaño"):
        task_agent.download_artifact(
            "https://coordinator.example/artifact",
            tmp_path / "too-large.bin",
            token="token",
            ca_file=None,
            expected_size=len(payload),
            expected_sha256="0" * 64,
            chunk_bytes=64,
            check_cancel=lambda: None,
        )


def test_lease_heartbeat_reports_cancellation_and_progress_increments_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    monkeypatch.setattr(
        task_agent,
        "json_request",
        lambda endpoint, **kwargs: calls.append((endpoint, kwargs)) or {"cancel_requested": True},
    )
    heartbeat = task_agent.LeaseHeartbeat("https://coordinator.test", "task", "token", None, 0)
    heartbeat._run()
    with pytest.raises(task_agent.TaskCancelledError, match="cancelar"):
        heartbeat.check()
    assert calls[0][0].endswith("/api/v1/tasks/task/heartbeat")

    monkeypatch.setattr(task_agent, "json_request", lambda *_args, **_kwargs: {})
    sequence = task_agent.post_progress(
        "https://coordinator.test",
        "task",
        "token",
        None,
        7,
        phase="uploading",
        processed=4,
        total=8,
        message="mitad",
    )
    assert sequence == 8


def test_clone_hostname_and_atomic_target_helpers_reject_escape_and_symlink(
    tmp_path: Path,
) -> None:
    assert task_agent.validate_clone_hostname("Clone-01.Example") == "clone-01.example"
    for hostname in ("", "bad..name", "-bad", "bad-", "a" * 64 + ".test"):
        with pytest.raises(ValueError, match="hostname"):
            task_agent.validate_clone_hostname(hostname)
    root = tmp_path / "root"
    root.mkdir()
    assert task_agent.safe_target_path(root, "etc/hostname") == root / "etc/hostname"
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="sale"):
        task_agent.safe_target_path(root, "escape/file")
    target = root / "etc/hostname"
    task_agent.atomic_text(target, "host\n", mode=0o640)
    assert target.read_text(encoding="utf-8") == "host\n"
    assert target.stat().st_mode & 0o777 == 0o640
    target.unlink()
    (outside / "host").write_text("outside", encoding="utf-8")
    target.symlink_to(outside / "host")
    with pytest.raises(ValueError, match="simbólico"):
        task_agent.atomic_text(target, "unsafe")


def test_restore_partition_and_mbr_artifact_paths_are_validated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = []
    monkeypatch.setattr(
        task_agent,
        "run_command",
        lambda arguments, **kwargs: calls.append((arguments, kwargs)) or "",
    )
    monkeypatch.setattr(task_agent.shutil, "which", lambda name: "/usr/bin/" + name)
    bios = {
        "mbr_disk_signature": "1a2b3c4d",
        "partitions": [{"number": 1, "role": "root", "start_sector": 2048, "size_sectors": 100}],
    }
    task_agent.restore_partition_table("/dev/vda", bios)
    assert calls[0][0][0] == "sfdisk"
    assert "label-id: 0x1a2b3c4d" in calls[0][1]["input_text"]

    artifact = tmp_path / "boot-sector.bin"
    artifact.write_bytes(b"x" * task_agent.MBR_BOOT_SECTOR_BYTES)
    device = tmp_path / "device"
    device.write_bytes(b"0" * 512)
    task_agent.write_mbr_boot_code(artifact, str(device))
    assert device.read_bytes()[: task_agent.MBR_BOOT_SECTOR_BYTES] == b"x" * 446
    artifact.write_bytes(b"short")
    with pytest.raises(ValueError, match="446"):
        task_agent.write_mbr_boot_code(artifact, str(device))


def test_command_version_and_capabilities_are_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        task_agent,
        "run_command",
        lambda *_args, **_kwargs: "partclone.ext4 v0.3.41",
    )
    assert task_agent.command_version() == "0.3.41"
    monkeypatch.setattr(task_agent, "run_command", lambda *_args, **_kwargs: "unknown")
    assert task_agent.command_version() == "0.3.45"
    monkeypatch.setattr(task_agent.shutil, "which", lambda _name: None)
    capabilities = task_agent.local_agent_capabilities()
    assert {"gpt", "mbr", "restore", "clone", "identity"}.issubset(capabilities)
    assert "capture.hot" not in capabilities
