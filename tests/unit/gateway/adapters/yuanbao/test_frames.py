"""Yuanbao's protobuf frames (P16-21): known bytes, round trips and malformed input."""

from __future__ import annotations

import json

import pytest

from indus.gateway.adapters.yuanbao import frames
from indus.gateway.adapters.yuanbao.frames import (
    Element,
    Fields,
    FrameError,
    Head,
    Push,
    Writer,
)


def test_varints_match_the_protobuf_encoding_guide() -> None:
    # 1, 150 and 300 as the protobuf encoding guide writes them; -1 as ten bytes.
    assert Writer().num(1, 1).build() == bytes([0x08, 0x01])
    assert Writer().num(1, 150).build() == bytes([0x08, 0x96, 0x01])
    assert Writer().num(2, 300).build() == bytes([0x10, 0xAC, 0x02])
    assert Writer().num(1, -1).build() == bytes([0x08] + [0xFF] * 9 + [0x01])
    assert Fields.parse(bytes([0x10, 0xAC, 0x02])).num(2) == 300


def test_a_text_element_has_known_bytes() -> None:
    raw = frames.element_bytes(frames.text_element("hi"))
    expected = bytes([0x0A, 0x0B]) + b"TIMTextElem" + bytes([0x12, 0x04, 0x0A, 0x02]) + b"hi"
    assert raw == expected
    assert frames.decode_element(raw) == Element("TIMTextElem", {"text": "hi"})


def test_a_ping_request_has_known_bytes() -> None:
    raw = frames.encode_frame(Head(frames.REQUEST, "ping", 5, "m1", "conn_access"))
    head = (
        bytes([0x12, 0x04])
        + b"ping"
        + bytes([0x18, 0x05, 0x22, 0x02])
        + b"m1"
        + bytes([0x2A, 0x0B])
        + b"conn_access"
    )
    assert raw == bytes([0x0A, len(head)]) + head
    decoded = frames.decode_frame(raw)
    assert decoded.head == Head(frames.REQUEST, "ping", 5, "m1", "conn_access")
    assert decoded.data == b""


def test_frames_round_trip_with_status_ack_and_data() -> None:
    head = Head(frames.PUSH, "inbound_message", 9, "p-1", frames.BIZ_MODULE, True, -3)
    decoded = frames.decode_frame(frames.encode_frame(head, b"\x0a\x01x"))
    assert decoded.head == head and decoded.data == b"\x0a\x01x"


def test_unknown_and_fixed_width_fields_are_skipped() -> None:
    data = (
        Writer().text(1, "a").build()
        + bytes([0x1D, 1, 2, 3, 4])  # field 3, 32-bit
        + bytes([0x21, 1, 2, 3, 4, 5, 6, 7, 8])  # field 4, 64-bit
        + Writer().num(99, 7).build()
    )
    fields = Fields.parse(data)
    assert fields.text(1) == "a" and fields.num(99) == 7 and fields.raw(3) == b""


@pytest.mark.parametrize(
    "data",
    [
        bytes([0x0A, 0x05, 0x61]),  # length past the end
        bytes([0x08, 0x80]),  # varint past the end
        bytes([0x08] + [0x80] * 10 + [0x01]),  # varint longer than ten bytes
        bytes([0x0B]),  # wire type 3 (groups) is not supported
        bytes([0x00, 0x01]),  # field number 0
    ],
)
def test_malformed_input_raises_frame_error(data: bytes) -> None:
    with pytest.raises(FrameError):
        Fields.parse(data)


def test_a_frame_without_a_head_is_refused() -> None:
    with pytest.raises(FrameError):
        frames.decode_frame(Writer().raw(2, b"x").build())


def test_pushes_round_trip_with_every_element_kind() -> None:
    image = frames.image_element(
        "https://x.myqcloud.com/a.png", uuid="u", size=10, width=3, height=2, image_format=3
    )
    push = Push(
        from_account="ana",
        sender_nickname="Ana",
        group_code="300",
        group_name="Ops",
        msg_seq=4,
        msg_time=1767258000,
        msg_id="m-9",
        elements=(
            frames.text_element("hello"),
            image,
            frames.file_element("https://x/f", uuid="v", name="f.pdf", size=5),
            frames.face_element({"sticker_id": "278", "name": "六六六"}),
            frames.custom_element({"elem_type": 1002, "text": "@bot", "user_id": "b"}),
        ),
        bot_owner_id="owner",
        private_from_group_code="301",
    )
    back = frames.decode_push(frames.push_bytes(push))
    assert back == push
    assert back.elements[1].get("image_info_array") == [
        {"type": 1, "size": 10, "width": 3, "height": 2, "url": "https://x.myqcloud.com/a.png"}
    ]
    assert back.elements[3].get("index") == 0
    assert json.loads(back.elements[3].get("data")) == {"sticker_id": "278", "name": "六六六"}


