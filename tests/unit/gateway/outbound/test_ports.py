"""The egress doors on the real adapter base (P15 wave 1 integration of P15-05 and P15-16..18)."""

from __future__ import annotations

import asyncio

from indus.gateway.core.send import SendResult
from indus.gateway.outbound import ports
from tests.unit.gateway.outbound.recording import fail, recording_adapter


def test_marks_reach_the_door_but_never_the_wire() -> None:
    adapter = recording_adapter()

    async def main() -> None:
        first = await ports.send_text(adapter, "c", "hi", final=False, meta={"thread_id": "t"})
        assert first.ok and first.message_id is not None
        await ports.edit_text(adapter, "c", first.message_id, "hi there", final=True)
        await ports.send_media_file(adapter, "c", "image", "/x.png", reply_to="in1")

    asyncio.run(main())
    assert [(op.op, op.interim, op.final) for op in adapter.ops] == [
        ("send", True, False),
        ("edit", False, True),
        ("image", False, False),
    ]
    assert adapter.ops[-1].reply_to == "in1"
    assert adapter.wire_meta == [{"thread_id": "t"}, {}, {"reply_to": "in1"}]


def test_media_kinds_map_to_native_methods_with_a_document_fallback() -> None:
    adapter = recording_adapter()

    async def main() -> list[SendResult]:
        return [
            await ports.send_media_file(adapter, "c", kind, f"/f.{kind}")
            for kind in ("voice", "video", "audio", "sticker")
        ]

    results = asyncio.run(main())
    assert all(r.ok for r in results)
    assert [(op.op, op.text) for op in adapter.ops] == [
        ("voice", "/f.voice"),
        ("document", "/f.video"),
        ("document", "/f.audio"),
        ("document", "/f.sticker"),
    ]


def test_result_readings_use_the_classifier() -> None:
    assert ports.chat_unreachable(fail("not_found", "Bad Request: chat not found"))
    assert not ports.chat_unreachable(fail("not_found", "Bad Request: message thread not found"))
    assert ports.chat_unreachable(fail("forbidden", "Forbidden: bot was kicked"))
    assert ports.is_flood(fail("rate_limited", retry_after=3.0))
    assert ports.is_bad_format(fail("bad_format", "can't parse entities"))
    # an interim edit dropped by a busy send slot is not a failure (P15-05 → P15-16)
    skipped = SendResult.skip()
    assert skipped.ok and skipped.skipped and not ports.is_flood(skipped)


def test_capabilities_drive_the_formatter_and_lengths() -> None:
    adapter = recording_adapter(max_len=80, markup="markdownv2", supports_delete=False)
    assert ports.max_length(adapter) == 80 and ports.formatter(adapter).markup == "markdownv2"
    assert ports.platform_of(adapter) == "stub"
    assert asyncio.run(ports.delete_message(adapter, "c", "m1")) is False
    assert adapter.ops == []
