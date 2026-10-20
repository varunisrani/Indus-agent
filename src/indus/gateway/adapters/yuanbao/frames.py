"""Yuanbao's WebSocket frames: a small protobuf wire codec and the messages the adapter speaks.

Every frame on the bot socket is one protobuf ``ConnMsg``: field 1 a ``Head``, field 2 the bytes
of a business message. ``Head`` fields: 1 ``cmd_type`` (0 request, 1 response, 2 push, 3 push
acknowledgement), 2 ``cmd`` (the command or method name), 3 ``seq_no``, 4 ``msg_id`` (pairs a
response with its request), 5 ``module`` (the service), 6 ``need_ack``, 10 ``status`` (non-zero:
the request failed). Field numbers are the vendor's (interop facts); everything here is wire
format only — no cryptography.

The codec reads the four wire types protobuf has today (varint, 64-bit, length-delimited,
32-bit); fields a reader does not know are skipped. Malformed input raises `FrameError`.

Business messages are built with `Writer` and read with `Fields`; one function per message the
adapter sends or reads (auth-bind, ping, push ack, C2C and group sends, reply heartbeats, group
info and member list) keeps the numbers in one place. Message bodies are lists of `Element`
(``msg_type`` such as ``TIMTextElem`` plus its content fields), the same shape the vendor's JSON
pushes use.
"""

from __future__ import annotations

import itertools
import json
import threading
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, cast

__all__ = [
    "AUTH_BIND",
    "BIZ_MODULE",
    "CONN_MODULE",
    "FINISH",
    "KICKOUT",
    "PING",
    "PUSH",
    "PUSH_ACK",
    "REQUEST",
    "RESPONSE",
    "RUNNING",
    "Element",
    "Fields",
    "Frame",
    "FrameError",
    "Head",
    "Push",
    "Writer",
    "auth_bind",
    "auth_bind_answer",
    "c2c_message",
    "custom_element",
    "decode_element",
    "decode_frame",
    "decode_push",
    "element_bytes",
    "encode_frame",
    "face_element",
    "file_element",
    "group_heartbeat",
    "group_info_answer",
    "group_info_query",
    "group_message",
    "image_element",
    "member_list_answer",
    "member_list_query",
    "next_seq",
    "private_heartbeat",
    "push_bytes",
    "push_from_json",
    "request",
    "result_answer",
    "text_element",
]

# ``Head.cmd_type``
REQUEST: Final = 0
RESPONSE: Final = 1
PUSH: Final = 2
PUSH_ACK: Final = 3

CONN_MODULE: Final = "conn_access"
BIZ_MODULE: Final = "yuanbao_openclaw_proxy"
AUTH_BIND: Final = "auth-bind"
PING: Final = "ping"
KICKOUT: Final = "kickout"

# Reply heartbeat values.
RUNNING: Final = 1
FINISH: Final = 2

_VARINT, _I64, _LEN, _I32 = 0, 1, 2, 5
_FIXED = {_I64: 8, _I32: 4}
_MAX_VARINT_BYTES = 10
_UINT64 = (1 << 64) - 1
_UINT32 = (1 << 32) - 1


class FrameError(ValueError):
    """Bytes that are not a well-formed protobuf message."""


# -- the wire format ------------------------------------------------------------------------------


def _varint(value: int) -> bytes:
    if value < 0:
        value &= _UINT64  # negative numbers travel as their 64-bit two's complement
    out = bytearray()
    while True:
        low = value & 0x7F
        value >>= 7
        if value:
            out.append(low | 0x80)
        else:
            out.append(low)
            return bytes(out)


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    for n in range(_MAX_VARINT_BYTES):
        if pos >= len(data):
            raise FrameError("a varint runs past the end of the message")
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << (7 * n)
        if not byte & 0x80:
            return value & _UINT64, pos
    raise FrameError("a varint is longer than ten bytes")


