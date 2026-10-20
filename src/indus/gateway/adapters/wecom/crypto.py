"""WeCom message crypto and safe XML parsing (P16-18, also used for P16-17 media).

Callback crypto (the vendor's "callback crypto" scheme):

- the AES key is the 43-character ``EncodingAESKey`` with one ``=`` added, base64-decoded: 32 bytes;
  the IV is its first 16 bytes; the cipher is AES-256-CBC;
- the plaintext is 16 random bytes, the message length as 4 bytes big-endian, the message, then the
  receive id (the corp id for a corp's own app), PKCS#7-padded to a multiple of **32** bytes;
- the signature is the lower-case hex SHA1 of token, timestamp, nonce and the base64 text, sorted as
  strings and joined (the P16-01 kit's ``verify_sha1_sorted``, a constant-time compare).

`CallbackCrypto.open` checks the signature first and only then decrypts; decryption checks the
padding, the declared length and the receive id, and every failure is a `CryptoError` with a fixed,
detail-free text. AES comes from the ``cryptography`` package (the ``wecom`` extra), imported only
when a cipher is needed; we never implement the cipher ourselves.

`parse_xml` reads the small flat XML documents WeCom sends with the standard library's expat set up
to refuse any document type declaration, entity declaration or external reference, so XXE and
entity-expansion payloads fail before any text is read.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import os
import re
import struct
from collections.abc import Callable
from typing import Any
from xml.parsers import expat

from indus.gateway.adapters._http import sha1_sorted_signature, verify_sha1_sorted

__all__ = [
    "BLOCK",
    "INSTALL_HINT",
    "CallbackCrypto",
    "CryptoError",
    "MissingCrypto",
    "UnsafeXml",
    "aes_cbc_decrypt",
    "aes_cbc_encrypt",
    "decode_aes_key",
    "pad",
    "parse_xml",
    "require_cipher",
    "unpad",
]

INSTALL_HINT = "pip install 'indus-agent[wecom]'"
# PKCS#7 block size of the WeCom scheme (not the AES block size).
BLOCK = 32
_KEY_TEXT = re.compile(r"^[A-Za-z0-9+/]{43}$")
# Flat documents only: a callback is a handful of fields.
MAX_XML_BYTES = 256 * 1024
MAX_XML_ELEMENTS = 256


class CryptoError(ValueError):
    """A payload that does not decrypt or verify (bad signature, padding, length or receive id)."""


class MissingCrypto(RuntimeError):
    """The ``cryptography`` package is not installed (the ``wecom`` extra)."""


class UnsafeXml(ValueError):
    """XML that is malformed or uses a feature we never accept (DTD, entities, references)."""


# -- AES ----------------------------------------------------------------------------------------


def _cipher(key: bytes, iv: bytes) -> Any:
    try:
        from cryptography.hazmat.primitives.ciphers import (  # pyright: ignore[reportMissingImports]
            Cipher,
            algorithms,
            modes,
        )
    except ModuleNotFoundError as exc:
        hint = f"WeCom crypto needs the cryptography package: {INSTALL_HINT}"
        raise MissingCrypto(hint) from exc
    return Cipher(algorithms.AES(key), modes.CBC(iv))  # pyright: ignore[reportUnknownVariableType]


def require_cipher() -> None:
    """`MissingCrypto` (with the install hint) unless the ``cryptography`` package is present."""
    _cipher(bytes(32), bytes(16))


def decode_aes_key(encoding_aes_key: str) -> bytes:
    """The 32-byte AES key from a 43-character ``EncodingAESKey``; `CryptoError` otherwise."""
    text = (encoding_aes_key or "").strip()
    if not _KEY_TEXT.match(text):
        raise CryptoError("the encoding AES key must be 43 base64 characters")
    try:
        key = base64.b64decode(text + "=", validate=True)
    except (binascii.Error, ValueError):
        raise CryptoError("the encoding AES key is not base64") from None
    if len(key) != 32:
        raise CryptoError("the encoding AES key does not decode to 32 bytes")
    return key


def pad(data: bytes, block: int = BLOCK) -> bytes:
    count = block - len(data) % block
    return data + bytes([count]) * count


def unpad(data: bytes, block: int = BLOCK) -> bytes:
    """PKCS#7 padding removed, every pad byte checked (`CryptoError` when it is not valid)."""
    if not data:
        raise CryptoError("empty plaintext")
    count = data[-1]
    if not 1 <= count <= block or count > len(data):
        raise CryptoError("bad padding")
    tail = data[-count:]
    bad = 0
    for value in tail:
        bad |= value ^ count
    if bad:
        raise CryptoError("bad padding")
    return data[:-count]


def aes_cbc_decrypt(key: bytes, data: bytes, *, iv: bytes | None = None) -> bytes:
    """AES-CBC decrypt (IV = the key's first 16 bytes unless given), padding not removed."""
    if not data or len(data) % 16:
        raise CryptoError("cipher text is not a whole number of AES blocks")
    decryptor = _cipher(key, iv if iv is not None else key[:16]).decryptor()
    return decryptor.update(data) + decryptor.finalize()


