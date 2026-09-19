import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from pyfog import plugins
from pyfog.domain import DomainJoinRequest, domain_plugin_call
from pyfog.hooks import HookError, HookLedger, HookPlan, run_post_deploy_hooks
from pyfog.plugins import (
    PluginCall,
    PluginDescriptor,
    PluginError,
    PluginRegistry,
    PluginResponse,
    PluginRunner,
    random_plugin_nonce,
    redact_plugin_message,
    request_id,
)

MIN_PLUGIN_NONCE_LENGTH = 10


def make_descriptor(**overrides):
    values = {
        "name": "test-plugin",
        "version": "1",
        "executable": ["plugin"],
        "capabilities": ["test.run"],
        "permissions": ["test.run"],
    }
    values.update(overrides)
    return PluginDescriptor.model_validate(values)


def make_plugin(tmp_path: Path) -> PluginRunner:
    script = tmp_path / "plugin.py"
    script.write_text(
        "import base64, json, sys\n"
        "request = json.load(sys.stdin)\n"
        "if request['operation'] == 'fail':\n"
        "    response = {'api_version': 1, 'request_id': request['request_id'], "
        "'success': False, 'error': 'failed'}\n"
        "    print(json.dumps(response))\n"
        "else:\n"
        "    result = {'operation': request['operation'], 'has_secret': 'secret_b64' in request}\n"
        "    response = {'api_version': 1, 'request_id': request['request_id'], "
        "'success': True, 'result': result}\n"
        "    print(json.dumps(response))\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    registry = PluginRegistry()
    registry.register(
        PluginDescriptor(
            name="test-plugin",
            version="1",
            executable=[sys.executable, str(script)],
            capabilities=["test.run", "test.rollback"],
            permissions=["test.run", "test.rollback"],
        )
    )
    return PluginRunner(registry)


def test_plugin_protocol_uses_stdin_for_secret_and_validates_capability(tmp_path):
    runner = make_plugin(tmp_path)
    response = runner.run(
        "test-plugin",
        PluginCall(
            operation="run",
            payload={"host_id": "host-1"},
            secret=b"do-not-log",
            required_capability="test.run",
        ),
    )

    assert response.success is True
    assert response.result == {"operation": "run", "has_secret": True}
    with pytest.raises(PluginError, match="payload"):
        runner.run("test-plugin", PluginCall(operation="run", payload={"password": "x"}))


def test_hooks_retry_idempotently_and_compensate(tmp_path):
    runner = make_plugin(tmp_path)
    plan = HookPlan(
        name="deploy",
        steps=[
            {
                "name": "prepare",
                "plugin": "test-plugin",
                "operation": "prepare",
                "capability": "test.run",
                "idempotency_key": "task/prepare",
                "rollback_plugin": "test-plugin",
                "rollback_operation": "rollback",
                "rollback_capability": "test.rollback",
            },
            {
                "name": "fail",
                "plugin": "test-plugin",
                "operation": "fail",
                "capability": "test.run",
                "idempotency_key": "task/fail",
            },
        ],
    )
    ledger = HookLedger()
    with pytest.raises(HookError, match="rollback"):
        run_post_deploy_hooks(plan, runner, context={"task_id": "task"}, ledger=ledger)
    assert ledger.completed_keys == {"task/prepare"}

    only_prepare = plan.model_copy(update={"steps": [plan.steps[0]]})
    result = run_post_deploy_hooks(only_prepare, runner, context={"task_id": "task"}, ledger=ledger)
    assert result.completed == ()


def test_domain_call_excludes_secret_reference_from_payload():
    request = DomainJoinRequest(
        host_id="5d4fbe67-28de-4e0c-b4a9-4f9cead8aa10",
        domain="example.test",
        hostname="pc-01",
        secret_ref="vault/domain/example",  # pragma: allowlist secret
        idempotency_key="task/domain",
    )
    call = domain_plugin_call(request, secret=b"password")

    assert "secret_ref" not in call.payload
    assert call.secret == b"password"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("executable", ["bad\nargument"]),
        ("capabilities", ["test.run", "test.run"]),
        ("capabilities", ["not valid"]),
        ("environment", ["PATH"]),
        ("environment", ["PYFOG_PLUGIN_BAD-VAR"]),
    ],
)
def test_plugin_descriptor_rejects_unsafe_metadata(field, value):
    with pytest.raises(ValueError, match=r"."):
        make_descriptor(**{field: value})


