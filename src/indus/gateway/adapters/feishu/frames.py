"""Frames of the Feishu / Lark long connection (P16-14).

The long connection's WebSocket carries binary protobuf messages of one type, ``Frame``:

====  ================  ==========  =============================================================
 no.  field             wire type   meaning
====  ================  ==========  =============================================================
  1   seq_id            varint      sequence number
  2   log_id            varint      server log id
  3   service           varint      the ``service_id`` of the connection URL
  4   method            varint      0 = control (ping / pong), 1 = data (an event or a card click)
  5   headers           bytes       repeated ``Header {1: key, 2: value}`` (strings)
  6   payload_encoding  bytes       string
  7   payload_type      bytes       string
  8   payload           bytes       the event JSON (data frames) or a client config (pongs)
  9   log_id_new        bytes       string
====  ================  ==========  =============================================================

Only these fields and wire types 0 and 2 matter, so this module is a small codec of its own (no
protobuf runtime): unsigned varints and length-delimited fields; fields of other wire types are
skipped when reading. Anything malformed raises `FrameError`.

A large event arrives in parts: data frames with the same ``message_id`` header, ``sum`` parts and
a ``seq`` index each. `PartAssembler` joins them, bounded in parts, bytes and age.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass, field, replace

__all__ = [
    "METHOD_CONTROL",
    "METHOD_DATA",
    "Frame",
    "FrameError",
    "PartAssembler",
    "decode_frame",
    "encode_frame",
    "ping_frame",
]

METHOD_CONTROL = 0
METHOD_DATA = 1

# Header names the protocol uses.
TYPE = "type"
MESSAGE_ID = "message_id"
SUM = "sum"
SEQ = "seq"
BIZ_RT = "biz_rt"

_MAX_VARINT_BYTES = 10
_MAX_FRAME_BYTES = 8 * 1024 * 1024


class FrameError(ValueError):
    """Bytes that are not a frame we can read."""


@dataclass(frozen=True)
class Frame:
    seq_id: int = 0
    log_id: int = 0
    service: int = 0
    method: int = METHOD_CONTROL
    headers: tuple[tuple[str, str], ...] = ()
    payload_encoding: str = ""
    payload_type: str = ""
    payload: bytes = b""
    log_id_new: str = ""

    def header(self, name: str) -> str:
        """The first value of header ``name``; ``""`` when absent."""
        for key, value in self.headers:
            if key == name:
                return value
        return ""

    @property
    def kind(self) -> str:
        """The ``type`` header: ``ping`` / ``pong`` (control), ``event`` / ``card`` (data)."""
        return self.header(TYPE)

    def with_header(self, name: str, value: str) -> Frame:
        kept = tuple((k, v) for k, v in self.headers if k != name)
        return replace(self, headers=(*kept, (name, value)))


# -- writing ----------------------------------------------------------------------------------


def _varint(value: int) -> bytes:
    if value < 0:
        # int32 negatives are written as 64-bit two's complement (ten bytes).
        value += 1 << 64
    out = bytearray()
    while True:
        low = value & 0x7F
        value >>= 7
        if value:
            out.append(low | 0x80)
        else:
            out.append(low)
            return bytes(out)


def _key(number: int, wire_type: int) -> bytes:
    return _varint((number << 3) | wire_type)


def _number_field(number: int, value: int) -> bytes:
    return _key(number, 0) + _varint(value)


def _bytes_field(number: int, value: bytes) -> bytes:
    return _key(number, 2) + _varint(len(value)) + value


def _text_field(number: int, value: str) -> bytes:
    return _bytes_field(number, value.encode("utf-8"))


def _encode_header(key: str, value: str) -> bytes:
    return _text_field(1, key) + _text_field(2, value)


def encode_frame(frame: Frame) -> bytes:
    """The frame's wire bytes (the four numbers always written; optional strings when set)."""
    parts = [
        _number_field(1, frame.seq_id),
        _number_field(2, frame.log_id),
        _number_field(3, frame.service),
        _number_field(4, frame.method),
    ]
    parts.extend(_bytes_field(5, _encode_header(k, v)) for k, v in frame.headers)
    if frame.payload_encoding:
        parts.append(_text_field(6, frame.payload_encoding))
    if frame.payload_type:
        parts.append(_text_field(7, frame.payload_type))
    if frame.payload:
        parts.append(_bytes_field(8, frame.payload))
    if frame.log_id_new:
        parts.append(_text_field(9, frame.log_id_new))
    return b"".join(parts)


# -- reading ----------------------------------------------------------------------------------


