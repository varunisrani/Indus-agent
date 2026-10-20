"""RS256 pieces shared by the adapters that check signed bearer tokens (Teams P16-11, Google Chat
P16-13).

Only the plumbing lives here: strict unpadded base64url, an RSA public key built from a JWK's
``n`` / ``e``, and one verify call. The signature check itself is ``cryptography``'s
RSASSA-PKCS1-v1_5 with SHA-256; no RSA arithmetic is written in this package (a guard test scans
``src/`` for it).
The algorithm allow-list, the claim checks and the key caches stay with each adapter, which knows
its issuer and its key source.

``cryptography`` comes with the base install (through ``mcp``'s ``pyjwt[crypto]``; also the
``google-chat`` extra) and is imported only when a key is built or used.
"""

from __future__ import annotations

import base64
import binascii
import functools
from dataclasses import dataclass
from typing import Any

__all__ = [
    "b64url_decode",
    "b64url_encode",
    "rs256_verify",
    "rsa_public_key",
]

_B64URL = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def b64url_encode(data: bytes) -> str:
    """Unpadded base64url."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    """Strict unpadded base64url (``ValueError`` when empty, padded, of another alphabet or of an
    impossible length)."""
    if not text or any(c not in _B64URL for c in text) or len(text) % 4 == 1:
        raise ValueError("not base64url")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        raise ValueError("not base64url") from None


@dataclass(frozen=True)
class _Primitives:
    invalid: type[Exception]
    sha256: Any
    pkcs1v15: Any
    numbers: Any
    public_key_type: type[Any]


@functools.cache
def _primitives() -> _Primitives:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    return _Primitives(
        InvalidSignature, hashes.SHA256, padding.PKCS1v15, rsa.RSAPublicNumbers, rsa.RSAPublicKey
    )


def rsa_public_key(n: int, e: int) -> Any:
    """``cryptography``'s RSA public key for a JWK's modulus and exponent (``ValueError`` when the
    library refuses the numbers)."""
    try:
        return _primitives().numbers(e, n).public_key()
    except (TypeError, ValueError) as exc:
        raise ValueError("unusable RSA public key") from exc


def rs256_verify(public_key: Any, signing_input: bytes, signature: bytes) -> bool:
    """True when ``signature`` is a valid RSASSA-PKCS1-v1_5 / SHA-256 signature of
    ``signing_input`` under ``public_key``; false for any other outcome (never raises for a bad
    signature)."""
    lib = _primitives()
    if not isinstance(public_key, lib.public_key_type):
        return False
    try:
        public_key.verify(signature, signing_input, lib.pkcs1v15(), lib.sha256())
    except (lib.invalid, ValueError, TypeError):
        return False
    return True
