"""Nostr event ids, keys and signatures for the Buzz adapter (P16-29).

What a Nostr event is (NIP-01): ``{id, pubkey, created_at, kind, tags, content, sig}`` where ``id``
is the SHA-256 of the compact JSON array ``[0, pubkey, created_at, kind, tags, content]`` and
``sig`` is a BIP-340 Schnorr signature over that id by the x-only public key ``pubkey``.

This module serialises, hashes and checks shapes. The elliptic-curve work (deriving the public
key, signing, verifying) is done by **coincurve** (libsecp256k1) behind the `NostrCrypto`
protocol, loaded lazily from the ``buzz`` extra by `load_crypto`; nothing here computes on the
curve. Tests inject a stand-in `NostrCrypto` so the contract suite runs without the extra.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from indus.gateway.adapters._identity import bech32_decode

__all__ = [
    "INSTALL_HINT",
    "Bip340",
    "MissingCrypto",
    "NostrCrypto",
    "NostrKeys",
    "decode_secret",
    "event_id",
    "load_crypto",
    "serialise",
    "signed_event",
    "verify_event",
]

INSTALL_HINT = "pip install 'indus-agent[buzz]'"

_HEX64 = re.compile(r"[0-9a-f]{64}")
_HEX128 = re.compile(r"[0-9a-f]{128}")
_SECRET_HEX = re.compile(r"[0-9a-fA-F]{64}")


class MissingCrypto(RuntimeError):
    """The signing library is not installed."""


class NostrCrypto(Protocol):
    """The curve operations the adapter needs (BIP-340 over secp256k1)."""

    def public_key(self, secret: bytes) -> str:
        """The x-only public key (64 lower-case hex) of a 32-byte secret; ``ValueError`` when
        the secret is not a valid key."""
        ...

    def sign(self, secret: bytes, digest: bytes) -> bytes:
        """A 64-byte BIP-340 signature of a 32-byte digest."""
        ...

    def verify(self, pubkey: str, digest: bytes, signature: bytes) -> bool:
        """Does ``signature`` sign ``digest`` for the x-only ``pubkey``? Never raises."""
        ...


class Bip340:
    """`NostrCrypto` on coincurve (``buzz`` extra)."""

    def __init__(self) -> None:
        try:
            import coincurve  # pyright: ignore[reportMissingImports]
        except ModuleNotFoundError:
            raise MissingCrypto(f"Buzz needs coincurve to sign events: {INSTALL_HINT}") from None
        self._lib: Any = coincurve

    def public_key(self, secret: bytes) -> str:
        try:
            key = self._lib.PublicKeyXOnly.from_secret(secret)
        except Exception:
            raise ValueError("not a valid secp256k1 secret key") from None
        return bytes(key.format()).hex()

    def sign(self, secret: bytes, digest: bytes) -> bytes:
        private = self._lib.PrivateKey(secret)
        return bytes(private.sign_schnorr(digest, secrets.token_bytes(32)))

    def verify(self, pubkey: str, digest: bytes, signature: bytes) -> bool:
        try:
            key = self._lib.PublicKeyXOnly(bytes.fromhex(pubkey))
            return bool(key.verify(signature, digest))
        except Exception:
            return False


def load_crypto() -> NostrCrypto:
    """The production `NostrCrypto` (``MissingCrypto`` with the install hint without the extra)."""
    return Bip340()


def decode_secret(value: str) -> bytes:
    """The 32 secret bytes of an ``nsec1…`` or 64-hex private key (``ValueError`` otherwise; the
    message never contains the key)."""
    text = (value or "").strip()
    if text.lower().startswith("nsec1"):
        decoded = bech32_decode(text)
        if decoded is None or decoded[0] != "nsec" or len(decoded[1]) != 32:
            raise ValueError("the Buzz private key is not a valid nsec")
        raw = decoded[1]
    elif _SECRET_HEX.fullmatch(text):
        raw = bytes.fromhex(text)
    else:
        raise ValueError("the Buzz private key must be an nsec or 64 hex characters")
    if not any(raw):
        raise ValueError("the Buzz private key is zero")
    return raw


@dataclass(frozen=True)
class NostrKeys:
    """A bot identity: the secret (never shown) and its public key."""

    secret: bytes = field(repr=False)
    pubkey: str

    @classmethod
    def load(cls, crypto: NostrCrypto, value: str) -> NostrKeys:
        secret = decode_secret(value)
        return cls(secret, crypto.public_key(secret))

    def __repr__(self) -> str:
        return f"NostrKeys(pubkey={self.pubkey[:12]}…)"


def serialise(
    pubkey: str, created_at: int, kind: int, tags: Sequence[Sequence[str]], content: str
) -> bytes:
    """The NIP-01 byte string an event id is the hash of (compact JSON, UTF-8, unescaped)."""
    data = [0, pubkey, created_at, kind, [list(t) for t in tags], content]
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def event_id(
    pubkey: str, created_at: int, kind: int, tags: Sequence[Sequence[str]], content: str
) -> str:
    return hashlib.sha256(serialise(pubkey, created_at, kind, tags, content)).hexdigest()


def signed_event(
    crypto: NostrCrypto,
    keys: NostrKeys,
    *,
    kind: int,
    tags: Sequence[Sequence[str]],
    content: str,
    created_at: int,
) -> dict[str, Any]:
    """A complete signed event by ``keys``."""
    ident = event_id(keys.pubkey, created_at, kind, tags, content)
    signature = crypto.sign(keys.secret, bytes.fromhex(ident))
    return {
        "id": ident,
        "pubkey": keys.pubkey,
        "created_at": created_at,
        "kind": kind,
        "tags": [list(t) for t in tags],
        "content": content,
        "sig": signature.hex(),
    }


def _tags(value: object) -> list[list[str]] | None:
    if not isinstance(value, list):
        return None
    out: list[list[str]] = []
    for tag in value:  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(tag, list) or not all(isinstance(p, str) for p in tag):  # pyright: ignore[reportUnknownVariableType]
            return None
        out.append([str(p) for p in tag])  # pyright: ignore[reportUnknownVariableType]
    return out


def verify_event(crypto: NostrCrypto, event: Mapping[str, Any]) -> bool:
    """Is ``event`` well formed, is its id the hash of its contents, does its signature verify?"""
    ident, pubkey, sig = event.get("id"), event.get("pubkey"), event.get("sig")
    created, kind, content = event.get("created_at"), event.get("kind"), event.get("content")
    tags = _tags(event.get("tags"))
    if not (isinstance(ident, str) and _HEX64.fullmatch(ident)):
        return False
    if not (isinstance(pubkey, str) and _HEX64.fullmatch(pubkey)):
        return False
    if not (isinstance(sig, str) and _HEX128.fullmatch(sig)):
        return False
    if not isinstance(created, int) or isinstance(created, bool) or created < 0:
        return False
    if not isinstance(kind, int) or isinstance(kind, bool) or not 0 <= kind <= 65535:
        return False
    if not isinstance(content, str) or tags is None:
        return False
    if event_id(pubkey, created, kind, tags, content) != ident:
        return False
    return crypto.verify(pubkey, bytes.fromhex(ident), bytes.fromhex(sig))
