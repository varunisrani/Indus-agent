"""Nostr event ids, keys and BIP-340 signatures for Buzz (P16-29).

The coincurve tests run only with the ``buzz`` extra installed; the rest needs nothing.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

import indus
from indus.gateway.adapters.buzz.crypto import (
    MissingCrypto,
    NostrKeys,
    decode_secret,
    event_id,
    load_crypto,
    serialise,
    signed_event,
    verify_event,
)
from tests.fakes.platform.buzz_wire import FakeNostrCrypto

VECTORS = json.loads(
    (Path(__file__).parents[4] / "fixtures" / "gateway" / "bip340_vectors.json").read_text()
)["vectors"]
# The NIP-19 spec's example private key, both forms built at runtime (never a key-shaped literal).
NSEC = "nsec1" + "vl029mgpspedva04g90vltkh6fvh240zqtv9k0t9af8935ke9laqsnlfe5"
NSEC_HEX = "67dea2ed018072d675f5415ecfaed7d2" + "597555e202d85b3d65ea4e58d2d92ffa"


SRC = Path(indus.__file__).resolve().parent
# Pieces of the secp256k1 domain parameters (field prime, group order, generator x and y): any of
# them in ``src/`` would mean curve arithmetic of our own instead of coincurve's libsecp256k1.
_CURVE_CONSTANTS = re.compile(
    r"fffffffefffffc2f|bfd25e8cd0364141|79be667ef9dcbbac|483ada7726a3c465", re.IGNORECASE
)


def test_no_curve_arithmetic_of_our_own_in_src() -> None:
    offenders = [
        f"{path.relative_to(SRC)}:{number}"
        for path in sorted(SRC.rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _CURVE_CONSTANTS.search(line.replace("_", ""))
    ]
    assert offenders == [], f"secp256k1 constants in src (use coincurve): {offenders}"


def test_the_id_is_the_hash_of_the_compact_serialisation() -> None:
    tags = [["h", "c0ffee"], ["e", "ab" * 32, "", "root"]]
    raw = serialise("aa" * 32, 1_700_000_000, 9, tags, 'line one\n"quoted" ü 😀')
    assert (
        raw
        == (
            '[0,"'
            + "aa" * 32
            + '",1700000000,9,[["h","c0ffee"],["e","'
            + "ab" * 32
            + '","","root"]],'
            '"line one\\n\\"quoted\\" ü 😀"]'
        ).encode()
    )
    assert event_id("aa" * 32, 1_700_000_000, 9, tags, 'line one\n"quoted" ü 😀') == (
        hashlib.sha256(raw).hexdigest()
    )


def test_private_keys_decode_from_nsec_or_hex() -> None:
    assert decode_secret(NSEC).hex() == NSEC_HEX
    assert decode_secret(NSEC_HEX.upper()).hex() == NSEC_HEX
    for bad in ("", "nsec1abc", NSEC[:-1] + "q", "npub" + NSEC[4:], "00" * 32, "zz" * 32, "ab"):
        with pytest.raises(ValueError) as refused:
            decode_secret(bad)
        assert NSEC_HEX not in str(refused.value)


def test_keys_never_show_their_secret() -> None:
    keys = NostrKeys.load(FakeNostrCrypto(), NSEC_HEX)
    assert NSEC_HEX not in repr(keys) and keys.pubkey[:12] in repr(keys)


def test_events_verify_only_when_id_and_signature_match() -> None:
    crypto = FakeNostrCrypto()
    keys = NostrKeys.load(crypto, NSEC_HEX)
    event = signed_event(crypto, keys, kind=9, tags=[["h", "c1"]], content="hi", created_at=5)
    assert verify_event(crypto, event)
    tampered: list[dict[str, Any]] = [
        {**event, "content": "changed"},
        {**event, "sig": "00" * 64},
        {**event, "pubkey": "bb" * 32},
        {**event, "id": event["id"].upper()},
        {**event, "kind": "9"},
        {**event, "created_at": True},
        {**event, "tags": [["h", 3]]},
        {k: v for k, v in event.items() if k != "sig"},
    ]
    for bad in tampered:
        assert not verify_event(crypto, bad)


def test_a_missing_signing_library_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "coincurve", None)
    with pytest.raises(MissingCrypto, match=r"indus-agent\[buzz\]"):
        load_crypto()


# -- the real BIP-340 implementation (buzz extra) --------------------------------------------------


@pytest.fixture
def bip340() -> Any:
    pytest.importorskip("coincurve")
    return load_crypto()


def test_bip340_vectors_verify_as_the_spec_says(bip340: Any) -> None:
    assert len(VECTORS) == 15
    for vector in VECTORS:
        digest = bytes.fromhex(vector["message"])
        signature = bytes.fromhex(vector["signature"])
        assert bip340.verify(vector["public_key"], digest, signature) is vector["valid"], vector
        if vector["secret_key"]:
            secret = bytes.fromhex(vector["secret_key"])
            assert bip340.public_key(secret) == vector["public_key"]


def test_bip340_signatures_round_trip_and_events_verify(bip340: Any) -> None:
    keys = NostrKeys.load(bip340, NSEC)
    digest = hashlib.sha256(b"indus").digest()
    signature = bip340.sign(keys.secret, digest)
    assert len(signature) == 64 and bip340.verify(keys.pubkey, digest, signature)
    assert not bip340.verify(keys.pubkey, hashlib.sha256(b"other").digest(), signature)
    event = signed_event(bip340, keys, kind=9, tags=[["h", "c1"]], content="hello", created_at=7)
    assert verify_event(bip340, event)
    assert not verify_event(bip340, {**event, "content": "hellO"})
    assert not bip340.verify("zz", digest, signature)
    with pytest.raises(ValueError):
        bip340.public_key(b"\xff" * 32)  # above the curve order
