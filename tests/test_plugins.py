import sys
from pathlib import Path

import pytest

from pyfog.domain import DomainJoinRequest, domain_plugin_call
from pyfog.hooks import HookError, HookLedger, HookPlan, run_post_deploy_hooks
from pyfog.plugins import (
    PluginCall,
    PluginDescriptor,
    PluginError,
    PluginRegistry,
    PluginRunner,
)


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
