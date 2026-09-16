import json
import ssl
import stat
import subprocess
import uuid
from unittest.mock import Mock

import pytest

from pyfog.schemas import Inventory
from scripts import collect_inventory as collector


def test_linux_collector_produces_valid_partial_inventory(tmp_path, monkeypatch):
    proc, sysfs = tmp_path / "proc", tmp_path / "sys"
    proc.mkdir()
    (proc / "cpuinfo").write_text("model name : Test CPU\n")
    (proc / "meminfo").write_text("MemTotal: 8192000 kB\n")
    interface = sysfs / "class/net/eth0"
    interface.mkdir(parents=True)
    (interface / "address").write_text("52:54:00:12:34:56\n")
    (interface / "operstate").write_text("up\n")
    loopback = sysfs / "class/net/lo"
    loopback.mkdir()
    (loopback / "address").write_text("00:00:00:00:00:00\n")
    monkeypatch.setattr(collector.platform, "system", lambda: "Linux")
    monkeypatch.setattr(collector.shutil, "which", lambda name: None)
    inventory = Inventory.model_validate(collector.collect(proc, sysfs))
    assert inventory.cpu.model == "Test CPU"
    assert inventory.memory.total_bytes == 8192000 * 1024
    assert len(inventory.interfaces) == 1
    assert inventory.disks == []
    assert any("lsblk" in warning for warning in inventory.warnings)
    assert inventory.system.serial_number == ""


def test_disks_are_parsed_without_changing_devices(monkeypatch):
    data = {
        "blockdevices": [
            {"name": "vda", "type": "disk", "size": "1048576", "log-sec": 512, "rm": "0"},
            {"name": "loop0", "type": "loop", "size": 4096},
        ]
    }
    run = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(data)))
    monkeypatch.setattr(collector.subprocess, "run", run)
    monkeypatch.setattr(collector.shutil, "which", lambda name: "/usr/bin/lsblk")
    result = collector.disk_inventory([])
    assert len(result) == 1
    assert result[0]["name"] == "/dev/vda"
    assert result[0]["removable"] is False
    args, kwargs = run.call_args
    assert args[0][0] == "/usr/bin/lsblk"
    assert "shell" not in kwargs
    assert kwargs["timeout"] == 15


@pytest.mark.parametrize(
    "url",
    [
        "http://192.0.2.1",
        "file:///etc/passwd",
        "https://u:p@example.com",
        "https://example.com/path",
    ],
)
def test_collector_refuses_unsafe_destinations(url):
    with pytest.raises(ValueError, match="URL base HTTPS"):
        collector.send_inventory(b"{}", url, str(uuid.uuid4()), "test-token", None)


def test_collector_disables_redirects(monkeypatch):
    seen_handlers = []

    def opener(*handlers):
        seen_handlers.extend(handlers)
        response = Mock()
        response.__enter__ = Mock(return_value=Mock(status=201))
        response.__exit__ = Mock(return_value=False)
        return Mock(open=Mock(return_value=response))

    monkeypatch.setattr(collector.urllib.request, "build_opener", opener)
    collector.send_inventory(b"{}", "https://pyfog.example", str(uuid.uuid4()), "test-token", None)
    assert any(isinstance(handler, collector.NoRedirect) for handler in seen_handlers)


def test_collector_keeps_hostname_and_certificate_validation_enabled(monkeypatch):
    contexts = []
    create_context = collector.ssl.create_default_context

    def secure_context(*, cafile=None):
        context = create_context(cafile=cafile)
        contexts.append(context)
        return context

    response = Mock()
    response.__enter__ = Mock(return_value=Mock(status=201))
    response.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(collector.ssl, "create_default_context", secure_context)
    monkeypatch.setattr(
        collector.urllib.request,
        "build_opener",
        lambda *handlers: Mock(open=Mock(return_value=response)),
    )

    collector.send_inventory(b"{}", "https://pyfog.example", str(uuid.uuid4()), "test-token", None)

    assert contexts[0].check_hostname is True
    assert contexts[0].verify_mode == ssl.CERT_REQUIRED


def test_pairing_waits_for_approval_and_submits_one_inventory(monkeypatch, inventory, capsys):
    request_id = str(uuid.uuid4())
    host_id = str(uuid.uuid4())
    responses = iter(
        [
            {"request_id": request_id, "poll_token": "poll-capability"},
            {"status": "pending"},
            {"status": "approved", "host_id": host_id, "inventory_token": "inventory-capability"},
            {"host_id": host_id, "report_id": inventory["report_id"], "created": True},
        ]
    )
    calls = []

    def fake_request(endpoint, **kwargs):
        calls.append((endpoint, kwargs))
        return next(responses)

    monkeypatch.setattr(collector, "json_request", fake_request)
    monkeypatch.setattr(collector.time, "sleep", lambda _: None)
    payload = json.dumps(inventory).encode()
    collector.pair_inventory(
        payload,
        inventory,
        "https://pyfog.example",
        None,
        timeout_seconds=10,
        interval_seconds=0.01,
    )
    output = capsys.readouterr().err
    assert "Solicitud PXE creada. Desafío:" in output
    assert "poll-capability" not in output
    assert calls[0][0] == "https://pyfog.example/api/v1/pairing/requests"
    request_body = json.loads(calls[0][1]["payload"])
    assert request_body["mac_address"] == inventory["interfaces"][0]["mac_address"]
    assert calls[-1][0].endswith(f"/pairing/requests/{request_id}/inventory")
    assert calls[-1][1]["token"] == "inventory-capability"
    assert calls[-1][1]["payload"] == payload


@pytest.mark.parametrize(
    ("state", "message"),
    [("rejected", "rechazada"), ("expired", "venció")],
)
def test_pairing_stops_on_terminal_state(monkeypatch, inventory, state, message):
    responses = iter(
        [
            {"request_id": str(uuid.uuid4()), "poll_token": "poll-capability"},
            {"status": state, "reason": "No reconocido"},
        ]
    )
    monkeypatch.setattr(collector, "json_request", lambda *args, **kwargs: next(responses))
    with pytest.raises(ValueError, match=message):
        collector.pair_inventory(
            json.dumps(inventory).encode(), inventory, "https://pyfog.example", None
        )


def test_output_file_is_private_and_cannot_overwrite_existing_file(
    tmp_path, monkeypatch, inventory
):
    output = tmp_path / "report.json"
    monkeypatch.setattr(collector, "collect", lambda: inventory)
    monkeypatch.setattr(collector.sys, "argv", ["collect_inventory", "--output", str(output)])
    collector.main()
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    original = output.read_bytes()
    with pytest.raises(SystemExit, match="1"):
        collector.main()
    assert output.read_bytes() == original


def test_collector_refuses_non_linux(monkeypatch):
    monkeypatch.setattr(collector.platform, "system", lambda: "Darwin")
    with pytest.raises(ValueError, match="solo admite Linux"):
        collector.collect()
