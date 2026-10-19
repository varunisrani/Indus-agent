"""The adapter base contract: defaults, guards and capability flags (P15-05)."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, ClassVar

import pytest

from indus.gateway.adapters.base import (
    CALLBACK_MAX_BYTES,
    Adapter,
    AdapterConnectError,
    ApprovalAction,
    ApprovalPrompt,
    Capabilities,
    ChatInfo,
    ClarifyPrompt,
    MediaKind,
    approval_callback,
    clarify_callback,
    code_points,
    parse_approval_callback,
    parse_clarify_callback,
    utf16_units,
)
from indus.gateway.core.events import InboundEvent, Source
from indus.gateway.core.send import ErrorKind, SendResult
from tests.fakes.clock import FakeClock

# Built at runtime: never a credential-shaped literal in the source (builder rules).
TOKEN = "".join(["12345", ":", "AAF", "x" * 30])


def _clock() -> FakeClock:
    return FakeClock(datetime(2026, 1, 1, tzinfo=UTC))


class Plain(Adapter):
    """Only the required hooks: every optional operation keeps the base default."""

    platform: ClassVar[str] = "plain"

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.calls: list[tuple[Any, ...]] = []
        self.open_error: BaseException | None = None
        self.send_error: BaseException | None = None

    async def _open(self, is_reconnect: bool) -> None:
        self.calls.append(("open", is_reconnect))
        if self.open_error is not None:
            raise self.open_error

    async def _close(self) -> None:
        self.calls.append(("close",))

    async def _send(
        self, chat_id: str, text: str, reply_to: str | None, meta: dict[str, Any]
    ) -> SendResult:
        self.calls.append(("send", chat_id, text, reply_to, dict(meta)))
        if self.send_error is not None:
            raise self.send_error
        return SendResult.sent(f"m{len(self.calls)}")


class Rich(Plain):
    platform: ClassVar[str] = "rich"
    capabilities: ClassVar[Capabilities] = Capabilities(
        supports_edit=True, supports_delete=True, supports_buttons=True, slot_spacing=1.0
    )

    async def _edit(
        self, chat_id: str, message_id: str, text: str, final: bool, meta: dict[str, Any]
    ) -> SendResult:
        self.calls.append(("edit", message_id, text, final))
        return SendResult.sent(message_id)

    async def _delete(self, chat_id: str, message_id: str) -> bool:
        self.calls.append(("delete", message_id))
        return True

    async def _send_approval(
        self, chat_id: str, prompt: ApprovalPrompt, meta: dict[str, Any]
    ) -> SendResult:
        self.calls.append(("buttons", [a.choice for a in prompt.actions]))
        return SendResult.sent("card")

    async def _send_clarify(
        self, chat_id: str, prompt: ClarifyPrompt, meta: dict[str, Any]
    ) -> SendResult:
        self.calls.append(("clarify-buttons", prompt.choices))
        return SendResult.sent("clr")

    async def _chat_info(self, chat_id: str) -> ChatInfo:
        return ChatInfo(name="General", type="group")


PROMPT = ApprovalPrompt(
    request_id="r1",
    session_key="k",
    text="Run `rm -rf build`?",
    fallback_text="Run `rm -rf build`? Reply /approve or /deny.",
    actions=(ApprovalAction("Once", "once"), ApprovalAction("Deny", "deny", "danger")),
)


def test_default_capabilities() -> None:
    caps = Capabilities()
    assert caps.max_len == 4096 and caps.len_fn is code_points
    assert not (caps.splits_natively or caps.supports_edit or caps.supports_delete)
    assert not (caps.supports_buttons or caps.owns_access_policy or caps.auth_upstream)
    assert caps.dm_is_private and caps.push_capable
    assert caps.slot_spacing == 0.0 and caps.inline_wait_cap == 60.0
    assert Plain.vendor_names == {}


def test_length_functions() -> None:
    assert code_points("a😀") == 2
    assert utf16_units("a😀") == 3
    assert utf16_units("é") == 1


def test_lifecycle_is_idempotent() -> None:
    async def main() -> Plain:
        adapter = Plain(clock=_clock())
        await adapter.disconnect()  # before connect: nothing
        await adapter.connect()
        await adapter.connect()
        assert adapter.connected
        await adapter.disconnect()
        await adapter.disconnect()
        await adapter.connect(is_reconnect=True)
        return adapter

    adapter = asyncio.run(main())
    assert adapter.calls == [("open", False), ("close",), ("open", True)]


def test_open_failure_is_typed_and_scrubbed() -> None:
    async def main() -> None:
        adapter = Plain(clock=_clock(), secrets=[TOKEN])
        adapter.open_error = RuntimeError(f"GET https://api.example/bot{TOKEN}/getMe failed")
        with pytest.raises(AdapterConnectError) as info:
            await adapter.connect()
        assert info.value.retryable
        assert TOKEN not in str(info.value)
        assert not adapter.connected
        adapter.open_error = AdapterConnectError("bad token", retryable=False, code="auth")
        with pytest.raises(AdapterConnectError) as fatal:
            await adapter.connect()
        assert not fatal.value.retryable and fatal.value.code == "auth"

    asyncio.run(main())


def test_send_exceptions_become_scrubbed_classified_results() -> None:
    async def main() -> SendResult:
        adapter = Plain(clock=_clock(), secrets=[TOKEN])
        adapter.send_error = ConnectionRefusedError(f"connect to /bot{TOKEN}/sendMessage refused")
        return await adapter.send("c1", "hi")

    result = asyncio.run(main())
    assert not result.ok and result.kind is ErrorKind.TRANSIENT and result.retryable
    assert result.error is not None and TOKEN not in result.error


def test_blank_text_is_not_sent() -> None:
    adapter = Plain(clock=_clock())
    result = asyncio.run(adapter.send("c", "  \n"))
    assert not result.ok and adapter.calls == []


def test_optional_operations_are_refused_without_reaching_the_platform() -> None:
    async def main() -> Plain:
        adapter = Plain(clock=_clock())
        assert (await adapter.edit("c", "m", "x")).unsupported
        assert (await adapter.edit("c", "m", "x", final=True)).unsupported
        assert await adapter.delete("c", "m") is False
        for send in (adapter.send_image, adapter.send_voice, adapter.send_document):
            assert (await send("c", "/tmp/a")).unsupported
        assert (await adapter.send_notice_private("c", "u", "psst")).unsupported
        await adapter.typing_start("c")
        await adapter.typing_stop("c")
        assert await adapter.chat_info("c") == ChatInfo(name="c")
        assert adapter.format("**x**") == "x"  # plain markup by default (P15-15)
        return adapter

    assert asyncio.run(main()).calls == []


def test_approval_and_clarify_fall_back_to_text_without_buttons() -> None:
    async def main() -> Plain:
        adapter = Plain(clock=_clock())
        await adapter.send_approval("c", PROMPT)
        prompt = ClarifyPrompt("q1", "k", "Which?", ("a", "b"), fallback_text="Which?\n1. a\n2. b")
        await adapter.send_clarify("c", prompt)
        await adapter.send_clarify("c", ClarifyPrompt("q2", "k", "Free answer?"))
        return adapter

    texts = [c[2] for c in asyncio.run(main()).calls]
    assert texts == [PROMPT.fallback_text, "Which?\n1. a\n2. b", "Free answer?"]


def test_buttons_used_when_supported() -> None:
    async def main() -> Rich:
        adapter = Rich(clock=_clock())
        await adapter.send_approval("c", PROMPT)
        # Another chat: the same chat would wait out Rich's 1.0 s spacing on the fake clock.
        await adapter.send_clarify("d", ClarifyPrompt("q", "k", "Which?", ("a", "b")))
        assert await adapter.chat_info("c") == ChatInfo("General", "group")
        return adapter

    assert asyncio.run(main()).calls == [
        ("buttons", ["once", "deny"]),
        ("clarify-buttons", ("a", "b")),
    ]


def test_edits_share_the_send_slot() -> None:
    async def main() -> Rich:
        clock = _clock()
        adapter = Rich(clock=clock)
        await adapter.send("c", "first")
        assert (await adapter.edit("c", "m1", "interim")).skipped  # spacing not passed
        assert (await adapter.edit("c", "m1", "final", final=True)).ok
        await clock.advance_and_settle(1.0)
        assert (await adapter.edit("c", "m1", "later interim")).ok
        return adapter

    adapter = asyncio.run(main())
    edits = [c for c in adapter.calls if c[0] == "edit"]
    assert edits == [("edit", "m1", "final", True), ("edit", "m1", "later interim", False)]


def test_emit_hands_events_to_the_sink_and_survives_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    event = InboundEvent(source=Source(platform="plain", chat_id="c"), text="hi")
    seen: list[InboundEvent] = []

    async def sink(e: InboundEvent) -> None:
        seen.append(e)
        if len(seen) == 2:
            raise RuntimeError("pipeline bug")

    async def main() -> None:
        adapter = Plain(clock=_clock())
        with caplog.at_level(logging.WARNING):
            await adapter.emit(event)
            await adapter.emit(event)
        assert sum("no handler" in r.message for r in caplog.records) == 1
        adapter.set_sink(sink)
        await adapter.emit(event)
        await adapter.emit(event)  # the sink raises: logged, not raised

    asyncio.run(main())
    assert seen == [event, event]


def test_send_with_retry_uses_the_adapters_cap() -> None:
    class Flooded(Plain):
        capabilities: ClassVar[Capabilities] = Capabilities(inline_wait_cap=5.0)

        async def _send(
            self, chat_id: str, text: str, reply_to: str | None, meta: dict[str, Any]
        ) -> SendResult:
            self.calls.append(("send", text, meta.get("plain", False)))
            return SendResult.failure("Too Many Requests: retry after 6", retry_after=6)

    async def main() -> tuple[SendResult, Flooded, FakeClock]:
        clock = _clock()
        adapter = Flooded(clock=clock)
        return await adapter.send_with_retry("c", "hi", rng=lambda: 0.0), adapter, clock

    result, adapter, clock = asyncio.run(main())
    assert result.is_flood and clock.monotonic() == 0.0
    assert adapter.calls == [("send", "hi", False)]


def test_plain_meta_reaches_the_adapter_on_fallback() -> None:
    class BadMarkup(Plain):
        async def _send(
            self, chat_id: str, text: str, reply_to: str | None, meta: dict[str, Any]
        ) -> SendResult:
            self.calls.append(("send", meta.get("plain", False)))
            if not meta.get("plain"):
                return SendResult.failure("Bad Request: can't parse entities")
            return SendResult.sent("p")

    async def main() -> BadMarkup:
        adapter = BadMarkup(clock=_clock())
        assert (await adapter.send_with_retry("c", "*x", meta={"thread_id": "t"})).ok
        return adapter

    assert asyncio.run(main()).calls == [("send", False), ("send", True)]


def test_callback_data_round_trip_and_cap() -> None:
    data = approval_callback("req-1", "session")
    assert data == "apv:req-1:session"
    assert parse_approval_callback(data) == ("req-1", "session")
    assert parse_approval_callback("apv:req-1:sometimes") is None
    assert parse_approval_callback("clr:x:1") is None
    assert clarify_callback("q.9", 3) == "clr:q.9:3"
    assert parse_clarify_callback("clr:q.9:3") == ("q.9", 3)
    assert parse_clarify_callback("clr:q:x") is None
    with pytest.raises(ValueError):
        approval_callback("a" * 41, "once")
    with pytest.raises(ValueError):
        approval_callback("bad id", "once")
    longest = approval_callback("a" * 40, "session")
    assert len(longest.encode()) <= CALLBACK_MAX_BYTES


def test_repr_and_settings_credentials_are_masked() -> None:
    from indus.foundation.config.schema import PlatformSection
    from indus.gateway.config import PlatformSettings

    settings = PlatformSettings(
        "plain", True, "credentials", PlatformSection(), None, {"X_TOKEN": TOKEN}
    )
    adapter = Plain(clock=_clock(), settings=settings)
    assert TOKEN not in repr(adapter) and TOKEN not in repr(settings)
    assert TOKEN not in adapter.scrub(f"url /bot{TOKEN}/x")


class Marked(Rich):
    capabilities: ClassVar[Capabilities] = Capabilities(supports_edit=True, markup="markdownv2")

    async def _send_media(
        self, kind: MediaKind, chat_id: str, path: str, caption: str | None, meta: dict[str, Any]
    ) -> SendResult:
        self.calls.append(("media", kind, path, dict(meta)))
        return SendResult.sent("f1")


def test_outbound_marks_never_reach_the_hooks() -> None:
    """The egress doors mark interim/final (P15-16); hooks get the metadata without them, and a
    ``final`` mark on an edit counts as a final edit (never skipped by the slot)."""

    async def main() -> Marked:
        adapter = Marked(clock=_clock())
        await adapter.send("c", "hi", meta={"interim": True, "thread_id": "t"})
        assert (await adapter.edit("c", "m1", "done", meta={"final": True})).ok
        await adapter.send_image("c", "/tmp/p.png", reply_to="in1", meta={"final": True})
        return adapter

    calls = asyncio.run(main()).calls
    assert calls == [
        ("send", "c", "hi", None, {"thread_id": "t"}),
        ("edit", "m1", "done", True),
        ("media", "image", "/tmp/p.png", {"reply_to": "in1"}),
    ]


def test_format_follows_the_markup_capability() -> None:
    adapter = Marked(clock=_clock())
    assert adapter.format("Version 1.2 is **out**!") == "Version 1\\.2 is *out*\\!"