def test_json_pushes_accept_both_key_styles() -> None:
    snake = frames.push_from_json(
        {
            "from_account": "ana",
            "group_code": "300",
            "msg_id": "m1",
            "msg_body": [{"msg_type": "TIMTextElem", "msg_content": {"text": "hi"}}],
        }
    )
    pascal = frames.push_from_json(
        {
            "From_Account": "ana",
            "GroupId": "300",
            "MsgKey": "m1",
            "MsgBody": [{"MsgType": "TIMTextElem", "MsgContent": json.dumps({"text": "hi"})}],
        }
    )
    assert snake is not None and pascal is not None
    assert snake.message_id == pascal.message_id == "m1"
    assert snake.elements == pascal.elements == (Element("TIMTextElem", {"text": "hi"}),)
    assert frames.push_from_json({"something": "else"}) is None


def test_auth_bind_and_business_requests_use_the_vendor_field_numbers() -> None:
    body = frames.auth_bind(
        uid="bot",
        source="bot",
        token="t",
        app_version="1",
        os_name="linux",
        instance_id="17",
        bot_version="1",
        route_env="pre",
    )
    fields = Fields.parse(body)
    assert fields.text(1) == "ybBot" and fields.text(5) == "pre"
    who, device = fields.sub(2), fields.sub(3)
    assert (who.text(1), who.text(2), who.text(3)) == ("bot", "bot", "t")
    assert (device.text(1), device.text(2), device.text(10), device.text(24)) == (
        "1",
        "linux",
        "17",
        "1",
    )
    c2c = Fields.parse(
        frames.c2c_message(
            msg_id="c1",
            to_account="ana",
            from_account="bot",
            elements=[frames.text_element("x")],
            group_code="300",
        )
    )
    assert (c2c.text(1), c2c.text(2), c2c.text(3), c2c.text(6)) == ("c1", "ana", "bot", "300")
    assert frames.decode_element(c2c.raw(5)) == frames.text_element("x")
    group = Fields.parse(
        frames.group_message(
            msg_id="g1",
            group_code="300",
            from_account="bot",
            elements=[frames.text_element("y")],
            quote="m7",
        )
    )
    assert (group.text(1), group.text(2), group.text(7)) == ("g1", "300", "m7")
    assert frames.decode_element(group.raw(6)) == frames.text_element("y")
    beat = Fields.parse(frames.group_heartbeat(from_account="b", group_code="3", value=2, at_ms=9))
    assert (beat.text(3), beat.num(4), beat.num(5)) == ("3", 9, 2)
    private = Fields.parse(frames.private_heartbeat(from_account="b", to_account="a", value=1))
    assert (private.text(2), private.num(3)) == ("a", 1)


def test_answers_decode() -> None:
    info = Writer().sub(3, Writer().text(1, "Ops").text(2, "o").text(3, "Owner").num(4, 7))
    assert frames.group_info_answer(info.build()) == {
        "code": 0,
        "message": "",
        "group_name": "Ops",
        "owner_id": "o",
        "owner_nickname": "Owner",
        "member_count": 7,
    }
    members = (
        Writer().sub(3, Writer().text(1, "u1").text(2, "Ana").num(3, 2)).num(4, 0).num(5, 1).build()
    )
    answer = frames.member_list_answer(members)
    assert answer["members"] == [
        {"user_id": "u1", "nickname": "Ana", "role": 2, "join_time": 0, "name_card": ""}
    ]
    assert answer["is_complete"] is True
    assert frames.result_answer(b"") == (0, "")
    assert frames.result_answer(Writer().num(1, 4).text(2, "no").build()) == (4, "no")
    assert frames.auth_bind_answer(Writer().text(3, "c-1").build()) == (0, "", "c-1")
