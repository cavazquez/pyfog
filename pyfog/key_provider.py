"""External, ephemeral LUKS2 key-provider contract."""

from __future__ import annotations

import base64
import binascii
from typing import Literal, cast

from pydantic import Field

from pyfog.layouts import Luks2Layout
from pyfog.plugins import PluginCall, PluginError, PluginRunner
from pyfog.schemas import Schema


class KeyProviderError(RuntimeError):
    """A provider did not return an ephemeral key under the expected contract."""


class Luks2KeyRequest(Schema):
    api_version: int = Field(default=1, ge=1, le=1, strict=True)
    uuid: str = Field(min_length=1, max_length=128)
    cipher: str = Field(min_length=1, max_length=128)
    sector_size: Literal[512, 4096]
    key_ref: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9._:/-]+$")


def decode_ephemeral_key(value: object) -> bytes:
    """Decode a bounded provider response without accepting a key from image metadata."""

    if not isinstance(value, str) or len(value) > 8192:
        raise KeyProviderError("El proveedor no devolvió una clave codificada válida.")
    try:
        key = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise KeyProviderError("La clave del proveedor no es Base64 válido.") from None
    if not 1 <= len(key) <= 4096:
        raise KeyProviderError("La clave del proveedor tiene un tamaño inválido.")
    return key


def external_luks2_key(
    runner: PluginRunner,
    plugin_name: str,
    layout: Luks2Layout,
    *,
    key_ref: str,
) -> bytes:
    """Ask a provider plugin for one key; it is returned only to the cryptsetup stdin caller."""

    request = Luks2KeyRequest(
        uuid=layout.uuid,
        cipher=layout.cipher,
        sector_size=cast(Literal[512, 4096], layout.sector_size),
        key_ref=key_ref,
    )
    try:
        response = runner.run(
            plugin_name,
            PluginCall(
                operation="luks2.key",
                required_capability="luks2.key",
                payload=request.model_dump(mode="json"),
            ),
        )
    except PluginError as error:
        raise KeyProviderError(str(error)) from None
    if not response.success:
        raise KeyProviderError(response.error)
    return decode_ephemeral_key(response.result.get("key_b64"))