def aes_cbc_encrypt(key: bytes, data: bytes, *, iv: bytes | None = None) -> bytes:
    """AES-CBC encrypt of already padded ``data``."""
    encryptor = _cipher(key, iv if iv is not None else key[:16]).encryptor()
    return encryptor.update(data) + encryptor.finalize()


# -- the callback scheme ------------------------------------------------------------------------


class CallbackCrypto:
    """Signature check and decryption for one WeCom app (see the module notes)."""

    def __init__(self, token: str, encoding_aes_key: str, receive_id: str) -> None:
        if not token:
            raise CryptoError("the callback token is empty")
        if not receive_id:
            raise CryptoError("the receive id (corp id) is empty")
        self._token = token
        self._key = decode_aes_key(encoding_aes_key)
        self.receive_id = receive_id

    def __repr__(self) -> str:
        return f"<CallbackCrypto receive_id={self.receive_id}>"

    def signature(self, timestamp: str, nonce: str, encrypted: str) -> str:
        return sha1_sorted_signature(self._token, timestamp, nonce, encrypted)

    def verify(self, offered: str | None, timestamp: str, nonce: str, encrypted: str) -> bool:
        """The offered ``msg_signature`` matches (constant-time compare)."""
        return verify_sha1_sorted(offered, self._token, timestamp, nonce, encrypted)

    def open(self, offered: str | None, timestamp: str, nonce: str, encrypted: str) -> bytes:
        """Verify the signature, then decrypt; `CryptoError` for either failure."""
        if not self.verify(offered, timestamp, nonce, encrypted):
            raise CryptoError("bad signature")
        return self.decrypt(encrypted)

    def decrypt(self, encrypted: str) -> bytes:
        """The message inside ``encrypted`` (padding, length and receive id checked). Callers
        verify the signature first (`open`)."""
        try:
            raw = base64.b64decode(encrypted.strip(), validate=True)
        except (binascii.Error, ValueError):
            raise CryptoError("the encrypted text is not base64") from None
        plain = unpad(aes_cbc_decrypt(self._key, raw))
        if len(plain) < 20:
            raise CryptoError("the plaintext is too short")
        (length,) = struct.unpack(">I", plain[16:20])
        end = 20 + length
        if end > len(plain):
            raise CryptoError("the declared length runs past the plaintext")
        receive_id = plain[end:]
        expected = self.receive_id.encode("utf-8")
        if not _equal(receive_id, expected):
            raise CryptoError("the receive id does not match")
        return plain[20:end]

    def encrypt(self, message: bytes, *, prefix: bytes | None = None) -> str:
        """The base64 text WeCom would send for ``message`` (fakes and the round-trip test)."""
        head = prefix if prefix is not None else os.urandom(16)
        if len(head) != 16:
            raise ValueError("the random prefix must be 16 bytes")
        body = head + struct.pack(">I", len(message)) + message + self.receive_id.encode("utf-8")
        return base64.b64encode(aes_cbc_encrypt(self._key, pad(body))).decode("ascii")


def _equal(a: bytes, b: bytes) -> bool:
    return hmac.compare_digest(a, b)


# -- XML ----------------------------------------------------------------------------------------


def _refuse(what: str) -> Callable[..., Any]:
    def handler(*_args: object) -> Any:
        raise UnsafeXml(f"{what} is not accepted")

    return handler


def parse_xml(raw: bytes | str) -> dict[str, str]:
    """The text of each child of the root element (first occurrence wins), CDATA included.

    Refuses (`UnsafeXml`) any DOCTYPE, entity declaration or external reference, documents over
    256 KiB or 256 elements, and malformed XML."""
    data = raw.encode("utf-8") if isinstance(raw, str) else raw
    if len(data) > MAX_XML_BYTES:
        raise UnsafeXml("the XML document is too large")
    parser = expat.ParserCreate()
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.StartDoctypeDeclHandler = _refuse("a document type declaration")
    parser.EntityDeclHandler = _refuse("an entity declaration")
    parser.UnparsedEntityDeclHandler = _refuse("an unparsed entity")
    parser.ExternalEntityRefHandler = _refuse("an external reference")
    parser.NotationDeclHandler = _refuse("a notation declaration")
    fields: dict[str, str] = {}
    depth = 0
    count = 0
    current: str | None = None
    buffer: list[str] = []

    def start(name: str, _attrs: object) -> None:
        nonlocal depth, count, current
        depth += 1
        count += 1
        if count > MAX_XML_ELEMENTS:
            raise UnsafeXml("the XML document has too many elements")
        if depth == 2:
            current = name
            buffer.clear()

    def end(name: str) -> None:
        nonlocal depth, current
        if depth == 2 and current is not None:
            fields.setdefault(current, "".join(buffer))
            current = None
        depth -= 1

    def text(chunk: str) -> None:
        if depth == 2 and current is not None:
            buffer.append(chunk)

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = text
    try:
        parser.Parse(data, True)
    except expat.ExpatError:
        raise UnsafeXml("the XML document is malformed") from None
    return fields
