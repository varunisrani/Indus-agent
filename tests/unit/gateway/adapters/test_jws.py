"""The shared RS256 helper (Teams P16-11, Google Chat P16-13): cryptography does the RSA, and no
RSA arithmetic of our own exists anywhere in ``src/``."""

from __future__ import annotations

import hashlib
import hmac
import re
from pathlib import Path
from typing import Any

import pytest

import indus
from indus.gateway.adapters._jws import b64url_decode, b64url_encode, rs256_verify, rsa_public_key
from tests.fakes.platform.teams_wire import rsa_private_key

SRC = Path(indus.__file__).resolve().parent


def _sign(key: Any, data: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    return key.sign(data, padding.PKCS1v15(), hashes.SHA256())


def _public(key: Any) -> object:
    numbers = key.public_key().public_numbers()
    return rsa_public_key(numbers.n, numbers.e)


def test_verification_is_pkcs1v15_sha256_and_never_raises_for_a_bad_signature() -> None:
    signer = rsa_private_key(2048, 0)
    other = rsa_private_key(2048, 1)
    public = _public(signer)
    message = b"header.payload"
    signature = _sign(signer, message)
    assert rs256_verify(public, message, signature)
    assert not rs256_verify(public, message + b"!", signature)
    assert not rs256_verify(public, message, signature[:-1])
    assert not rs256_verify(public, message, b"\x00" + signature)
    assert not rs256_verify(public, message, _sign(other, message))
    # Key confusion: an HMAC over the input keyed with the public numbers is not a signature.
    mac = hmac.new(str(public).encode(), message, hashlib.sha256).digest()
    assert not rs256_verify(public, message, mac)
    # Anything that is not an RSA public key verifies nothing.
    for key in (None, b"secret", "key", signer):
        assert not rs256_verify(key, message, signature)


def test_unusable_key_numbers_are_refused() -> None:
    numbers = rsa_private_key(2048, 0).public_key().public_numbers()
    with pytest.raises(ValueError):
        rsa_public_key(numbers.n, 4)  # an even exponent
    with pytest.raises(ValueError):
        rsa_public_key(numbers.n, numbers.n + 2)  # exponent above the modulus


def test_base64url_is_strict() -> None:
    assert b64url_decode(b64url_encode(b"\xff\xfe")) == b"\xff\xfe"
    assert b64url_encode(b"hi") == "aGk"
    for bad in ("", "a", "aGk=", "a+b/", "a b", "é"):
        with pytest.raises(ValueError):
            b64url_decode(bad)


_MODEXP = re.compile(r"\bpow\(\s*[^,()]+,\s*[^,()]+,\s*[^,()]+\)")
_DIGEST_INFO = re.compile(r"3031300d0609608648016503040201", re.IGNORECASE)


def test_no_rsa_arithmetic_of_our_own_in_src() -> None:
    offenders = [
        f"{path.relative_to(SRC)}:{number}"
        for path in sorted(SRC.rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _MODEXP.search(line) or _DIGEST_INFO.search(line)
    ]
    assert offenders == [], f"modular exponentiation / hand-built DigestInfo in src: {offenders}"