class Fields:
    """One parsed protobuf message: field number → values in wire order."""

    def __init__(self, values: Mapping[int, Sequence[int | bytes]] | None = None) -> None:
        self._values: dict[int, list[int | bytes]] = {k: list(v) for k, v in (values or {}).items()}

    @classmethod
    def parse(cls, data: bytes) -> Fields:
        found: dict[int, list[int | bytes]] = {}
        pos = 0
        size = len(data)
        while pos < size:
            key, pos = _read_varint(data, pos)
            number, kind = key >> 3, key & 0x07
            if number == 0:
                raise FrameError("field number 0")
            if kind == _VARINT:
                value, pos = _read_varint(data, pos)
                found.setdefault(number, []).append(value)
                continue
            if kind == _LEN:
                length, pos = _read_varint(data, pos)
            elif kind in _FIXED:
                length = _FIXED[kind]
            else:
                raise FrameError(f"wire type {kind} is not supported")
            end = pos + length
            if end > size:
                raise FrameError("a field runs past the end of the message")
            chunk = bytes(data[pos:end])
            pos = end
            if kind == _LEN:
                found.setdefault(number, []).append(chunk)
            # 32/64-bit fields are skipped: no message here uses them.
        return cls(found)

    def num(self, number: int, default: int = 0) -> int:
        for value in self._values.get(number, ()):
            if isinstance(value, int):
                return value
        return default

    def raw(self, number: int) -> bytes:
        for value in self._values.get(number, ()):
            if isinstance(value, bytes):
                return value
        return b""

    def raws(self, number: int) -> list[bytes]:
        return [v for v in self._values.get(number, ()) if isinstance(v, bytes)]

    def text(self, number: int) -> str:
        return self.raw(number).decode("utf-8", "replace")

    def sub(self, number: int) -> Fields:
        return Fields.parse(self.raw(number))

    def subs(self, number: int) -> list[Fields]:
        return [Fields.parse(chunk) for chunk in self.raws(number)]

    def __contains__(self, number: object) -> bool:
        return number in self._values


class Writer:
    """Builds one protobuf message field by field (empty values are left out unless kept)."""

    def __init__(self) -> None:
        self._out = bytearray()

    def num(self, number: int, value: int | None, *, keep_zero: bool = False) -> Writer:
        if value is None or (value == 0 and not keep_zero):
            return self
        self._out += _varint(number << 3 | _VARINT) + _varint(int(value))
        return self

    def raw(self, number: int, value: bytes, *, keep_empty: bool = False) -> Writer:
        if not value and not keep_empty:
            return self
        self._out += _varint(number << 3 | _LEN) + _varint(len(value)) + value
        return self

    def text(self, number: int, value: str | None, *, keep_empty: bool = False) -> Writer:
        return self.raw(number, (value or "").encode("utf-8"), keep_empty=keep_empty)

    def sub(self, number: int, value: Writer | bytes, *, keep_empty: bool = False) -> Writer:
        data = value.build() if isinstance(value, Writer) else value
        return self.raw(number, data, keep_empty=keep_empty)

    def build(self) -> bytes:
        return bytes(self._out)


# -- ConnMsg ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Head:
    cmd_type: int
    cmd: str = ""
    seq_no: int = 0
    msg_id: str = ""
    module: str = ""
    need_ack: bool = False
    status: int = 0


@dataclass(frozen=True)
class Frame:
    head: Head
    data: bytes = b""


_seq_lock = threading.Lock()
_seq = itertools.count()


def next_seq() -> int:
    """The next ``seq_no`` (one counter per process, wrapping at 32 bits)."""
    with _seq_lock:
        return next(_seq) & _UINT32


def _head(head: Head) -> Writer:
    status = head.status & _UINT64 if head.status < 0 else head.status
    return (
        Writer()
        .num(1, head.cmd_type)
        .text(2, head.cmd)
        .num(3, head.seq_no)
        .text(4, head.msg_id)
        .text(5, head.module)
        .num(6, 1 if head.need_ack else 0)
        .num(10, status)
    )


def encode_frame(head: Head, data: bytes = b"") -> bytes:
    return Writer().sub(1, _head(head), keep_empty=True).raw(2, data).build()


def decode_frame(raw: bytes) -> Frame:
    """``FrameError`` when ``raw`` is not a ``ConnMsg``."""
    fields = Fields.parse(raw)
    if 1 not in fields:
        raise FrameError("the frame has no head")
    head = fields.sub(1)
    status = head.num(10)
    if status >= 1 << 63:
        status -= 1 << 64
    return Frame(
        Head(
            cmd_type=head.num(1),
            cmd=head.text(2),
            seq_no=head.num(3),
            msg_id=head.text(4),
            module=head.text(5),
            need_ack=head.num(6) != 0,
            status=status,
        ),
        fields.raw(2),
    )


def request(cmd: str, msg_id: str, module: str, data: bytes = b"") -> bytes:
    return encode_frame(Head(REQUEST, cmd, next_seq(), msg_id, module), data)


