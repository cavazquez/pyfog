"""Optional domain integration boundary implemented as a least-privilege plugin."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from pydantic import Field, field_validator

from pyfog.plugins import PluginCall, PluginResponse, PluginRunner
from pyfog.schemas import Schema

MAX_DNS_NAME_LENGTH = 253


class DomainIntegrationError(ValueError):
    """A domain request is invalid or the selected integration is unavailable."""


class DomainJoinRequest(Schema):
    """Secret-free AD/LDAP join intent passed to a domain plugin."""

    host_id: UUID
    domain: str = Field(min_length=1, max_length=MAX_DNS_NAME_LENGTH, pattern=r"^[A-Za-z0-9.-]+$")
    organizational_unit: str = Field(default="", max_length=255, pattern=r"^[A-Za-z0-9=, ._/-]*$")
    hostname: str = Field(
        min_length=1,
        max_length=MAX_DNS_NAME_LENGTH,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9.-]*$",
    )
    dns_servers: list[str] = Field(default_factory=list, max_length=8)
    secret_ref: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9._:/-]+$")
    idempotency_key: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9._:/-]+$")

    @field_validator("dns_servers")
    @classmethod
    def validate_dns_servers(cls, value: list[str]) -> list[str]:
        if any(len(server) > MAX_DNS_NAME_LENGTH or not server for server in value):
            raise ValueError("Los servidores DNS del dominio no son válidos.")
        return value


@dataclass(frozen=True)
class DomainJoinResult:
    success: bool
    plugin: str
    message: str
    details: dict[str, object]


def domain_plugin_call(request: DomainJoinRequest, *, secret: bytes | None) -> PluginCall:
    """Build a domain plugin call without placing the credential in JSON payload fields."""

    if secret is None:
        raise DomainIntegrationError("El secreto de unión debe provenir del proveedor seguro.")
    return PluginCall(
        operation="domain.join",
        required_capability="domain.join",
        secret=secret,
        payload=request.model_dump(mode="json", exclude={"secret_ref"}),
    )


def join_domain(
    runner: PluginRunner,
    plugin_name: str,
    request: DomainJoinRequest,
    *,
    secret: bytes | None,
) -> DomainJoinResult:
    """Run the configured domain plugin and return only bounded, secret-free details."""

    try:
        response: PluginResponse = runner.run(
            plugin_name,
            domain_plugin_call(request, secret=secret),
        )
    except (DomainIntegrationError, RuntimeError) as error:
        raise DomainIntegrationError(str(error)) from None
    message = response.error if not response.success else "Unión de dominio completada."
    return DomainJoinResult(response.success, plugin_name, message[:500], response.result)


def domain_plugin_contract() -> dict[str, object]:
    """Describe the stable capability expected by #72."""

    return {
        "api_version": 1,
        "capability": "domain.join",
        "operations": ["domain.join", "domain.leave", "domain.rollback"],
        "input": "secret-free intent plus one stdin-only provider secret",
        "output": "bounded JSON response with request_id correlation",
        "side_effects": "must be idempotent by idempotency_key and expose rollback",
        "network": "plugin-owned and explicitly allowlisted outside the coordinator",
    }
