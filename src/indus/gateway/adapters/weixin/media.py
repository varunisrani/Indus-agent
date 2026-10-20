"""Weixin media crypto and CDN addresses (P16-19).

Tencent's iLink CDN stores every file encrypted with **AES-128 in ECB mode** with PKCS#7 padding,
one random 16-byte key per file. ECB is mandated by the vendor protocol (both directions have to
match what the Weixin clients produce and expect), not a choice of ours; it leaks equal 16-byte
blocks, so it is used for nothing but this interop. The cipher comes from the ``cryptography``
package (the ``weixin`` extra), imported only when a file is encrypted or decrypted; we never
implement AES ourselves.

Key formats (as the vendor sends them): base64 of the 16 raw bytes, or base64 of their 32 lower-
or upper-case hex characters; an image item may also carry the hex form directly (``aeskey``).
Outbound items carry base64 of the hex form (clients render a raw-byte key as a grey box).

Downloads go only to Tencent's media hosts over https (`check_cdn_url`); a message cannot point
the adapter at any other server.
"""

from __future__ import annotations

import base64
import binascii
import secrets
from typing import Any
from urllib.parse import quote, urlsplit

__all__ = [
    "BLOCK",
    "CDN_HOSTS",
    "INSTALL_HINT",
    "MediaKeyError",
    "MissingCrypto",
    "check_cdn_url",
    "decrypt",
    "download_url",
    "encrypt",
    "key_for_api",
    "new_key",
    "padded_size",
    "parse_key",
    "upload_url",
]

INSTALL_HINT = "pip install 'indus-agent[weixin]'"
BLOCK = 16
# Hosts the vendor serves media from (inbound ``full_url`` must name one of them).
CDN_HOSTS = frozenset(
    {
        "novac2c.cdn.weixin.qq.com",
        "ilinkai.weixin.qq.com",
        "wx.qlogo.cn",
        "thirdwx.qlogo.cn",
        "res.wx.qq.com",
        "mmbiz.qpic.cn",
        "mmbiz.qlogo.cn",
    }
)
_HEX = frozenset("0123456789abcdefABCDEF")


class MissingCrypto(RuntimeError):
    """The ``cryptography`` package is not installed (the ``weixin`` extra)."""


class MediaKeyError(ValueError):
    """A media key in none of the vendor's formats, or a CDN URL we refuse."""


def _ecb(key: bytes) -> Any:
    try:
        from cryptography.hazmat.primitives.ciphers import (  # pyright: ignore[reportMissingImports]
            Cipher,
            algorithms,
            modes,
        )
    except ImportError:
        raise MissingCrypto(
            f"Weixin media needs the cryptography package: {INSTALL_HINT}"
        ) from None
    if len(key) != BLOCK:
        raise MediaKeyError("a Weixin media key is 16 bytes")
    return Cipher(algorithms.AES(key), modes.ECB())  # vendor-mandated ECB (module notes)


def new_key() -> bytes:
    return secrets.token_bytes(BLOCK)


def padded_size(size: int) -> int:
    """Ciphertext size of ``size`` plaintext bytes (PKCS#7 always adds 1..16 bytes)."""
    return (size // BLOCK + 1) * BLOCK


def encrypt(data: bytes, key: bytes) -> bytes:
    cipher = _ecb(key)
    from cryptography.hazmat.primitives import padding  # pyright: ignore[reportMissingImports]

    padder = padding.PKCS7(BLOCK * 8).padder()
    padded = padder.update(data) + padder.finalize()
    encryptor = cipher.encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def decrypt(data: bytes, key: bytes) -> bytes:
    """Decrypt; a well-formed PKCS#7 tail is removed, anything else is returned as the cipher
    gave it (the vendor's clients behave the same way)."""
    if len(data) % BLOCK:
        raise MediaKeyError("the downloaded media is not a whole number of AES blocks")
    decryptor = _ecb(key).decryptor()
    clear = decryptor.update(data) + decryptor.finalize()
    pad = clear[-1] if clear else 0
    if 1 <= pad <= BLOCK and clear.endswith(bytes([pad]) * pad):
        return clear[:-pad]
    return clear


def parse_key(text: str) -> bytes:
    """The 16 key bytes from a vendor key field (base64 of raw bytes or of 32 hex digits, or
    the 32 hex digits themselves)."""
    value = text.strip()
    if len(value) == 32 and set(value) <= _HEX:
        return bytes.fromhex(value)
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise MediaKeyError("a Weixin media key that is not base64") from None
    if len(decoded) == BLOCK:
        return decoded
    if len(decoded) == 2 * BLOCK:
        hex_text = decoded.decode("ascii", errors="replace")
        if set(hex_text) <= _HEX:
            return bytes.fromhex(hex_text)
    raise MediaKeyError(f"a Weixin media key of {len(decoded)} bytes")


def key_for_api(key: bytes) -> str:
    """The form outbound items carry: base64 of the key's hex digits."""
    return base64.b64encode(key.hex().encode("ascii")).decode("ascii")


def check_cdn_url(url: str) -> str:
    """``url`` when it is https on one of Tencent's media hosts, else `MediaKeyError`."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or host not in CDN_HOSTS or parts.username or parts.password:
        raise MediaKeyError(f"refused media URL host {host or '?'}")
    return url


def download_url(cdn_base: str, param: str) -> str:
    return f"{cdn_base.rstrip('/')}/download?encrypted_query_param={quote(param, safe='')}"


def upload_url(cdn_base: str, param: str, filekey: str) -> str:
    return (
        f"{cdn_base.rstrip('/')}/upload?encrypted_query_param={quote(param, safe='')}"
        f"&filekey={quote(filekey, safe='')}"
    )