def test_plugin_descriptor_and_response_contracts_reject_inconsistent_values():
    with pytest.raises(ValueError, match="cubierto"):
        make_descriptor(permissions=["test.rollback"])
    with pytest.raises(ValueError, match="respuesta exitosa"):
        PluginResponse(request_id=uuid4(), success=True, error="unexpected")
    with pytest.raises(ValueError, match="respuesta fallida"):
        PluginResponse(request_id=uuid4(), success=False)
    with pytest.raises(PluginError, match="payload"):
        PluginResponse(
            request_id=uuid4(),
            success=True,
            result={"nested": [{"private_key": "secret"}]},  # pragma: allowlist secret
        )
    with pytest.raises(PluginError, match="JSON"):
        plugins._json_bytes({"not_serializable": object()})


def test_plugin_registry_loads_regular_descriptors_and_rejects_duplicates(tmp_path):
    registry = PluginRegistry()
    descriptor_path = tmp_path / "plugin.json"
    descriptor_path.write_text(make_descriptor().model_dump_json(), encoding="utf-8")

    loaded = registry.load_file(descriptor_path)

    assert loaded.name == "test-plugin"
    assert registry.names() == ("test-plugin",)
    assert registry.get("test-plugin") == loaded
    with pytest.raises(PluginError, match="ya está registrado"):
        registry.load_file(descriptor_path)
    with pytest.raises(PluginError, match="No existe"):
        registry.get("missing")

    plugin_directory = tmp_path / "plugins"
    plugin_directory.mkdir()
    second_path = plugin_directory / "second.json"
    second_path.write_text(
        make_descriptor(name="second-plugin").model_dump_json(), encoding="utf-8"
    )
    directory_registry = PluginRegistry()
    assert [item.name for item in directory_registry.load_directory(plugin_directory)] == [
        "second-plugin"
    ]

    invalid_path = tmp_path / "invalid.json"
    invalid_path.write_text("not json", encoding="utf-8")
    with pytest.raises(PluginError, match=r"descriptor.*inválido"):
        registry.load_file(invalid_path)
    with pytest.raises(PluginError, match="archivo regular"):
        registry.load_file(tmp_path / "missing.json")
    symlink = tmp_path / "plugin-link.json"
    symlink.symlink_to(descriptor_path)
    with pytest.raises(PluginError, match="archivo regular"):
        registry.load_file(symlink)
    with pytest.raises(PluginError, match="directorio regular"):
        registry.load_directory(tmp_path / "missing-directory")


def test_plugin_runner_sends_bounded_wire_and_declared_environment(monkeypatch):
    request = uuid4()
    descriptor = make_descriptor(environment=["PYFOG_PLUGIN_TOKEN"])
    registry = PluginRegistry()
    registry.register(descriptor)
    runner = PluginRunner(registry, environment={"PYFOG_PLUGIN_TOKEN": "allowed"})
    captured = {}
    monkeypatch.setattr(plugins.shutil, "which", lambda _name: "/usr/bin/plugin")

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        response = {
            "api_version": 1,
            "request_id": str(request),
            "success": True,
            "result": {"has_secret": True},
        }
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps(response).encode(), stderr=b""
        )

    monkeypatch.setattr(plugins.subprocess, "run", fake_run)
    result = runner.run(
        "test-plugin",
        PluginCall(
            operation="run",
            payload={
                "nested": [
                    {
                        "has_secret": True,  # pragma: allowlist secret
                        "secret_ref": "vault/ref",  # pragma: allowlist secret
                    }
                ]
            },
            secret=b"one-shot",
            request_id=request,
            required_capability="test.run",
        ),
    )

    wire = json.loads(captured["kwargs"]["input"])
    assert result.success is True
    assert captured["command"] == ["/usr/bin/plugin"]
    assert wire["request_id"] == str(request)
    assert wire["secret_b64"]
    assert captured["kwargs"]["env"]["PYFOG_PLUGIN_TOKEN"] == "allowed"
    assert "OTHER" not in captured["kwargs"]["env"]

    missing_environment = PluginRunner(registry)
    missing_environment.run(
        "test-plugin",
        PluginCall(operation="run", payload={}, request_id=request),
    )
    assert "PYFOG_PLUGIN_TOKEN" not in captured["kwargs"]["env"]