# -- message bodies ------------------------------------------------------------------------------

# ``MsgContent`` fields: (number, key, is_int). 8 (image infos) and 999 (the ext map) are nested.
_CONTENT: tuple[tuple[int, str, bool], ...] = (
    (1, "text", False),
    (2, "uuid", False),
    (3, "image_format", True),
    (4, "data", False),
    (5, "desc", False),
    (6, "ext", False),
    (7, "sound", False),
    (9, "index", True),
    (10, "url", False),
    (11, "file_size", True),
    (12, "file_name", False),
)
_IMAGE_INFO: tuple[tuple[int, str, bool], ...] = (
    (1, "type", True),
    (2, "size", True),
    (3, "width", True),
    (4, "height", True),
    (5, "url", False),
)


@dataclass(frozen=True)
class Element:
    """One message body element: ``kind`` (``TIMTextElem`` …) and its content fields."""

    kind: str
    content: Mapping[str, Any] = field(default_factory=dict[str, Any])

    def get(self, key: str, default: Any = None) -> Any:
        return self.content.get(key, default)

    def as_json(self) -> dict[str, Any]:
        return {"msg_type": self.kind, "msg_content": dict(self.content)}


def _write_table(values: Mapping[str, Any], table: Iterable[tuple[int, str, bool]]) -> Writer:
    out = Writer()
    for number, key, is_int in table:
        value = values.get(key)
        if value in (None, ""):
            continue
        if is_int:
            out.num(number, int(value), keep_zero=key == "index")
        else:
            out.text(number, str(value))
    return out


