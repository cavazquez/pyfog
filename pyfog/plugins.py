"""Versioned, least-privilege plugin protocol for optional integrations.

Plugins are deliberately process boundaries.  The coordinator sends one bounded JSON request on
stdin and receives one bounded JSON response on stdout; no plugin receives a web session, database
handle, or unrestricted environment.  A one-shot secret, when required by an integration, is
encoded in that stdin request and is never placed in argv, environment, or log messages.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import Field, field_validator, model_validator

from pyfog.schemas import Schema

PLUGIN_API_VERSION = 1
MAX_PLUGIN_INPUT_BYTES = 256 * 1024
MAX_PLUGIN_OUTPUT_BYTES = 256 * 1024
MAX_PLUGIN_TIMEOUT_SECONDS = 300
SECRET_KEY_NAMES = frozenset({"password", "token", "secret", "private_key", "credential"})


class PluginError(RuntimeError):
    """A plugin could not be loaded, authorized, or completed safely."""


class PluginDescriptor(Schema):
    """Signed-out-of-band descriptor for one executable plugin."""

    api_version: Literal[1] = 1
    name: str = Field(
        min_length=1,
        max_length=80,
        pattern=r"^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*$",
    )
    version: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]*$",
    )
    executable: list[str] = Field(min_length=1, max_length=16)
    capabilities: list[str] = Field(min_length=1, max_length=32)
    permissions: list[str] = Field(default_factory=list, max_length=32)
    environment: list[str] = Field(default_factory=list, max_length=16)
    timeout_seconds: int = Field(default=30, ge=1, le=MAX_PLUGIN_TIMEOUT_SECONDS, strict=True)
    max_input_bytes: int = Field(
        default=MAX_PLUGIN_INPUT_BYTES, gt=0, le=MAX_PLUGIN_INPUT_BYTES, strict=True
    )
    max_output_bytes: int = Field(
        default=MAX_PLUGIN_OUTPUT_BYTES, gt=0, le=MAX_PLUGIN_OUTPUT_BYTES, strict=True
    )

    @field_validator("executable")
    @classmethod
    def valid_executable(cls, value: list[str]) -> list[str]:
        if any(
            not item or "\x00" in item or "\n" in item or "\r" in item or len(item) > 4096
            for item in value
        ):
            raise ValueError("El comando del plugin contiene un argumento inválido.")
        return value

    @field_validator("capabilities", "permissions")
    @classmethod
    def valid_names(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(
            not item
            or len(item) > 80
            or not all(character.isalnum() or character in ".-_" for character in item)
            for item in value
        ):
            raise ValueError("Las capacidades y permisos del plugin no son válidos.")
        return sorted(value)

    @field_validator("environment")
    @classmethod
    def valid_environment(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(
            not item or not item.startswith("PYFOG_PLUGIN_") or not item.isidentifier()
            for item in value
        ):
            raise ValueError("El plugin sólo puede declarar variables PYFOG_PLUGIN_*.")
        return sorted(value)

    @model_validator(mode="after")
    def validate_capabilities(self) -> PluginDescriptor:
        if not self.capabilities:
            raise ValueError("El plugin debe declarar al menos una capacidad.")
        if any(permission not in self.capabilities for permission in self.permissions):
            raise ValueError("Un permiso del plugin debe estar cubierto por una capacidad.")
        return self


class PluginResponse(Schema):
    """Bounded response emitted by the plugin process."""

    api_version: Literal[1] = 1
    request_id: UUID
    success: bool = Field(strict=True)
    result: dict[str, Any] = Field(default_factory=dict)
    error: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def validate_result(self) -> PluginResponse:
        if self.success and self.error:
            raise ValueError("Una respuesta exitosa no puede contener un error.")
        if not self.success and not self.error:
            raise ValueError("Una respuesta fallida debe explicar el error.")
        _reject_secrets(self.result)
        return self


@dataclass(frozen=True)
class PluginCall:
    """Request data kept in memory by the coordinator, outside the wire schema."""

    operation: str
    payload: Mapping[str, Any]
    secret: bytes | None = None
    request_id: UUID = field(default_factory=uuid4)
    required_capability: str | None = None
    timeout_seconds: int | None = None


def _reject_secrets(value: object, *, path: str = "payload") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key).casefold()
            presence_flag = isinstance(item, bool) and (
                key_text.startswith(("has_", "contains_", "uses_", "requires_"))
                or key_text.endswith(("_present", "_available"))
            )
            if (
                key_text in SECRET_KEY_NAMES
                or any(
                    marker in key_text
                    for marker in ("password", "token", "credential", "private_key")
                )
                or ("secret" in key_text and not key_text.endswith("_ref") and not presence_flag)
            ):
                raise PluginError(f"El payload del plugin no puede contener {path}.{key}.")
            _reject_secrets(item, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secrets(item, path=f"{path}[{index}]")


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return (
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise PluginError(f"El payload del plugin no es JSON: {error}") from None


class PluginRegistry:
    """In-memory registry populated from an administrator-owned descriptor directory."""

    def __init__(self) -> None:
        self._descriptors: dict[str, PluginDescriptor] = {}

    def register(self, descriptor: PluginDescriptor) -> None:
        if descriptor.name in self._descriptors:
            raise PluginError(f"El plugin {descriptor.name} ya está registrado.")
        self._descriptors[descriptor.name] = descriptor

    def get(self, name: str) -> PluginDescriptor:
        try:
            return self._descriptors[name]
        except KeyError:
            raise PluginError(f"No existe el plugin {name}.") from None

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._descriptors))

    def load_file(self, path: Path) -> PluginDescriptor:
        if path.is_symlink() or not path.is_file():
            raise PluginError("El descriptor del plugin debe ser un archivo regular.")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            descriptor = PluginDescriptor.model_validate(value)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise PluginError(f"El descriptor del plugin es inválido: {error}") from None
        self.register(descriptor)
        return descriptor

    def load_directory(self, directory: Path) -> tuple[PluginDescriptor, ...]:
        if directory.is_symlink() or not directory.is_dir():
            raise PluginError("El directorio de plugins debe ser un directorio regular.")
        loaded = [self.load_file(path) for path in sorted(directory.glob("*.json"))]
        return tuple(loaded)


class PluginRunner:
    """Execute registered plugins through a bounded, non-shell subprocess boundary."""

    def __init__(
        self, registry: PluginRegistry, *, environment: Mapping[str, str] | None = None
    ) -> None:
        self.registry = registry
        self.environment = dict(environment or {})

    def run(self, plugin_name: str, call: PluginCall) -> PluginResponse:
        descriptor = self.registry.get(plugin_name)
        if (
            not call.operation
            or len(call.operation) > 80
            or not all(character.isalnum() or character in ".-_" for character in call.operation)
        ):
            raise PluginError("La operación del plugin no es válida.")
        _reject_secrets(call.payload)
        if call.required_capability and call.required_capability not in descriptor.capabilities:
            raise PluginError(f"El plugin {plugin_name} no declara la capacidad requerida.")
        if call.required_capability and call.required_capability not in descriptor.permissions:
            raise PluginError(f"La capacidad {call.required_capability} no fue habilitada.")
        if (
            call.timeout_seconds is not None
            and not 1 <= call.timeout_seconds <= descriptor.timeout_seconds
        ):
            raise PluginError("El timeout solicitado excede el límite del descriptor.")
        wire: dict[str, Any] = {
            "api_version": PLUGIN_API_VERSION,
            "request_id": str(call.request_id),
            "operation": call.operation,
            "payload": dict(call.payload),
        }
        if call.secret is not None:
            if not isinstance(call.secret, bytes) or not 1 <= len(call.secret) <= 16 * 1024:
                raise PluginError("El secreto del plugin no tiene un tamaño permitido.")
            wire["secret_b64"] = base64.b64encode(call.secret).decode("ascii")
        encoded = _json_bytes(wire)
        if len(encoded) > descriptor.max_input_bytes:
            raise PluginError("La solicitud del plugin supera el límite de entrada.")
        command = self._resolve_command(descriptor)
        env = {"PATH": os.environ.get("PATH", ""), "LANG": "C", "LC_ALL": "C"}
        for variable in descriptor.environment:
            if variable in self.environment:
                env[variable] = self.environment[variable]
        try:
            completed = subprocess.run(  # noqa: S603 - argv is descriptor-owned and shell is disabled
                command,
                input=encoded,
                capture_output=True,
                check=False,
                shell=False,
                env=env,
                timeout=call.timeout_seconds or descriptor.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise PluginError(
                f"El plugin {plugin_name} no terminó dentro del límite: {error}"
            ) from None
        if len(completed.stdout) > descriptor.max_output_bytes:
            raise PluginError("La respuesta del plugin supera el límite de salida.")
        if completed.returncode != 0:
            raise PluginError(f"El plugin {plugin_name} terminó con código {completed.returncode}.")
        try:
            value = json.loads(completed.stdout)
            response = PluginResponse.model_validate(value)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise PluginError(
                f"El plugin {plugin_name} devolvió un contrato inválido: {error}"
            ) from None
        if response.request_id != call.request_id:
            raise PluginError("La respuesta del plugin no corresponde a la solicitud.")
        return response

    @staticmethod
    def _resolve_command(descriptor: PluginDescriptor) -> list[str]:
        executable = descriptor.executable[0]
        if "/" in executable:
            path = Path(executable)
            try:
                resolved_path = path.resolve(strict=True)
            except OSError:
                resolved_path = path
            if not resolved_path.is_file() or not os.access(resolved_path, os.X_OK):
                raise PluginError("El ejecutable del plugin no es un archivo ejecutable seguro.")
            resolved = str(resolved_path)
        else:
            resolved = shutil.which(executable) or ""
            if not resolved:
                raise PluginError(f"No se encontró el ejecutable del plugin: {executable}.")
        return [resolved, *descriptor.executable[1:]]


def request_id() -> UUID:
    """Generate a request ID without exposing host or task identifiers."""

    return uuid4()


def redact_plugin_message(message: str) -> str:
    """Return a bounded message suitable for task events and audit records."""

    text = " ".join(message.split())[:500]
    for marker in ("secret", "token", "password", "credential"):
        if marker in text.casefold():
            return f"Plugin error ({marker} redacted)"
    return text or "Plugin error"


def random_plugin_nonce() -> str:
    """Return a non-secret correlation nonce for integrations that need one."""

    return secrets.token_urlsafe(18)
