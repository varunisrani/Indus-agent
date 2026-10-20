"""The long connection's frame codec and part assembly (P16-14), and the webhook crypto against
vectors made outside our code (the vendor's own example and openssl)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from indus.gateway.adapters.feishu.crypto import (
    DecryptError,
    decrypt,
    signature,
    verify_signature,
)
from indus.gateway.adapters.feishu.frames import (
    METHOD_CONTROL,
    METHOD_DATA,
    Frame,
    FrameError,
    PartAssembler,
    decode_frame,
    encode_frame,
    ping_frame,
)
from tests.fakes.platform.feishu_wire import (  # pyright: ignore[reportPrivateUsage]
    _frame,
    read_frame,
)

VECTORS = json.loads(
    (Path(__file__).parents[4] / "fixtures/gateway/crypto_vectors/feishu.json").read_text()
)


def test_a_ping_frame_has_the_known_bytes() -> None:
    # seq 0, log 0, service 77, method 0, one header type=ping (worked out by hand).
    expected = bytes.fromhex("08001000184d20002a0c0a0474797065120470696e67")
    assert encode_frame(ping_frame(77)) == expected
    assert decode_frame(expected) == Frame(service=77, headers=(("type", "ping"),))


def test_frames_from_the_independent_writer_decode() -> None:
    payload = json.dumps({"schema": "2.0", "text": "héllo"}).encode()
    raw = _frame(
        METHOD_DATA, {"type": "event", "message_id": "m1", "sum": "1"}, payload, seq=300, service=9
    )
    frame = decode_frame(raw)
    assert frame.method == METHOD_DATA and frame.service == 9 and frame.seq_id == 300
    assert frame.kind == "event" and frame.header("message_id") == "m1"
    assert frame.payload == payload
    # And our writer is read back by the fake's reader.
    back = read_frame(encode_frame(frame.with_header("biz_rt", "3")))
    assert back["headers"]["biz_rt"] == "3" and back["payload"] == payload
    assert back["seq"] == 300 and back["service"] == 9 and back["method"] == METHOD_DATA


def test_round_trip_with_large_numbers_and_unknown_fields() -> None:
    frame = Frame(
        seq_id=2**40 + 5,
        log_id=2**63 - 1,
        service=2**31 - 1,
        method=METHOD_CONTROL,
        headers=(("type", "pong"), ("a", "")),
        payload_encoding="gzip",
        payload_type="json",
        payload=b"\x00\x01",
        log_id_new="L-1",
    )
    raw = encode_frame(frame)
    assert decode_frame(raw) == frame
    # A fixed64 (wire type 1) and a fixed32 (wire type 5) field of an unknown number are skipped.
    # (Keys 20<<3|1 = 161 and 21<<3|5 = 173 take two varint bytes each.)
    extra = b"\xa1\x01" + b"\x01" * 8 + b"\xad\x01" + b"\x02" * 4
    assert decode_frame(raw + extra) == frame
    assert decode_frame(encode_frame(Frame(method=-1))).method == -1


@pytest.mark.parametrize(
    "raw",
    [
        b"\x08",  # ends inside a number
        b"\x08" + b"\xff" * 11,  # number too long
        b"\x42\x05ab",  # payload runs past the end
        b"\x03",  # wire type 3
        b"\x00\x00",  # field number 0
        b"\x2a\x04\x0a\x02\xff\xfe",  # header key is not UTF-8
    ],
)
def test_malformed_frames_raise(raw: bytes) -> None:
    with pytest.raises(FrameError):
        decode_frame(raw)


def _part(message: str, total: int, index: int, payload: bytes) -> Frame:
    headers = (("type", "event"), ("message_id", message), ("sum", str(total)), ("seq", str(index)))
    return Frame(method=METHOD_DATA, headers=headers, payload=payload)


def test_parts_join_in_any_order_and_repeats_count_once() -> None:
    parts = PartAssembler()
    assert parts.add(_part("m", 3, 2, b"C"), 0.0) is None
    assert parts.add(_part("m", 3, 0, b"A"), 0.1) is None
    assert parts.add(_part("m", 3, 0, b"A"), 0.2) is None
    assert parts.add(_part("m", 3, 1, b"B"), 0.3) == b"ABC"
    assert len(parts) == 0
    whole = Frame(method=METHOD_DATA, payload=b"x")
    assert parts.add(whole, 1.0) == b"x"


def test_parts_are_bounded() -> None:
    parts = PartAssembler(max_parts=4, max_bytes=10, max_age_s=5.0, max_sets=2)
    assert parts.add(_part("big", 9, 0, b"a"), 0.0) is None  # too many parts
    assert parts.add(_part("m", 2, 5, b"a"), 0.0) is None  # index out of range
    assert parts.add(_part("", 2, 0, b"a"), 0.0) is None  # no message id
    assert parts.add(_part("fat", 2, 0, b"x" * 11), 0.0) is None  # over the byte cap
    assert parts.add(_part("old", 2, 0, b"a"), 0.0) is None
    assert parts.add(_part("old", 2, 1, b"b"), 6.0) is None  # the first half expired
    assert parts.add(_part("s1", 2, 0, b"a"), 7.0) is None
    assert parts.add(_part("s2", 2, 0, b"a"), 7.0) is None
    assert parts.add(_part("s3", 2, 0, b"a"), 7.0) is None  # pushes s1 out
    assert len(parts) == 2
    assert parts.add(_part("s1", 2, 1, b"b"), 7.5) is None
    assert parts.dropped >= 6


def test_the_vendor_example_decrypts() -> None:
    vendor = VECTORS["vendor"]
    assert decrypt(vendor["encrypt_key"], vendor["encrypt"]) == vendor["plain"].encode()


def test_an_openssl_made_body_decrypts() -> None:
    case = VECTORS["url_verification"]
    plain = decrypt(case["encrypt_key"], case["encrypt"])
    assert plain.decode() == case["plain"]
    assert json.loads(plain)["token"] == case["verification_token"]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("", "P37w+VZImNgPEO1RBhJ6RtKl7n6zymIbEG1pReEzghk="),
        ("test key", "not base64!!"),
        ("test key", "AAAA"),
        ("wrong key", "P37w+VZImNgPEO1RBhJ6RtKl7n6zymIbEG1pReEzghk="),
    ],
)
def test_bad_cipher_text_or_key_raises(key: str, value: str) -> None:
    with pytest.raises(DecryptError):
        decrypt(key, value)


def test_the_signature_matches_openssl_and_compares_safely() -> None:
    case = VECTORS["signature"]
    body = case["body"].encode()
    assert (
        signature(case["timestamp"], case["nonce"], case["encrypt_key"], body) == case["signature"]
    )
    assert verify_signature(
        case["timestamp"], case["nonce"], case["encrypt_key"], body, case["signature"].upper()
    )
    assert not verify_signature(
        case["timestamp"], case["nonce"], case["encrypt_key"], body + b" ", case["signature"]
    )
    assert not verify_signature(case["timestamp"], case["nonce"], case["encrypt_key"], body, "é")
    assert not verify_signature(None, case["nonce"], case["encrypt_key"], body, case["signature"])