def test_plugin_runner_rejects_invalid_calls_before_subprocess():
    registry = PluginRegistry()
    registry.register(make_descriptor())
    runner = PluginRunner(registry)

    with pytest.raises(PluginError, match="operación"):
        runner.run("test-plugin", PluginCall(operation="", payload={}))
    with pytest.raises(PluginError, match="capacidad requerida"):
        runner.run(
            "test-plugin",
            PluginCall(operation="run", payload={}, required_capability="missing"),
        )

    restricted = PluginRegistry()
    restricted.register(make_descriptor(permissions=[]))
    with pytest.raises(PluginError, match="no fue habilitada"):
        PluginRunner(restricted).run(
            "test-plugin",
            PluginCall(operation="run", payload={}, required_capability="test.run"),
        )
    with pytest.raises(PluginError, match="timeout"):
        runner.run("test-plugin", PluginCall(operation="run", payload={}, timeout_seconds=31))
    with pytest.raises(PluginError, match="secreto"):
        runner.run("test-plugin", PluginCall(operation="run", payload={}, secret=b""))

    small_input = PluginRegistry()
    small_input.register(make_descriptor(max_input_bytes=1))
    with pytest.raises(PluginError, match="entrada"):
        PluginRunner(small_input).run(
            "test-plugin", PluginCall(operation="run", payload={"too": "large"})
        )


def test_plugin_runner_wraps_process_and_response_failures(monkeypatch):
    request = uuid4()
    descriptor = make_descriptor()
    registry = PluginRegistry()
    registry.register(descriptor)
    runner = PluginRunner(registry)
    monkeypatch.setattr(plugins.shutil, "which", lambda _name: "/usr/bin/plugin")

    def set_result(stdout=b"{}", returncode=0):
        def fake_run(command, **_kwargs):
            return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr=b"")

        monkeypatch.setattr(plugins.subprocess, "run", fake_run)

    def raise_oserror(*_args, **_kwargs):
        raise OSError

    monkeypatch.setattr(plugins.subprocess, "run", raise_oserror)
    with pytest.raises(PluginError, match="no terminó"):
        runner.run("test-plugin", PluginCall(operation="run", payload={}))

    def raise_timeout(*_args, **_kwargs):
        command = "plugin"
        raise subprocess.TimeoutExpired(command, 1)

    monkeypatch.setattr(plugins.subprocess, "run", raise_timeout)
    with pytest.raises(PluginError, match="no terminó"):
        runner.run("test-plugin", PluginCall(operation="run", payload={}))

    set_result(stdout=b"x" * (descriptor.max_output_bytes + 1))
    with pytest.raises(PluginError, match="salida"):
        runner.run("test-plugin", PluginCall(operation="run", payload={}))
    set_result(stdout=b"{}", returncode=7)
    with pytest.raises(PluginError, match="código 7"):
        runner.run("test-plugin", PluginCall(operation="run", payload={}))
    set_result(stdout=b"not json")
    with pytest.raises(PluginError, match="contrato inválido"):
        runner.run("test-plugin", PluginCall(operation="run", payload={}))
    set_result(
        stdout=json.dumps(
            {
                "api_version": 1,
                "request_id": str(uuid4()),
                "success": True,
                "result": {},
            }
        ).encode()
    )
    with pytest.raises(PluginError, match="no corresponde"):
        runner.run("test-plugin", PluginCall(operation="run", payload={}, request_id=request))


def test_plugin_command_resolution_and_message_helpers(tmp_path, monkeypatch):
    missing = tmp_path / "missing-plugin"
    with pytest.raises(PluginError, match="ejecutable"):
        PluginRunner._resolve_command(make_descriptor(executable=[str(missing)]))

    executable = tmp_path / "plugin"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    with pytest.raises(PluginError, match="ejecutable"):
        PluginRunner._resolve_command(make_descriptor(executable=[str(executable)]))
    executable.chmod(0o700)
    assert PluginRunner._resolve_command(make_descriptor(executable=[str(executable)])) == [
        str(executable),
    ]

    monkeypatch.setattr(plugins.shutil, "which", lambda _name: None)
    with pytest.raises(PluginError, match="No se encontró"):
        PluginRunner._resolve_command(make_descriptor())
    assert isinstance(request_id(), type(uuid4()))
    assert len(random_plugin_nonce()) > MIN_PLUGIN_NONCE_LENGTH
    assert redact_plugin_message("token=secret") == "Plugin error (secret redacted)"
    assert redact_plugin_message("  plain   message ") == "plain message"
    assert redact_plugin_message("   ") == "Plugin error"
