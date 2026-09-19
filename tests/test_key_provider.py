import base64
from uuid import uuid4

import pytest

from pyfog.key_provider import KeyProviderError, decode_ephemeral_key, external_luks2_key
from pyfog.layouts import Luks2Layout
from pyfog.plugins import PluginError, PluginResponse


def test_external_key_decoder_is_bounded_and_never_accepts_plaintext():
    assert decode_ephemeral_key(base64.b64encode(b"key").decode()) == b"key"
    with pytest.raises(KeyProviderError):
        decode_ephemeral_key("plaintext")
    with pytest.raises(KeyProviderError):
        decode_ephemeral_key(base64.b64encode(b"x" * 4097).decode())
    with pytest.raises(KeyProviderError):
        decode_ephemeral_key(None)
    with pytest.raises(KeyProviderError):
        decode_ephemeral_key(base64.b64encode(b"").decode())


class FakeKeyProvider:
    def __init__(self, response=None, error=None) -> None:
        self.response = response
        self.error = error
        self.call = None

    def run(self, plugin_name, call):
        self.call = (plugin_name, call)
        if self.error is not None:
            raise self.error
        return self.response


def test_external_key_provider_uses_only_layout_metadata_and_returns_ephemeral_key():
    key = base64.b64encode(b"key").decode()
    runner = FakeKeyProvider(
        response=PluginResponse(request_id=uuid4(), success=True, result={"key_b64": key})
    )
    result = external_luks2_key(
        runner,
        "vault-provider",
        Luks2Layout("luks-1", "aes-xts-plain64", 4096),
        key_ref="vault/luks/luks-1",  # pragma: allowlist secret
    )
    assert result == b"key"
    plugin_name, call = runner.call
    assert plugin_name == "vault-provider"
    assert call.operation == "luks2.key"
    assert call.payload == {
        "api_version": 1,
        "uuid": "luks-1",
        "cipher": "aes-xts-plain64",
        "sector_size": 4096,
        "key_ref": "vault/luks/luks-1",
    }


def test_external_key_provider_translates_provider_failures():
    layout = Luks2Layout("luks-1", "aes", 512)
    failed = FakeKeyProvider(
        response=PluginResponse(request_id=uuid4(), success=False, error="unavailable")
    )
    with pytest.raises(KeyProviderError, match="unavailable"):
        external_luks2_key(failed, "provider", layout, key_ref="vault/luks")

    broken = FakeKeyProvider(error=PluginError("provider failed"))
    with pytest.raises(KeyProviderError, match="provider failed"):
        external_luks2_key(broken, "provider", layout, key_ref="vault/luks")
