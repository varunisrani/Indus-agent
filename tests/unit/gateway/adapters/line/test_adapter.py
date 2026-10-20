"""P16-23: LINE specifics over the wire fake (the shared contract runs in tests/contract)."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from collections.abc import Awaitable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.gateway.adapters import _http
from indus.gateway.adapters._http import verify_hmac_sha256_base64
from indus.gateway.adapters.base import (
    FINAL_KEY,
    INTERIM_KEY,
    AdapterConnectError,
    ApprovalAction,
    ApprovalPrompt,
    ClarifyPrompt,
    approval_callback,
)
from indus.gateway.adapters.line import SECRET_ENV, TOKEN_ENV, create_adapter
from indus.gateway.adapters.line.adapter import (
    ANSWER_PREFIX,
    MEDIA_TTL_S,
    PREVIEW_PNG,
    LineAdapter,
    LineOptions,
    media_base,
)
from indus.gateway.config import AdapterConfig, load_gateway_settings
from indus.gateway.core.events import Attachment, InboundEvent, MessageKind
from indus.gateway.core.send import ErrorKind
from indus.gateway.extras import attach_fetcher
from indus.gateway.intake.media import MediaIntake
from tests.contract.gateway import line_case
from tests.contract.gateway.conftest import settle
from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import FailureKind
from tests.fakes.platform.http_wire import ApiCall, Callback
from tests.fakes.platform.line_wire import LineWire, line_signature

START = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
VECTORS = Path(__file__).resolve().parents[4] / "fixtures" / "gateway" / "crypto_vectors"
USER = line_case.USER_ID
GROUP = line_case.GROUP_ID


def run[T](coro: Awaitable[T]) -> T:
    async def main() -> T:
        return await coro

    return asyncio.run(main())


class Rig:
    """The LINE wire fake, our adapter on it, and what it emitted."""

    def __init__(self, options: LineOptions | None = None) -> None:
        self.clock = FakeClock(START)
        self.env = line_case.make_env(self.clock)
        self.wire: LineWire = self.env.wire
        self.platform = self.env.platform
        self.adapter = line_case.build_adapter(self.env, options)
        self.events: list[InboundEvent] = []
        self.adapter.set_sink(self._sink)

    async def _sink(self, event: InboundEvent) -> None:
        self.events.append(event)

    async def settle[T](self, awaitable: Awaitable[T]) -> T:
        return await settle(self.env, awaitable)

    async def connect(self) -> None:
        await self.settle(self.adapter.connect())

    async def pump(self) -> None:
        await self.wire.pump()
        await self.adapter.listener.idle()

    def bodies(self, calls: list[ApiCall]) -> list[dict[str, Any]]:
        return [c.json() for c in calls]


# -- signatures ---------------------------------------------------------------------------------


def test_signature_vectors_from_the_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    data = json.loads((VECTORS / "line_signature.json").read_text(encoding="utf-8"))
    secret = data["channel_secret"]
    compared: list[bytes] = []
    real = hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        compared.append(a)
        return real(a, b)

    monkeypatch.setattr(_http, "_compare", spy)
    for vector in data["vectors"]:
        body = vector["body"].encode("utf-8")
        assert verify_hmac_sha256_base64(secret, body, vector["signature"]), vector["name"]
        assert line_signature(secret, body) == vector["signature"]
        assert not verify_hmac_sha256_base64(secret, body + b" ", vector["signature"])
    assert len(compared) == 2 * len(data["vectors"])

    # The adapter checks the same signature on the raw body before it parses anything.
    rig = Rig()
    rig.adapter._channel_secret = secret  # pyright: ignore[reportPrivateUsage]
    rig.wire.secret = secret

    async def main() -> list[int]:
        await rig.connect()
        codes: list[int] = []
        for vector in data["vectors"][:3]:
            body = vector["body"].encode("utf-8")
            headers = {"X-Line-Signature": vector["signature"]}
            codes.append(
                (await rig.wire.deliver(Callback("/line/webhook", body, headers))).status_code
            )
        await rig.adapter.listener.idle()
        await rig.adapter.disconnect()
        return codes

    assert run(main()) == [200, 200, 200]


def test_a_bad_or_missing_signature_is_refused_before_parsing() -> None:
    rig = Rig()
    rig.platform.dm(USER, "hello")
    (item,) = rig.platform.drain_inbound()
    event = rig.wire.event(item)
    assert event is not None

    async def main() -> list[int]:
        await rig.connect()
        good = rig.wire.signed([event])
        forged = Callback(good.path, good.body, {**good.headers, "X-Line-Signature": "AAAA"})
        missing = Callback(good.path, good.body, {"Content-Type": "application/json"})
        tampered = Callback(good.path, good.body.replace(b"hello", b"HELLO"), good.headers)
        not_json = rig.wire.signed([])
        not_json = Callback(
            not_json.path, b"{oops", {"X-Line-Signature": line_signature(rig.wire.secret, b"{oops")}
        )
        codes = [(await rig.wire.deliver(c)).status_code for c in (forged, missing, tampered)]
        codes.append((await rig.wire.deliver(not_json)).status_code)
        await rig.adapter.listener.idle()
        await rig.adapter.disconnect()
        return codes

    assert run(main()) == [401, 401, 401, 400]
    assert rig.events == []


# -- reply token, push, batching ----------------------------------------------------------------


def _inbound_then_send(rig: Rig, wait: float, text: str = "the answer") -> Any:
    async def main() -> Any:
        await rig.connect()
        rig.platform.dm(USER, "question")
        await rig.pump()
        rig.clock.advance(wait)
        result = await rig.settle(rig.adapter.send(USER, text))
        await rig.adapter.disconnect()
        return result

    return run(main())


def test_reply_token_at_49_seconds_is_used() -> None:
    rig = Rig()
    result = _inbound_then_send(rig, 49.0)
    assert result.ok
    assert len(rig.wire.reply_calls()) == 1 and rig.wire.push_calls() == []
    assert rig.platform.texts(USER) == ["the answer"]


def test_reply_token_at_51_seconds_falls_back_to_push() -> None:
    rig = Rig()
    result = _inbound_then_send(rig, 51.0)
    assert result.ok
    assert rig.wire.reply_calls() == []
    (push,) = rig.bodies(rig.wire.push_calls())
    assert push["to"] == USER and push["messages"] == [{"type": "text", "text": "the answer"}]


def test_a_token_is_single_use_and_a_refused_token_falls_back_to_push() -> None:
    rig = Rig()
    rig.wire.token_ttl = 10.0  # LINE expired it sooner than we expected

    async def main() -> None:
        await rig.connect()
        rig.platform.dm(USER, "question")
        await rig.pump()
        rig.clock.advance(20)
        assert (await rig.settle(rig.adapter.send(USER, "first"))).ok
        assert (await rig.settle(rig.adapter.send(USER, "second"))).ok
        await rig.adapter.disconnect()

    run(main())
    assert len(rig.wire.reply_calls()) == 1  # tried once, refused
    assert len(rig.wire.push_calls()) == 2
    assert rig.platform.texts(USER) == ["first", "second"]


def test_a_reply_that_timed_out_is_never_pushed_again() -> None:
    rig = Rig()

    async def main() -> Any:
        await rig.connect()
        rig.platform.dm(USER, "question")
        await rig.pump()
        rig.platform.fail(FailureKind.READ_TIMEOUT, op="send")
        result = await rig.settle(rig.adapter.send(USER, "answer"))
        await rig.adapter.disconnect()
        return result

    result = run(main())
    assert not result.ok and result.maybe_delivered and not result.retryable
    assert len(rig.wire.reply_calls()) == 1 and rig.wire.push_calls() == []


def _paragraphs(count: int, size: int = 4000) -> str:
    return "\n".join(f"p{n:02d}" + "x" * (size - 3) for n in range(count))


def test_twelve_bubbles_go_out_in_three_calls() -> None:
    rig = Rig()
    text = _paragraphs(12)

    async def main() -> Any:
        await rig.connect()
        rig.platform.dm(USER, "long one please")
        await rig.pump()
        result = await rig.settle(rig.adapter.send(USER, text))
        await rig.adapter.disconnect()
        return result

    result = run(main())
    assert result.ok and len(result.ids) == 12
    (reply,) = rig.bodies(rig.wire.reply_calls())
    pushes = rig.bodies(rig.wire.push_calls())
    assert [len(reply["messages"])] + [len(p["messages"]) for p in pushes] == [5, 5, 2]
    texts = rig.platform.texts(USER)
    assert len(texts) == 12 and "\n".join(texts) == text
    assert all(len(t) <= 4500 for t in texts)
    assert set(result.ids) == {m.message_id for m in rig.platform.messages(USER)}


def test_a_failed_later_call_reports_what_landed_and_the_remainder() -> None:
    rig = Rig()
    text = _paragraphs(8)

    async def main() -> Any:
        await rig.connect()
        rig.platform.fail(FailureKind.CONNECT_TIMEOUT, op="send", after=6)
        result = await rig.settle(rig.adapter.send(GROUP, text))
        await rig.adapter.disconnect()
        return result

    result = run(main())
    assert not result.ok and result.partial and result.retryable
    assert len(result.ids) == 5
    assert result.remainder is not None and result.remainder.startswith("p05")
    # the refused call was taken back whole: only the first five bubbles are visible
    assert [t[:3] for t in rig.platform.texts(GROUP)] == ["p00", "p01", "p02", "p03", "p04"]


def test_a_long_server_wait_is_handed_back_and_the_monthly_quota_is_not_retried() -> None:
    rig = Rig()

    async def main() -> tuple[Any, Any, float]:
        await rig.connect()
        before = rig.clock.monotonic()
        rig.platform.fail(FailureKind.RATE_LIMIT, op="send", retry_after=30)
        flood = await rig.settle(rig.adapter.send_with_retry(GROUP, "x", rng=lambda: 0.0))
        waited = rig.clock.monotonic() - before
        quota = rig.adapter._status_failure(  # pyright: ignore[reportPrivateUsage]
            _line_error(429, {"message": "You have reached your monthly limit."})
        )
        await rig.adapter.disconnect()
        return flood, quota, waited

    flood, quota, waited = run(main())
    assert flood.is_flood and flood.retry_after == 30 and waited < 30
    assert not quota.retryable and quota.kind is ErrorKind.UNKNOWN


def _line_error(status: int, body: dict[str, Any]) -> Any:
    from indus.gateway.adapters.line.api import LineError

    return LineError(status, json.dumps(body), {})


def test_a_quoted_reply_carries_the_quote_token() -> None:
    rig = Rig()

    async def main() -> None:
        await rig.connect()
        rig.platform.dm(USER, "quote me")
        await rig.pump()
        message_id = rig.events[0].message_id
        assert message_id is not None
        await rig.settle(rig.adapter.send(USER, "quoted", reply_to=message_id))
        await rig.adapter.disconnect()

    run(main())
    (reply,) = rig.bodies(rig.wire.reply_calls())
    assert reply["messages"][0]["quoteToken"].startswith("q")


# -- prompts ------------------------------------------------------------------------------------


def test_approval_and_clarify_cards_fit_line_limits() -> None:
    rig = Rig()
    prompt = ApprovalPrompt(
        request_id="req9",
        session_key="k",
        text="Run this? " + "rm -rf build " * 20,
        fallback_text="Run this?\nReply /approve or /deny.",
        actions=(
            ApprovalAction("Allow once", "once"),
            ApprovalAction("Allow for this session", "session"),
            ApprovalAction("Always allow", "always"),
            ApprovalAction("Deny", "deny"),
        ),
    )
    many = ClarifyPrompt("c1", "k", "Pick", ("a", "b", "c", "d", "e"), fallback_text="1. a\n2. b")

    async def main() -> tuple[Any, Any]:
        await rig.connect()
        card = await rig.settle(rig.adapter.send_approval(USER, prompt))
        text = await rig.settle(rig.adapter.send_clarify(USER, many))
        await rig.adapter.disconnect()
        return card, text

    card, text = run(main())
    assert card.ok and text.ok
    first, second = rig.bodies(rig.wire.push_calls())
    kinds = [m["type"] for m in first["messages"]]
    assert kinds == ["text", "template"]  # the long command is spelled out above the card
    actions = first["messages"][1]["template"]["actions"]
    assert [a["data"] for a in actions] == [
        approval_callback("req9", c) for c in ("once", "session", "always", "deny")
    ]
    assert all(len(a["label"]) <= 20 for a in actions)
    assert second["messages"] == [{"type": "text", "text": "1. a\n2. b"}]


# -- media --------------------------------------------------------------------------------------


def test_media_url_expires_after_its_ttl(tmp_path: Path) -> None:
    rig = Rig()
    image = tmp_path / "chart.png"
    image.write_bytes(b"\x89PNG fake image bytes")

    async def main() -> tuple[int, bytes, int, int]:
        await rig.connect()
        result = await rig.settle(rig.adapter.send_image(GROUP, str(image), caption="the chart"))
        assert result.ok
        (push,) = rig.bodies(rig.wire.push_calls())
        image_msg, caption = push["messages"]
        assert caption == {"type": "text", "text": "the chart"}
        url = image_msg["originalContentUrl"]
        assert (
            url.startswith(line_case.MEDIA_BASE + "/line/media?")
            and image_msg["previewImageUrl"] == url
        )
        fresh = await rig.wire.fetch(url)
        rig.clock.advance(MEDIA_TTL_S - 1)
        still = await rig.wire.fetch(url)
        rig.clock.advance(1)
        gone = await rig.wire.fetch(url)
        unknown = await rig.wire.fetch(line_case.MEDIA_BASE + "/line/media?t=nope")
        await rig.adapter.disconnect()
        return (
            fresh.status_code,
            fresh.content,
            still.status_code,
            gone.status_code * 1000 + (unknown.status_code),
        )

    status, content, still, gone = run(main())
    assert status == 200 and content == image.read_bytes() and still == 200
    assert gone == 410_404


def test_video_gets_a_generated_preview_and_files_are_refused(tmp_path: Path) -> None:
    rig = Rig()
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake mp4")
    gif = tmp_path / "anim.gif"
    gif.write_bytes(b"GIF89a")

    async def main() -> tuple[Any, Any, Any, bytes]:
        await rig.connect()
        sent = await rig.settle(rig.adapter.send_video(GROUP, str(video)))
        (push,) = rig.bodies(rig.wire.push_calls())
        preview = await rig.wire.fetch(push["messages"][0]["previewImageUrl"])
        wrong = await rig.settle(rig.adapter.send_image(GROUP, str(gif)))
        doc = await rig.settle(rig.adapter.send_document(GROUP, str(video)))
        await rig.adapter.disconnect()
        return sent, wrong, doc, preview.content

    sent, wrong, doc, preview = run(main())
    assert sent.ok and preview == PREVIEW_PNG and preview.startswith(b"\x89PNG\r\n\x1a\n")
    assert not wrong.ok and "png" in (wrong.error or "")
    assert doc.unsupported
    assert len(rig.wire.push_calls()) == 1


def test_media_needs_a_public_https_url(tmp_path: Path) -> None:
    rig = Rig(LineOptions(media_base=None))
    image = tmp_path / "a.jpg"
    image.write_bytes(b"jpeg")

    async def main() -> Any:
        await rig.connect()
        result = await rig.settle(rig.adapter.send_image(GROUP, str(image)))
        await rig.adapter.disconnect()
        return result

    result = run(main())
    assert not result.ok and "webhook_public_url" in (result.error or "")
    assert rig.wire.push_calls() == []
    assert media_base("https://t.example/line/webhook", "/line/webhook") == "https://t.example"
    assert media_base("https://t.example/base/line/webhook", "/line/webhook") == (
        "https://t.example/base"
    )
    assert media_base("http://t.example/line/webhook", "/line/webhook") is None
    assert media_base(None, "/line/webhook") is None


def test_the_media_fetcher_feeds_the_intake(scope: Scope) -> None:
    rig = Rig()
    rig.platform.document(USER, USER, "notes.txt")

    async def main() -> tuple[bytes, list[InboundEvent]]:
        await rig.connect()
        await rig.pump()
        intake = MediaIntake(scope)
        undo = attach_fetcher(intake, rig.adapter)
        assert undo is not None
        undo()
        (event,) = rig.events
        data = b"".join([c async for c in rig.adapter.fetch_media(event.media[0])])
        with pytest.raises(ValueError):
            async for _ in rig.adapter.fetch_media(Attachment(ref="/etc/passwd")):
                pass
        await rig.adapter.disconnect()
        return data, rig.events

    data, events = run(main())
    assert data == b"fake bytes of notes.txt"
    assert events[0].kind is MessageKind.DOCUMENT and events[0].media[0].ref.startswith("line:")
    assert events[0].metadata["file_name"] == "notes.txt"


# -- the slow-turn card -------------------------------------------------------------------------


def _card_data(rig: Rig) -> str:
    cards = [m for m in rig.platform.messages(USER) if m.buttons]
    assert len(cards) == 1
    return cards[0].buttons[0].data


def test_a_slow_turn_offers_the_still_working_button() -> None:
    rig = Rig()

    async def main() -> None:
        await rig.connect()
        rig.platform.dm(USER, "think hard")
        await rig.pump()
        await rig.adapter.typing_start(USER)
        await rig.clock.run_until_idle()
        await rig.clock.advance_and_settle(44)
        assert rig.wire.reply_calls() == []
        await rig.clock.advance_and_settle(1)
        assert len(rig.wire.reply_calls()) == 1
        data = _card_data(rig)
        assert data.startswith(ANSWER_PREFIX)
        progress = await rig.settle(
            rig.adapter.send(USER, "still searching", meta={INTERIM_KEY: True})
        )
        held = await rig.settle(rig.adapter.send(USER, "the answer", meta={FINAL_KEY: True}))
        assert progress.ok and held.ok
        assert "the answer" not in rig.platform.texts(USER)
        rig.platform.button_press(USER, USER, data)
        await rig.pump()
        await rig.adapter.disconnect()

    run(main())
    replies = rig.bodies(rig.wire.reply_calls())
    assert len(replies) == 2  # the card, then the answer on the tap's fresh token
    assert replies[1]["messages"] == [{"type": "text", "text": "the answer"}]
    assert [b["messages"][0]["text"] for b in rig.bodies(rig.wire.push_calls())] == [
        "still searching"
    ]
    assert rig.events[-1].text == "think hard"  # the tap itself is not an inbound event


def test_a_held_answer_is_pushed_when_nobody_taps_and_a_fast_turn_gets_no_card() -> None:
    rig = Rig()

    async def main() -> None:
        await rig.connect()
        rig.platform.dm(USER, "slow")
        await rig.pump()
        await rig.adapter.typing_start(USER)
        await rig.clock.run_until_idle()
        await rig.clock.advance_and_settle(45)
        await rig.settle(rig.adapter.send(USER, "late answer", meta={FINAL_KEY: True}))
        await rig.clock.advance_and_settle(599)
        assert "late answer" not in rig.platform.texts(USER)
        await rig.clock.advance_and_settle(1)
        assert "late answer" in rig.platform.texts(USER)
        # a fast turn: the answer goes out before the threshold, so no card
        rig.platform.dm(USER, "fast")
        await rig.pump()
        await rig.adapter.typing_start(USER)
        await rig.clock.run_until_idle()
        await rig.clock.advance_and_settle(5)
        await rig.settle(rig.adapter.send(USER, "quick", meta={FINAL_KEY: True}))
        await rig.clock.advance_and_settle(60)
        await rig.adapter.disconnect()

    run(main())
    cards = [m for m in rig.platform.messages(USER) if m.buttons]
    assert len(cards) == 1
    assert rig.platform.texts(USER)[-1] == "quick"


def test_zero_turns_the_card_off_and_a_stale_tap_is_answered() -> None:
    rig = Rig(LineOptions(slow_reply_s=0, media_base=None))

    async def main() -> None:
        await rig.connect()
        rig.platform.dm(USER, "slow")
        await rig.pump()
        await rig.adapter.typing_start(USER)
        await rig.clock.run_until_idle()
        await rig.clock.advance_and_settle(46)
        rig.platform.button_press(USER, USER, ANSWER_PREFIX + "deadbeef")
        await rig.pump()
        await rig.adapter.disconnect()

    run(main())
    assert not [m for m in rig.platform.messages(USER) if m.buttons]
    assert rig.platform.texts(USER) == ["That answer is no longer waiting here."]


# -- inbound ------------------------------------------------------------------------------------


def test_inbound_normalisation_and_drops() -> None:
    rig = Rig()
    wire = rig.wire

    def event(kind: str, source: dict[str, Any], **fields: Any) -> dict[str, Any]:
        n = len(wire.reply_tokens) + 1
        return {
            "type": kind,
            "mode": fields.pop("mode", "active"),
            "timestamp": 1767258000000,
            "source": source,
            "webhookEventId": fields.pop("event_id", f"ev{n}"),
            "replyToken": wire.issue_token(str(source.get("groupId") or source.get("userId"))),
            **fields,
        }

    user = {"type": "user", "userId": USER}
    room = {"type": "room", "roomId": "R" + "c" * 32, "userId": USER}
    mention = {
        "mentionees": [
            {"index": 3, "length": 9, "type": "user", "userId": line_case.BOT_ID, "isSelf": True}
        ]
    }

    async def main() -> None:
        await rig.connect()
        sent = await rig.settle(rig.adapter.send(USER, "earlier answer"))
        assert sent.message_id is not None
        batch = [
            event(
                "message",
                room,
                message={
                    "id": "1",
                    "type": "text",
                    "text": "hi @IndusBot what",
                    "mention": mention,
                },
            ),
            event(
                "message", user, message={"id": "2", "type": "text", "text": "x"}, mode="standby"
            ),
            event(
                "message", user, message={"id": "3", "type": "text", "text": "dup"}, event_id="d"
            ),
            event(
                "message", user, message={"id": "4", "type": "text", "text": "dup"}, event_id="d"
            ),
            event(
                "message",
                {"type": "user", "userId": line_case.BOT_ID},
                message={"id": "5", "type": "text", "text": "me"},
            ),
            event(
                "message",
                user,
                message={
                    "id": "6",
                    "type": "text",
                    "text": "about that",
                    "quotedMessageId": sent.message_id,
                },
            ),
            event("message", user, message={"id": "7", "type": "sticker", "keywords": ["happy"]}),
            event(
                "message",
                user,
                message={
                    "id": "8",
                    "type": "location",
                    "title": "Office",
                    "address": "1 Main St",
                    "latitude": 1.5,
                    "longitude": 2.5,
                },
            ),
            event("follow", user),
        ]
        await rig.wire.deliver(rig.wire.signed(batch))
        await rig.adapter.listener.idle()
        await rig.adapter.disconnect()

    run(main())
    texts = [e.text for e in rig.events]
    assert texts == [
        "hi what",
        "dup",
        "about that",
        "(sticker: happy)",
        "Office 1 Main St (1.5, 2.5)",
    ]
    room_event = rig.events[0]
    assert room_event.source.chat_type == "group" and room_event.metadata["mentioned"] is True
    assert room_event.source.chat_id.startswith("R")
    quoted = rig.events[2]
    assert quoted.reply_to_is_own and quoted.reply_to_id is not None
    assert rig.events[3].kind is MessageKind.STICKER
    assert rig.events[4].kind is MessageKind.LOCATION


# -- wiring -------------------------------------------------------------------------------------

TOKEN = line_case.TOKEN
SECRET = line_case.SECRET


def _config(scope: Scope, *, send_only: bool = False, extra: str = "") -> AdapterConfig:
    secrets: Any = scope.secrets
    secrets.set(TOKEN_ENV, TOKEN)
    secrets.set(SECRET_ENV, SECRET)
    if extra:
        from tests.unit.cli.commands.cmdrun import write_settings

        write_settings(scope, extra)
    settings = load_gateway_settings(scope).get("line")
    assert settings is not None and settings.enabled
    return AdapterConfig("line", settings, scope, scope.clock, None, send_only=send_only)


def test_create_adapter_reads_credentials_and_options(scope: Scope) -> None:
    config = _config(
        scope,
        extra=(
            "platforms:\n  line:\n    webhook_public_url: https://t.example/line/webhook\n"
            "    slow_reply_seconds: 30\n"
        ),
    )
    adapter = create_adapter(config)
    assert isinstance(adapter, LineAdapter)
    assert adapter.listener.settings.port == 8646 and adapter.listener.settings.host == "127.0.0.1"
    assert adapter.listener.settings.path == "/line/webhook"
    assert adapter.options.slow_reply_s == 30 and adapter.options.media_base == "https://t.example"
    for secret in (TOKEN, SECRET):
        assert secret not in repr(adapter) and secret not in repr(adapter.api)
        assert secret not in adapter.scrub(f"boom {secret}")


def test_create_adapter_refuses_missing_credentials_and_plain_http(scope: Scope) -> None:
    settings = load_gateway_settings(scope).get("telegram")
    assert settings is not None
    with pytest.raises(ValueError, match="LINE_CHANNEL_ACCESS_TOKEN"):
        create_adapter(AdapterConfig("line", settings, scope, scope.clock, None))
    config = _config(scope, extra="platforms:\n  line:\n    api_base_url: http://x.example\n")
    with pytest.raises(ValueError, match="https"):
        create_adapter(config)


def test_indus_send_opens_line_send_only(scope: Scope, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock(START)
    env = line_case.make_env(clock)
    wire: LineWire = env.wire
    monkeypatch.setattr("indus.gateway.adapters.line.CLIENT_FACTORY", lambda _config: wire.client())
    adapter = create_adapter(_config(scope, send_only=True))

    async def main() -> Any:
        await adapter.connect()
        listening = adapter.listener.accepting
        result = await adapter.send(GROUP, "deploy done")
        media = await adapter.send_image(GROUP, __file__)
        assert media.unsupported  # nobody would serve the file to LINE
        await adapter.disconnect()
        return listening, result

    listening, result = run(main())
    assert not listening and result.ok
    assert [c.path for c in wire.calls] == ["/v2/bot/info", "/v2/bot/message/push"]
    assert env.platform.texts(GROUP) == ["deploy done"]


def test_a_token_revoked_while_running_reports_the_fatal_state() -> None:
    rig = Rig()
    fatal: list[tuple[str, str]] = []
    rig.adapter.set_fatal_listener(lambda code, message: fatal.append((code, message)))

    async def main() -> Any:
        await rig.connect()
        rig.wire.token = "another-token"
        result = await rig.settle(rig.adapter.send(USER, "hello"))
        await rig.adapter.disconnect()
        return result

    result = run(main())
    assert not result.ok and rig.platform.texts(USER) == []
    assert [code for code, _ in fatal] == ["line_auth"]
    assert TOKEN not in fatal[0][1]


def test_a_refused_token_is_fatal_and_never_shown(caplog: pytest.LogCaptureFixture) -> None:
    rig = Rig()
    rig.wire.token = "another-token"

    async def main() -> AdapterConnectError:
        with pytest.raises(AdapterConnectError) as caught:
            await rig.connect()
        return caught.value

    with caplog.at_level(logging.DEBUG):
        error = run(main())
    assert not error.retryable and error.code == "line_auth"
    assert TOKEN not in str(error) and TOKEN not in caplog.text and SECRET not in caplog.text
