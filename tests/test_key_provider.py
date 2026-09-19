import base64

import pytest

from pyfog.key_provider import KeyProviderError, decode_ephemeral_key


def test_external_key_decoder_is_bounded_and_never_accepts_plaintext():
    assert decode_ephemeral_key(base64.b64encode(b"key").decode()) == b"key"
    with pytest.raises(KeyProviderError):
        decode_ephemeral_key("plaintext")
    with pytest.raises(KeyProviderError):
        decode_ephemeral_key(base64.b64encode(b"x" * 4097).decode())
