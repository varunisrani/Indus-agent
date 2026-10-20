"""Webhook body decryption and request signatures for Feishu / Lark (P16-14).

With an encrypt key set on the app, Feishu sends ``{"encrypt": "<base64>"}`` bodies: AES-256-CBC
with the SHA-256 digest of the encrypt key as the key, the first 16 bytes of the decoded text as
the IV and PKCS#7 padding. The cipher work is done by ``cryptography`` (no hand-written AES).

Each request is also signed: ``X-Lark-Signature`` is the hex SHA-256 digest of the request
timestamp, nonce, encrypt key and raw body, concatenated. It is checked on the raw body, in
constant time, before anything is parsed.
"""

from __future__ import annotations

import base64
import binascii
import hashlib

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from indus.gateway.adapters._http import constant_time_equal

__all__ = ["DecryptError", "decrypt", "signature", "verify_signature"]

_BLOCK = 16
# A webhook body is capped at 1 MiB by the listener; the cipher text can be no larger.
_MAX_CIPHER_CHARS = 2 * 1024 * 1024


class DecryptError(ValueError):
    """An ``encrypt`` value that does not decrypt with the configured key."""


def _key(encrypt_key: str) -> bytes:
    return hashlib.sha256(encrypt_key.encode("utf-8")).digest()


def decrypt(encrypt_key: str, encrypted: str) -> bytes:
    """The plain bytes of an ``encrypt`` value; `DecryptError` for anything malformed."""
    if not encrypt_key:
        raise DecryptError("no encrypt key is configured")
    if not encrypted or len(encrypted) > _MAX_CIPHER_CHARS:
        raise DecryptError("the encrypted value is empty or too large")
    try:
        raw = base64.b64decode(encrypted, validate=True)
    except (binascii.Error, ValueError):
        raise DecryptError("the encrypted value is not base64") from None
    if len(raw) < 2 * _BLOCK or len(raw) % _BLOCK:
        raise DecryptError("the encrypted value has a wrong length")
    iv, body = raw[:_BLOCK], raw[_BLOCK:]
    decryptor = Cipher(algorithms.AES(_key(encrypt_key)), modes.CBC(iv)).decryptor()
    padded = decryptor.update(body) + decryptor.finalize()
    unpadder = padding.PKCS7(_BLOCK * 8).unpadder()
    try:
        return unpadder.update(padded) + unpadder.finalize()
    except ValueError:
        raise DecryptError("the encrypted value does not decrypt with this key") from None


def signature(timestamp: str, nonce: str, encrypt_key: str, body: bytes) -> str:
    """The hex signature Feishu puts in ``X-Lark-Signature``."""
    digest = hashlib.sha256()
    digest.update(timestamp.encode("utf-8"))
    digest.update(nonce.encode("utf-8"))
    digest.update(encrypt_key.encode("utf-8"))
    digest.update(body)
    return digest.hexdigest()


def verify_signature(
    timestamp: str | None, nonce: str | None, encrypt_key: str, body: bytes, offered: str | None
) -> bool:
    """True when ``offered`` is the request's signature (compared in constant time)."""
    if not (timestamp and nonce and offered and encrypt_key):
        return False
    expected = signature(timestamp, nonce, encrypt_key, body)
    return constant_time_equal(offered.strip().lower(), expected)