@dataclass
class _Reader:
    data: bytes
    at: int = 0

    def done(self) -> bool:
        return self.at >= len(self.data)

    def varint(self) -> int:
        value = 0
        for shift_index in range(_MAX_VARINT_BYTES):
            if self.at >= len(self.data):
                raise FrameError("the frame ends inside a number")
            byte = self.data[self.at]
            self.at += 1
            value |= (byte & 0x7F) << (7 * shift_index)
            if not byte & 0x80:
                return value
        raise FrameError("a number in the frame is too long")

    def chunk(self) -> bytes:
        size = self.varint()
        end = self.at + size
        if end > len(self.data):
            raise FrameError("a field runs past the end of the frame")
        piece = self.data[self.at : end]
        self.at = end
        return piece

    def skip(self, wire_type: int) -> None:
        if wire_type == 0:
            self.varint()
        elif wire_type == 1:
            self._advance(8)
        elif wire_type == 2:
            self.chunk()
        elif wire_type == 5:
            self._advance(4)
        else:
            raise FrameError(f"wire type {wire_type} is not supported")

    def _advance(self, count: int) -> None:
        if self.at + count > len(self.data):
            raise FrameError("a field runs past the end of the frame")
        self.at += count

    def fields(self) -> Iterable[tuple[int, int]]:
        while not self.done():
            key = self.varint()
            number, wire_type = key >> 3, key & 0x7
            if number == 0:
                raise FrameError("field number 0")
            yield number, wire_type


def _text(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise FrameError("a text field is not UTF-8") from None


def _signed32(value: int) -> int:
    value &= (1 << 64) - 1
    if value >= 1 << 63:
        value -= 1 << 64
    return value


def _decode_header(raw: bytes) -> tuple[str, str]:
    reader = _Reader(raw)
    key = value = ""
    for number, wire_type in reader.fields():
        if number in (1, 2) and wire_type == 2:
            text = _text(reader.chunk())
            if number == 1:
                key = text
            else:
                value = text
        else:
            reader.skip(wire_type)
    return key, value


def decode_frame(data: bytes) -> Frame:
    """Read one frame; `FrameError` for anything that is not a well-formed frame."""
    if len(data) > _MAX_FRAME_BYTES:
        raise FrameError("the frame is too large")
    reader = _Reader(bytes(data))
    numbers = {1: 0, 2: 0, 3: 0, 4: 0}
    headers: list[tuple[str, str]] = []
    texts = {6: "", 7: "", 9: ""}
    payload = b""
    for number, wire_type in reader.fields():
        if number in numbers and wire_type == 0:
            numbers[number] = reader.varint()
        elif number == 5 and wire_type == 2:
            headers.append(_decode_header(reader.chunk()))
        elif number in texts and wire_type == 2:
            texts[number] = _text(reader.chunk())
        elif number == 8 and wire_type == 2:
            payload = reader.chunk()
        else:
            reader.skip(wire_type)
    return Frame(
        seq_id=numbers[1],
        log_id=numbers[2],
        service=_signed32(numbers[3]),
        method=_signed32(numbers[4]),
        headers=tuple(headers),
        payload_encoding=texts[6],
        payload_type=texts[7],
        payload=payload,
        log_id_new=texts[9],
    )


def ping_frame(service: int) -> Frame:
    """The client's keep-alive (a control frame of type ``ping``)."""
    return Frame(service=service, method=METHOD_CONTROL, headers=((TYPE, "ping"),))


# -- parts ------------------------------------------------------------------------------------


@dataclass
class _Pending:
    started: float
    total: int
    parts: dict[int, bytes] = field(default_factory=dict[int, bytes])
    size: int = 0


class PartAssembler:
    """Joins the parts of split data frames (see the module notes).

    ``add(frame, now)`` returns the whole payload once every part is in (at once for a frame that
    is not split), else None. A set older than ``max_age_s``, a part count over ``max_parts``, a
    total over ``max_bytes`` or a bad ``sum`` / ``seq`` drops the set; at most ``max_sets`` sets are
    open at once (the oldest goes)."""

    def __init__(
        self,
        *,
        max_parts: int = 64,
        max_bytes: int = 4 * 1024 * 1024,
        max_age_s: float = 30.0,
        max_sets: int = 64,
    ) -> None:
        self.max_parts = max_parts
        self.max_bytes = max_bytes
        self.max_age_s = max_age_s
        self.max_sets = max_sets
        self._open: OrderedDict[str, _Pending] = OrderedDict()
        self.dropped = 0

    def __len__(self) -> int:
        return len(self._open)

    def _expire(self, now: float) -> None:
        for key in [k for k, p in self._open.items() if now - p.started > self.max_age_s]:
            del self._open[key]
            self.dropped += 1

    def add(self, frame: Frame, now: float) -> bytes | None:
        self._expire(now)
        try:
            total = int(frame.header(SUM) or "1")
            index = int(frame.header(SEQ) or "0")
        except ValueError:
            self.dropped += 1
            return None
        if total <= 1:
            return frame.payload if len(frame.payload) <= self.max_bytes else None
        key = frame.header(MESSAGE_ID)
        if not key or total > self.max_parts or not 0 <= index < total:
            self.dropped += 1
            return None
        pending = self._open.get(key)
        if pending is None:
            pending = _Pending(started=now, total=total)
            self._open[key] = pending
            while len(self._open) > self.max_sets:
                self._open.popitem(last=False)
                self.dropped += 1
        if pending.total != total:
            del self._open[key]
            self.dropped += 1
            return None
        if index not in pending.parts:
            pending.parts[index] = frame.payload
            pending.size += len(frame.payload)
        if pending.size > self.max_bytes:
            del self._open[key]
            self.dropped += 1
            return None
        if len(pending.parts) < total:
            return None
        del self._open[key]
        return b"".join(pending.parts[i] for i in range(total))