def _read_table(fields: Fields, table: Iterable[tuple[int, str, bool]]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for number, key, is_int in table:
        if number not in fields:
            continue
        found[key] = fields.num(number) if is_int else fields.text(number)
    return found


def _content_bytes(content: Mapping[str, Any]) -> bytes:
    out = _write_table(content, _CONTENT)
    infos = content.get("image_info_array")
    for info in cast(Sequence[Any], infos) if isinstance(infos, list | tuple) else ():
        if isinstance(info, Mapping):
            out.sub(8, _write_table(cast(Mapping[str, Any], info), _IMAGE_INFO), keep_empty=True)
    ext = content.get("ext_map")
    if isinstance(ext, Mapping):
        for key, value in cast(Mapping[Any, Any], ext).items():
            out.sub(999, Writer().text(1, str(key)).text(2, str(value)))
    return out.build()


def _content_of(fields: Fields) -> dict[str, Any]:
    content = _read_table(fields, _CONTENT)
    infos = [_read_table(f, _IMAGE_INFO) for f in fields.subs(8)]
    if infos:
        content["image_info_array"] = infos
    ext = {f.text(1): f.text(2) for f in fields.subs(999) if f.text(1)}
    if ext:
        content["ext_map"] = ext
    return content


def element_bytes(element: Element) -> bytes:
    return Writer().text(1, element.kind).sub(2, _content_bytes(element.content)).build()


def decode_element(data: bytes) -> Element:
    fields = Fields.parse(data)
    return Element(fields.text(1), _content_of(fields.sub(2)))


def text_element(text: str) -> Element:
    return Element("TIMTextElem", {"text": text})


def custom_element(data: Mapping[str, Any]) -> Element:
    return Element("TIMCustomElem", {"data": json.dumps(dict(data), ensure_ascii=False)})


def face_element(data: Mapping[str, Any]) -> Element:
    """A sticker: index 0 and the sticker's description as compact JSON."""
    payload = json.dumps(dict(data), ensure_ascii=False, separators=(",", ":"))
    return Element("TIMFaceElem", {"index": 0, "data": payload})


def image_element(
    url: str, *, uuid: str, size: int, width: int, height: int, image_format: int
) -> Element:
    info = {"type": 1, "size": size, "width": width, "height": height, "url": url}
    return Element(
        "TIMImageElem",
        {"uuid": uuid, "image_format": image_format, "image_info_array": [info]},
    )


def file_element(url: str, *, uuid: str, name: str, size: int) -> Element:
    return Element("TIMFileElem", {"uuid": uuid, "file_name": name, "file_size": size, "url": url})


# -- inbound pushes -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Push:
    """One ``InboundMessagePush`` (fields we read; the rest is ignored)."""

    callback_command: str = ""
    from_account: str = ""
    to_account: str = ""
    sender_nickname: str = ""
    group_code: str = ""
    group_name: str = ""
    msg_seq: int = 0
    msg_time: int = 0
    msg_key: str = ""
    msg_id: str = ""
    elements: tuple[Element, ...] = ()
    cloud_custom_data: str = ""
    bot_owner_id: str = ""
    private_from_group_code: str = ""

    @property
    def message_id(self) -> str:
        return self.msg_id or self.msg_key


def decode_push(data: bytes) -> Push:
    """``FrameError`` when ``data`` is not a push."""
    fields = Fields.parse(data)
    return Push(
        callback_command=fields.text(1),
        from_account=fields.text(2),
        to_account=fields.text(3),
        sender_nickname=fields.text(4),
        group_code=fields.text(6) or fields.text(5),
        group_name=fields.text(7),
        msg_seq=fields.num(8),
        msg_time=fields.num(10),
        msg_key=fields.text(11),
        msg_id=fields.text(12),
        elements=tuple(decode_element(chunk) for chunk in fields.raws(13)),
        cloud_custom_data=fields.text(14),
        bot_owner_id=fields.text(16),
        private_from_group_code=fields.text(19),
    )


def push_bytes(push: Push) -> bytes:
    """The protobuf form of ``push`` (the wire fake sends these)."""
    out = (
        Writer()
        .text(1, push.callback_command)
        .text(2, push.from_account)
        .text(3, push.to_account)
        .text(4, push.sender_nickname)
        .text(6, push.group_code)
        .text(7, push.group_name)
        .num(8, push.msg_seq)
        .num(10, push.msg_time)
        .text(11, push.msg_key)
        .text(12, push.msg_id)
    )
    for element in push.elements:
        out.raw(13, element_bytes(element))
    return (
        out.text(14, push.cloud_custom_data)
        .text(16, push.bot_owner_id)
        .text(19, push.private_from_group_code)
        .build()
    )


def _pick(raw: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        value = raw.get(name)
        if value not in (None, "", 0):
            return value
    return None


def _json_elements(value: object) -> Iterator[Element]:
    for item in cast(Sequence[Any], value) if isinstance(value, list) else ():
        if not isinstance(item, Mapping):
            continue
        entry = cast(Mapping[str, Any], item)
        kind = str(_pick(entry, "msg_type", "MsgType") or "")
        content = _pick(entry, "msg_content", "MsgContent")
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except ValueError:
                content = {"text": content}
        if not isinstance(content, Mapping):
            content = {}
        if kind:
            yield Element(kind, dict(cast(Mapping[str, Any], content)))


def push_from_json(raw: Mapping[str, Any]) -> Push | None:
    """A push sent as JSON (snake_case or the IM callback's PascalCase keys); None when it holds
    no message."""

    def text(*names: str) -> str:
        value = _pick(raw, *names)
        return str(value) if value is not None else ""

    def number(*names: str) -> int:
        value = _pick(raw, *names)
        try:
            return int(value) if value is not None else 0
        except (TypeError, ValueError):
            return 0

    elements = tuple(_json_elements(_pick(raw, "msg_body", "MsgBody")))
    push = Push(
        callback_command=text("callback_command", "CallbackCommand"),
        from_account=text("from_account", "From_Account"),
        to_account=text("to_account", "To_Account"),
        sender_nickname=text("sender_nickname", "nick_name"),
        group_code=text("group_code", "GroupId", "group_id"),
        group_name=text("group_name"),
        msg_seq=number("msg_seq", "MsgSeq"),
        msg_time=number("msg_time", "MsgTime"),
        msg_key=text("msg_key", "MsgKey"),
        msg_id=text("msg_id"),
        elements=elements,
        cloud_custom_data=text("cloud_custom_data", "CloudCustomData"),
        bot_owner_id=text("bot_owner_id", "botOwnerId"),
        private_from_group_code=text("private_from_group_code"),
    )
    if not push.from_account and not push.elements and not push.callback_command:
        return None
    return push


# -- requests the adapter sends -----------------------------------------------------------------


def auth_bind(
    *,
    uid: str,
    source: str,
    token: str,
    app_version: str,
    os_name: str,
    instance_id: str,
    bot_version: str,
    route_env: str = "",
) -> bytes:
    """``AuthBindReq``: 1 business id, 2 ``{uid, source, token}``, 3 device info, 5 env name."""
    device = (
        Writer()
        .text(1, app_version)
        .text(2, os_name)
        .text(10, instance_id, keep_empty=True)
        .text(24, bot_version)
    )
    who = Writer().text(1, uid).text(2, source).text(3, token)
    return (
        Writer()
        .text(1, "ybBot", keep_empty=True)
        .sub(2, who)
        .sub(3, device)
        .text(5, route_env)
        .build()
    )


def auth_bind_answer(data: bytes) -> tuple[int, str, str]:
    """``AuthBindRsp``: (code, message, connect id)."""
    fields = Fields.parse(data)
    return fields.num(1), fields.text(2), fields.text(3)


def result_answer(data: bytes) -> tuple[int, str]:
    """The ``{code 1, message 2}`` every business answer starts with ((0, "") when empty)."""
    if not data:
        return 0, ""
    fields = Fields.parse(data)
    code = fields.num(1)
    if code >= 1 << 63:
        code -= 1 << 64
    return code, fields.text(2)


def _body(out: Writer, number: int, elements: Iterable[Element]) -> Writer:
    for element in elements:
        out.raw(number, element_bytes(element))
    return out


def c2c_message(
    *,
    msg_id: str,
    to_account: str,
    from_account: str,
    elements: Sequence[Element],
    group_code: str = "",
    random: int = 0,
) -> bytes:
    """``SendC2CMessageReq``: 1 msg id, 2 to, 3 from, 4 random, 5 body, 6 group code (a DM that
    started in a group)."""
    out = (
        Writer()
        .text(1, msg_id)
        .text(2, to_account, keep_empty=True)
        .text(3, from_account)
        .num(4, random)
    )
    return _body(out, 5, elements).text(6, group_code).build()


def group_message(
    *,
    msg_id: str,
    group_code: str,
    from_account: str,
    elements: Sequence[Element],
    quote: str = "",
) -> bytes:
    """``SendGroupMessageReq``: 1 msg id, 2 group code, 3 from, 6 body, 7 quoted message id."""
    out = Writer().text(1, msg_id).text(2, group_code, keep_empty=True).text(3, from_account)
    return _body(out, 6, elements).text(7, quote).build()


def private_heartbeat(*, from_account: str, to_account: str, value: int) -> bytes:
    """``SendPrivateHeartbeatReq``: 1 from, 2 to, 3 RUNNING / FINISH."""
    return Writer().text(1, from_account).text(2, to_account).num(3, value, keep_zero=True).build()


def group_heartbeat(*, from_account: str, group_code: str, value: int, at_ms: int) -> bytes:
    """``SendGroupHeartbeatReq``: 1 from, 2 to (empty for groups), 3 group code, 4 time (ms),
    5 RUNNING / FINISH."""
    return (
        Writer()
        .text(1, from_account)
        .text(2, "", keep_empty=True)
        .text(3, group_code)
        .num(4, at_ms)
        .num(5, value, keep_zero=True)
        .build()
    )


def group_info_query(group_code: str) -> bytes:
    return Writer().text(1, group_code, keep_empty=True).build()


def group_info_answer(data: bytes) -> dict[str, Any]:
    """``QueryGroupInfoRsp``: code, message, ``{name 1, owner id 2, owner nickname 3, size 4}``."""
    fields = Fields.parse(data)
    info = fields.sub(3)
    return {
        "code": fields.num(1),
        "message": fields.text(2),
        "group_name": info.text(1),
        "owner_id": info.text(2),
        "owner_nickname": info.text(3),
        "member_count": info.num(4),
    }


def member_list_query(group_code: str, offset: int, limit: int) -> bytes:
    return Writer().text(1, group_code, keep_empty=True).num(2, offset).num(3, limit).build()


def member_list_answer(data: bytes) -> dict[str, Any]:
    """``GetGroupMemberListRsp``: code, message, members ``{user id 1, nickname 2, role 3,
    join time 4, name card 5}``, next offset 4, complete 5."""
    fields = Fields.parse(data)
    members = [
        {
            "user_id": m.text(1),
            "nickname": m.text(2),
            "role": m.num(3),
            "join_time": m.num(4),
            "name_card": m.text(5),
        }
        for m in fields.subs(3)
    ]
    return {
        "code": fields.num(1),
        "message": fields.text(2),
        "members": members,
        "next_offset": fields.num(4),
        "is_complete": fields.num(5) != 0,
    }
