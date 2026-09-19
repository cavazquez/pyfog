from uuid import uuid4

import pytest

from pyfog.domain import (
    DomainIntegrationError,
    DomainJoinRequest,
    domain_plugin_call,
    domain_plugin_contract,
    join_domain,
)
from pyfog.plugins import PluginResponse


def request() -> DomainJoinRequest:
    return DomainJoinRequest(
        host_id=uuid4(),
        domain="example.test",
        organizational_unit="OU=Linux,DC=example,DC=test",
        hostname="linux-01",
        dns_servers=["192.0.2.53"],
        secret_ref="vault/domain/example",  # pragma: allowlist secret
        idempotency_key="task/domain-1",
    )


class FakeRunner:
    def __init__(self, response=None, error=None) -> None:
        self.response = response
        self.error = error
        self.call = None

    def run(self, plugin_name, call):
        self.call = (plugin_name, call)
        if self.error is not None:
            raise self.error
        return self.response


def test_domain_join_keeps_secret_out_of_payload_and_bounds_plugin_message():
    domain_request = request()
    call = domain_plugin_call(domain_request, secret=b"one-shot-secret")
    assert call.operation == "domain.join"
    assert call.secret == b"one-shot-secret"
    assert "secret_ref" not in call.payload

    response = PluginResponse(
        request_id=uuid4(),
        success=True,
        result={"request_id": "plugin-request"},
    )
    runner = FakeRunner(response=response)
    result = join_domain(runner, "domain-plugin", domain_request, secret=b"secret")

    assert result.success is True
    assert result.plugin == "domain-plugin"
    assert result.message == "Unión de dominio completada."
    assert result.details == {"request_id": "plugin-request"}
    assert runner.call[0] == "domain-plugin"


@pytest.mark.parametrize("error", [DomainIntegrationError("invalid"), RuntimeError("offline")])
def test_domain_join_translates_plugin_failures(error):
    with pytest.raises(DomainIntegrationError, match=str(error)):
        join_domain(FakeRunner(error=error), "domain-plugin", request(), secret=b"secret")

    with pytest.raises(DomainIntegrationError, match="proveedor seguro"):
        domain_plugin_call(request(), secret=None)


def test_domain_join_returns_failure_without_exposing_a_secret():
    response = PluginResponse(request_id=uuid4(), success=False, error="plugin refused")
    result = join_domain(
        FakeRunner(response=response), "domain-plugin", request(), secret=b"secret"
    )
    assert result.success is False
    assert result.message == "plugin refused"


def test_domain_contract_and_dns_validation_are_explicit():
    contract = domain_plugin_contract()
    assert contract["capability"] == "domain.join"
    assert "domain.rollback" in contract["operations"]

    with pytest.raises(ValueError, match="servidores DNS"):
        DomainJoinRequest.model_validate({**request().model_dump(), "dns_servers": [""]})
