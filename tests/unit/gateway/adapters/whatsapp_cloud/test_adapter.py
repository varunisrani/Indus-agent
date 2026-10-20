"""P16-04: the WhatsApp Business Cloud adapter over the fake Graph API and signed webhooks."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from indus.foundation.config.schema import PlatformSection
from indus.foundation.scope import Scope
from indus.gateway.adapters import _http
from indus.gateway.adapters import whatsapp_cloud as cloud_mod
from indus.gateway.adapters.base import (
    AdapterConnectError,
    ApprovalAction,
    ApprovalPrompt,
    ClarifyPrompt,
    approval_callback,
    clarify_callback,
)
from indus.gateway.adapters.whatsapp_cloud import create_adapter
from indus.gateway.adapters.whatsapp_cloud.adapter import (
    DEFAULT_PATH,
    GRAPH_ROUTES,
    MAX_BODY_BYTES,
    WhatsAppCloudAdapter,
    mask_number,
)
from indus.gateway.adapters.whatsapp_cloud.markup import to_whatsapp
from indus.gateway.config import AdapterConfig, PlatformSettings
from indus.gateway.core.events import Attachment, InboundEvent, MessageKind
from indus.gateway.core.send import ErrorKind
from indus.gateway.delivery import send_command
from indus.gateway.outbound.sender import FinalReply, OutboundSender
from indus.gateway.registry import PlatformEntry, PlatformRegistry
from tests.contract.gateway.conftest import CLOCK_START
from tests.contract.gateway.whatsapp_cloud_case import build, make_env
from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import FailureKind
from tests.fakes.platform.http_wire import Callback
from tests.fakes.platform.whatsapp_cloud_wire import (
    APP_SECRET,
    BUSINESS_NUMBER,
    PHONE_NUMBER_ID,
    TOKEN,
    VERIFY_TOKEN,
    VERSION,
    WhatsAppCloudWire,
)

ANA = "15550000200"
BEN = "15550000300"


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


class Rig:
    """The adapter over the fake Graph API, its events collected."""

    def __init__(self, *, send_only: bool = False, **cloud: str) -> None:
        self.clock = FakeClock(CLOCK_START)
        env = make_env(self.clock)
        self.platform = env.platform
        self.wire: WhatsAppCloudWire = env.wire
        self.adapter = build(env, send_only=send_only, **cloud)
        self.events: list[InboundEvent] = []

        async def sink(event: InboundEvent) -> None:
            self.events.append(event)

        self.adapter.set_sink(sink)

    async def deliver(self, callback: Callback) -> httpx.Response:
        answer = await self.wire.deliver(callback)
        await self.adapter.listener.idle()
        return answer

    async def inbound(self, *messages: dict[str, Any], **kw: Any) -> httpx.Response:
        return await self.deliver(self.wire.signed(self.wire.envelope(messages, **kw)))


def text_message(sender: str, body: str, wamid: str, **extra: Any) -> dict[str, Any]:
    return {
        "from": sender,
        "id": wamid,
        "timestamp": "1767258000",
        "type": "text",
        "text": {"body": body},
        **extra,
    }


PROMPT = ApprovalPrompt(
    request_id="req1",
    session_key="key",
    text="Run this command?\n```\nrm -rf build\n```",
    fallback_text="Run this command? rm -rf build\nReply /approve or /deny.",
    actions=(
        ApprovalAction("Allow once", "once", "primary"),
        ApprovalAction("Allow for this session", "session"),
        ApprovalAction("Always allow", "always"),
        ApprovalAction("Deny", "deny", "danger"),
    ),
)


# -- webhooks ------------------------------------------------------------------------------------


def test_a_duplicate_wamid_is_ignored() -> None:
    rig = Rig()

    async def main() -> list[int]:
        await rig.adapter.connect()
        once = text_message(ANA, "hello", "wamid.A1")
        statuses = [
            (await rig.inbound(once, once)).status_code,  # twice in one batch
            (await rig.inbound(once)).status_code,  # Meta's retry
            (await rig.inbound(text_message(ANA, "again", "wamid.A2"))).status_code,
        ]
        await rig.adapter.disconnect()
        return statuses

    assert run(main()) == [200, 200, 200]
    assert [e.text for e in rig.events] == ["hello", "again"]
    assert [e.message_id for e in rig.events] == ["wamid.A1", "wamid.A2"]


def test_a_bad_signature_is_401_before_parsing_and_compared_in_constant_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = Rig()
    compared: list[tuple[bytes, bytes]] = []
    real = _http._compare  # pyright: ignore[reportPrivateUsage]

    def spy(offered: bytes, expected: bytes) -> bool:
        compared.append((offered, expected))
        return real(offered, expected)

    monkeypatch.setattr(_http, "_compare", spy)
    body = rig.wire.envelope([text_message(ANA, "forged", "wamid.F1")])

    async def main() -> list[int]:
        await rig.adapter.connect()
        forged = rig.wire.signed(body, secret="not-the-app-secret")
        missing = Callback(DEFAULT_PATH, b"{not json", {"Content-Type": "application/json"})
        garbage = rig.wire.signed(b"{not json", secret="other")
        codes = [(await rig.deliver(c)).status_code for c in (forged, missing, garbage)]
        good_bad_json = (await rig.deliver(rig.wire.signed(b"{not json"))).status_code
        await rig.adapter.disconnect()
        return [*codes, good_bad_json]

    assert run(main()) == [401, 401, 401, 400]
    assert rig.events == []
    assert len(compared) >= 3


def test_the_subscription_handshake() -> None:
    rig = Rig()

    async def main() -> list[tuple[int, str]]:
        await rig.adapter.connect()
        out: list[tuple[int, str]] = []
        for callback in (
            rig.wire.handshake("subscribe", VERIFY_TOKEN, "1158201444"),
            rig.wire.handshake("subscribe", "wrong-token", "1158201444"),
            rig.wire.handshake("unsubscribe", VERIFY_TOKEN, "1158201444"),
            rig.wire.handshake("subscribe", VERIFY_TOKEN, "<script>"),
        ):
            answer = await rig.deliver(callback)
            out.append((answer.status_code, answer.text))
        await rig.adapter.disconnect()
        return out

    answers = run(main())
    assert answers[0] == (200, "1158201444")
    assert [code for code, _ in answers[1:]] == [403, 400, 400]
    assert "<script>" not in answers[3][1]

    unset = Rig(verify_token="")

    async def no_token() -> int:
        await unset.adapter.connect()
        answer = await unset.deliver(unset.wire.handshake("subscribe", "", "1"))
        await unset.adapter.disconnect()
        return answer.status_code

    assert run(no_token()) == 503


def test_an_oversized_body_is_refused_before_it_is_read() -> None:
    rig = Rig()

    async def main() -> int:
        await rig.adapter.connect()
        body = b"x" * (MAX_BODY_BYTES + 1)
        answer = await rig.deliver(rig.wire.signed(body))
        await rig.adapter.disconnect()
        return answer.status_code

    assert run(main()) == 413
    assert rig.events == []


def test_message_kinds_become_events() -> None:
    rig = Rig()
    rig.wire.media["vo1"] = (b"ogg", "audio/ogg; codecs=opus", "vo1")
    messages = [
        text_message(ANA, "hi there", "wamid.T1"),
        {
            "from": ANA,
            "id": "wamid.V1",
            "type": "audio",
            "audio": {"id": "vo1", "mime_type": "audio/ogg", "voice": True},
        },
        {
            "from": ANA,
            "id": "wamid.M1",
            "type": "audio",
            "audio": {"id": "mu1", "mime_type": "audio/mpeg"},
        },
        {
            "from": ANA,
            "id": "wamid.I1",
            "type": "image",
            "image": {"id": "im1", "mime_type": "image/jpeg", "caption": "look"},
        },
        {
            "from": ANA,
            "id": "wamid.D1",
            "type": "document",
            "document": {"id": "do1", "mime_type": "application/pdf", "filename": "report.pdf"},
        },
        {
            "from": ANA,
            "id": "wamid.L1",
            "type": "location",
            "location": {"latitude": 52.5, "longitude": 13.4, "name": "Office"},
        },
        {
            "from": ANA,
            "id": "wamid.C1",
            "type": "contacts",
            "contacts": [{"name": {"formatted_name": "Ravi K"}}],
        },
        {
            "from": ANA,
            "id": "wamid.B1",
            "type": "button",
            "button": {"text": "Yes please", "payload": "yes"},
        },
        {
            "from": ANA,
            "id": "wamid.R1",
            "type": "reaction",
            "reaction": {"message_id": "wamid.T1", "emoji": "👍"},
        },
        {"from": ANA, "id": "wamid.U1", "type": "unsupported", "errors": [{"code": 131051}]},
        {
            "from": ANA,
            "id": "wamid.G1",
            "group_id": "120363",
            "type": "text",
            "text": {"body": "group"},
        },
        {"from": ANA, "id": "wamid.E1", "type": "text", "text": {"body": "   "}},
        "not a message",
    ]
    contacts = [{"profile": {"name": "Ana"}, "wa_id": ANA}]

    async def main() -> None:
        await rig.adapter.connect()
        answer = await rig.inbound(*messages, contacts=contacts)  # pyright: ignore[reportArgumentType]
        assert answer.status_code == 200
        await rig.adapter.disconnect()

    run(main())
    kinds = [(e.message_id, e.kind, e.text) for e in rig.events]
    assert kinds == [
        ("wamid.T1", MessageKind.TEXT, "hi there"),
        ("wamid.V1", MessageKind.VOICE, ""),
        ("wamid.M1", MessageKind.AUDIO, ""),
        ("wamid.I1", MessageKind.PHOTO, "look"),
        ("wamid.D1", MessageKind.DOCUMENT, ""),
        ("wamid.L1", MessageKind.LOCATION, "[Location: Office (52.5, 13.4)]"),
        ("wamid.C1", MessageKind.TEXT, "[Shared contact: Ravi K]"),
        ("wamid.B1", MessageKind.TEXT, "Yes please"),
    ]
    first = rig.events[0]
    assert first.source.chat_type == "dm" and first.source.chat_id == ANA
    assert first.user_id == ANA and first.source.user_id_alt == f"+{ANA}"
    assert first.user_name == "Ana" and first.timestamp is not None
    assert rig.events[1].media == (Attachment(ref="wac:vo1", mime="audio/ogg"),)
    assert rig.events[4].metadata["filename"] == "report.pdf"


def test_only_our_number_and_account_and_message_changes_count() -> None:
    rig = Rig(waba_id="102290129340398")

    async def main() -> None:
        await rig.adapter.connect()
        await rig.inbound(text_message(ANA, "other number", "wamid.X1"), phone_number_id="999")
        await rig.inbound(text_message(ANA, "other account", "wamid.X2"), waba_id="777")
        await rig.inbound(text_message(ANA, "template news", "wamid.X3"), field="account_update")
        await rig.inbound(statuses=[{"id": "wamid.S1", "status": "read", "recipient_id": ANA}])
        await rig.deliver(rig.wire.signed({"object": "page", "entry": []}))
        await rig.inbound(text_message(ANA, "ours", "wamid.X4"))
        await rig.adapter.disconnect()

    run(main())
    assert [e.text for e in rig.events] == ["ours"]


def test_a_reply_to_our_message_is_marked_own() -> None:
    rig = Rig()
    context = {"context": {"from": BUSINESS_NUMBER, "id": "wamid.OUT1"}}

    async def main() -> None:
        await rig.adapter.connect()
        await rig.inbound(text_message(ANA, "about that", "wamid.Q1", **context))
        await rig.inbound(
            text_message(ANA, "quoting me", "wamid.Q2", context={"from": ANA, "id": "wamid.Q1"})
        )
        await rig.adapter.disconnect()

    run(main())
    assert [(e.reply_to_id, e.reply_to_is_own) for e in rig.events] == [
        ("wamid.OUT1", True),
        ("wamid.Q1", False),
    ]


# -- sending -------------------------------------------------------------------------------------


def test_a_send_outside_the_24_hour_window_is_forbidden_final_and_tried_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = Rig()
    rig.wire.window_closed.add(BEN)
    caplog.set_level(logging.INFO)

    async def main() -> Any:
        await rig.adapter.connect()
        result = await rig.adapter.send_with_retry(BEN, "your report is ready", rng=lambda: 0.0)
        await rig.adapter.disconnect()
        return result

    result = run(main())
    assert not result.ok and result.kind is ErrorKind.FORBIDDEN and not result.retryable
    assert "24-hour customer-service window" in (result.error or "")
    assert rig.adapter.is_final_failure(result)
    assert len(rig.wire.calls_to("POST", f"/{VERSION}/{PHONE_NUMBER_ID}/messages")) == 1
    assert rig.platform.texts(BEN) == []  # no "delivery failed" notice either
    assert BEN not in (result.error or "") and BEN not in caplog.text


def test_long_text_is_split_converted_and_quotes_only_the_first_piece() -> None:
    rig = Rig()
    words = " ".join(f"**w{n:04d}**" for n in range(1000))

    async def main() -> Any:
        await rig.adapter.connect()
        result = await rig.adapter.send(ANA, words, reply_to="wamid.IN1")
        plain = await rig.adapter.send(ANA, "**kept**", meta={"plain": True})
        await rig.adapter.disconnect()
        return result, plain

    result, plain = run(main())
    assert result.ok and len(result.ids) == 3
    sent = rig.platform.messages(ANA)
    assert all(len(m.text) <= 4096 for m in sent)
    assert sent[0].text.startswith("*w0000* *w0001*") and "**" not in sent[0].text
    assert [m.reply_to for m in sent[:3]] == ["wamid.IN1", None, None]
    assert plain.ok and sent[-1].text == "**kept**"


def test_four_approval_choices_render_as_a_list_and_three_as_buttons() -> None:
    rig = Rig()
    three = replace(PROMPT, actions=PROMPT.actions[:2] + PROMPT.actions[3:])

    async def main() -> tuple[Any, Any]:
        await rig.adapter.connect()
        listed = await rig.adapter.send_approval(ANA, PROMPT)
        buttons = await rig.adapter.send_approval(ANA, three)
        await rig.adapter.disconnect()
        return listed, buttons

    listed, buttons = run(main())
    posts = [c.json() for c in rig.wire.calls_to("POST", f"/{VERSION}/{PHONE_NUMBER_ID}/messages")]
    as_list, as_buttons = posts[0]["interactive"], posts[1]["interactive"]
    assert as_list["type"] == "list"
    rows = as_list["action"]["sections"][0]["rows"]
    assert [r["id"] for r in rows] == [approval_callback("req1", a.choice) for a in PROMPT.actions]
    assert all(len(r["title"]) <= 24 for r in rows) and len(as_list["action"]["button"]) <= 20
    assert as_buttons["type"] == "button"
    replies = [b["reply"] for b in as_buttons["action"]["buttons"]]
    assert [r["id"] for r in replies] == [
        approval_callback("req1", a.choice) for a in three.actions
    ]
    assert replies[1]["title"] == "Allow for this sess…" and len(replies[1]["title"]) == 20
    assert "```\nrm -rf build\n```" in as_list["body"]["text"]
    message = rig.platform.message(str(listed.message_id))
    assert message is not None and len(message.buttons) == 4 and buttons.ok


def test_a_prompt_too_long_for_a_card_goes_as_the_text_card() -> None:
    rig = Rig()
    long = replace(PROMPT, text="Run this?\n" + "x" * 1100)
    many = ClarifyPrompt(
        "cl1", "key", "Pick one", tuple(f"option {n}" for n in range(11)), fallback_text="1. …"
    )

    async def main() -> None:
        await rig.adapter.connect()
        assert (await rig.adapter.send_approval(ANA, long)).ok
        assert (await rig.adapter.send_clarify(ANA, many)).ok
        await rig.adapter.disconnect()

    run(main())
    posts = [c.json() for c in rig.wire.calls_to("POST", f"/{VERSION}/{PHONE_NUMBER_ID}/messages")]
    assert [p["type"] for p in posts] == ["text", "text"]
    assert "/approve" in posts[0]["text"]["body"]


def test_clarify_choices_are_numbered_buttons_with_the_full_text_in_the_body() -> None:
    rig = Rig()
    prompt = ClarifyPrompt(
        "cl2", "key", "Which deploy target?", ("staging cluster in Frankfurt", "production")
    )

    async def main() -> None:
        await rig.adapter.connect()
        assert (await rig.adapter.send_clarify(ANA, prompt)).ok
        await rig.adapter.disconnect()

    run(main())
    (post,) = [c.json() for c in rig.wire.calls_to("POST", f"/{VERSION}/{PHONE_NUMBER_ID}/")]
    interactive = post["interactive"]
    replies = [b["reply"] for b in interactive["action"]["buttons"]]
    assert [r["id"] for r in replies] == [clarify_callback("cl2", 0), clarify_callback("cl2", 1)]
    assert [r["title"] for r in replies] == ["1. staging cluster…", "2. production"]
    assert "1. staging cluster in Frankfurt" in interactive["body"]["text"]


def test_a_tap_must_name_an_option_its_prompt_offered() -> None:
    rig = Rig()

    async def main() -> None:
        await rig.adapter.connect()
        card = await rig.adapter.send_approval(ANA, PROMPT)
        wamid = str(card.message_id)

        def tap(choice: str, n: int, prompt: str) -> dict[str, Any]:
            return {
                "from": ANA,
                "id": f"wamid.tap{n}",
                "type": "interactive",
                "interactive": {"type": "list_reply", "list_reply": {"id": choice, "title": "x"}},
                "context": {"from": BUSINESS_NUMBER, "id": prompt},
            }

        await rig.inbound(tap(approval_callback("other", "once"), 1, wamid))
        await rig.inbound(tap(approval_callback("req1", "deny"), 2, wamid))
        await rig.inbound(tap(approval_callback("old", "once"), 3, "wamid.from-before-restart"))
        await rig.adapter.disconnect()

    run(main())
    assert [e.metadata["button"] for e in rig.events] == [
        approval_callback("req1", "deny"),
        approval_callback("old", "once"),
    ]


def test_typing_marks_the_last_inbound_message_read() -> None:
    rig = Rig()

    async def main() -> None:
        await rig.adapter.connect()
        await rig.adapter.typing_start(ANA)  # nothing to attach it to yet
        rig.platform.dm(ANA, "hi")
        await rig.wire.pump()
        await rig.adapter.listener.idle()
        await rig.adapter.typing_start(ANA)
        await rig.adapter.disconnect()

    run(main())
    receipts = [
        c.json()
        for c in rig.wire.calls_to("POST", f"/{VERSION}/{PHONE_NUMBER_ID}/messages")
        if c.json().get("status") == "read"
    ]
    assert receipts == [
        {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": rig.events[0].message_id,
            "typing_indicator": {"type": "text"},
        }
    ]
    assert [op.chat_id for op in rig.platform.ops_of("typing")] == [ANA]


def test_media_is_uploaded_then_sent_by_id(tmp_path: Path) -> None:
    rig = Rig()
    photo = tmp_path / "chart.png"
    photo.write_bytes(b"\x89PNG fake")
    notes = tmp_path / "notes.txt"
    notes.write_text("hello")
    huge = tmp_path / "huge.png"
    huge.write_bytes(b"0" * (5 * 1024 * 1024 + 1))

    async def main() -> list[Any]:
        await rig.adapter.connect()
        results = [
            await rig.adapter.send_image(ANA, str(photo), caption="**Q3**", reply_to="wamid.IN9"),
            await rig.adapter.send_file(ANA, str(notes)),
            await rig.adapter.send_image(ANA, str(huge)),
        ]
        await rig.adapter.disconnect()
        return results

    image, document, too_big = run(main())
    assert image.ok and document.ok
    assert not too_big.ok and too_big.kind is ErrorKind.TOO_LONG
    uploads = rig.wire.calls_to("POST", f"/{VERSION}/{PHONE_NUMBER_ID}/media")
    assert len(uploads) == 2  # the oversized file never left the machine
    posts = [c.json() for c in rig.wire.calls_to("POST", f"/{VERSION}/{PHONE_NUMBER_ID}/messages")]
    assert posts[0]["image"] == {"id": "up1", "caption": "**Q3**"}
    assert posts[0]["context"] == {"message_id": "wamid.IN9"}
    assert posts[1]["document"] == {"id": "up2", "filename": "notes.txt"}
    media = rig.platform.ops_of("media")
    assert [(op.extra["kind"], op.text) for op in media] == [("photo", "**Q3**"), ("document", "")]
    assert rig.wire.media["up1"][0] == b"\x89PNG fake"


def test_the_fetcher_downloads_in_two_steps_and_keeps_the_token_on_meta_hosts() -> None:
    rig = Rig()
    registered: list[tuple[str, Callable[..., Any]]] = []

    class Seat:
        def register_fetcher(self, platform: str, fetcher: Any) -> Callable[[], None]:
            registered.append((platform, fetcher))
            return lambda: None

    assert rig.adapter.register_fetchers(Seat()) is not None
    rig.wire.media["vo1"] = (b"ogg bytes", "audio/ogg", "vo1")

    async def fetch(ref: str) -> bytes:
        chunks = [c async for c in rig.adapter.fetch_media(Attachment(ref=ref, mime=""))]
        return b"".join(chunks)

    async def main() -> None:
        await rig.adapter.connect()
        assert await fetch("wac:vo1") == b"ogg bytes"
        for bad in ("wac:../etc", "tg:vo1", "wac:"):
            with pytest.raises(ValueError):
                await fetch(bad)
        await rig.adapter.disconnect()

    run(main())
    assert registered == [("whatsapp_cloud", rig.adapter.fetch_media)]
    gets = [c.path for c in rig.wire.calls_to("GET")]
    assert f"/{VERSION}/vo1" in gets and "/media-download/vo1" in gets
    assert rig.adapter._token_may_go_to("https://lookaside.fbsbx.com/whatsapp_business/x")  # pyright: ignore[reportPrivateUsage]
    for foreign in (
        "https://evil.test/x",
        "http://lookaside.fbsbx.com/x",
        "https://fbsbx.com.evil.test/",
    ):
        assert not rig.adapter._token_may_go_to(foreign)  # pyright: ignore[reportPrivateUsage]


# -- connecting ----------------------------------------------------------------------------------


def test_connect_refusals_are_fatal_with_a_reason() -> None:
    def refused(rig: Rig) -> AdapterConnectError:
        async def main() -> AdapterConnectError:
            try:
                await rig.adapter.connect()
            except AdapterConnectError as exc:
                return exc
            raise AssertionError("connect should have failed")

        return run(main())

    bad_token = refused(Rig(access_token="x" * 24))
    assert bad_token.code == "whatsapp_cloud_auth" and not bad_token.retryable
    unknown = refused(Rig(phone_number_id="999000"))
    assert unknown.code == "whatsapp_cloud_config" and not unknown.retryable
    no_secret = Rig(app_secret="")
    exc = refused(no_secret)
    assert exc.code == "whatsapp_cloud_app_secret_missing" and not exc.retryable
    assert no_secret.wire.calls == []  # refused before any call
    rig = Rig()
    rig.platform.fail_connect(FailureKind.CONNECT_TIMEOUT)
    flaky = refused(rig)
    assert flaky.retryable and rig.wire.closed == 1  # the client was closed again


def test_a_refused_token_on_send_is_final_and_reports_the_fatal_state() -> None:
    rig = Rig()
    fatal: list[tuple[str, str]] = []
    rig.adapter.set_fatal_listener(lambda code, message: fatal.append((code, message)))

    async def main() -> Any:
        await rig.adapter.connect()
        rig.wire.token = "rotated-" + "z" * 16
        result = await rig.adapter.send_with_retry(ANA, "hi", rng=lambda: 0.0)
        await rig.adapter.disconnect()
        return result

    result = run(main())
    assert not result.ok and rig.adapter.is_final_failure(result)
    assert fatal and fatal[0][0] == "whatsapp_cloud_auth"
    assert len(rig.wire.calls_to("POST")) == 1
    assert TOKEN not in (result.error or "") and TOKEN not in fatal[0][1]


def test_send_only_opens_no_listener_and_consumes_nothing() -> None:
    rig = Rig(send_only=True, app_secret="")

    async def main() -> Any:
        await rig.adapter.connect()
        rig.platform.dm(ANA, "for the gateway")
        result = await rig.adapter.send(ANA, "one-shot")
        accepting = rig.adapter.listener.accepting
        await rig.adapter.disconnect()
        return result, accepting

    result, accepting = run(main())
    assert result.ok and not accepting
    assert rig.platform.texts(ANA) == ["one-shot"]
    assert rig.platform.next_inbound() is not None and rig.events == []


def platform_settings(**extra: Any) -> PlatformSettings:
    return PlatformSettings(
        "whatsapp_cloud",
        True,
        "credentials",
        PlatformSection.model_validate({"extra": extra}),
        credentials={
            "WHATSAPP_CLOUD_ACCESS_TOKEN": TOKEN,
            "WHATSAPP_CLOUD_PHONE_NUMBER_ID": PHONE_NUMBER_ID,
        },
    )


def test_create_adapter_reads_credentials_and_binds_loopback(
    scope: Scope, fake_clock: FakeClock
) -> None:
    secrets: Any = scope.secrets
    secrets.set("WHATSAPP_CLOUD_APP_SECRET", APP_SECRET)
    secrets.set("WHATSAPP_CLOUD_VERIFY_TOKEN", VERIFY_TOKEN)
    adapter = create_adapter(
        AdapterConfig("whatsapp_cloud", platform_settings(api_version="v24.0"), scope, fake_clock)
    )
    assert adapter.cloud.app_secret == APP_SECRET and adapter.cloud.verify_token == VERIFY_TOKEN
    assert adapter.cloud.api_version == "v24.0"
    listen = adapter.listener.settings
    assert (listen.host, listen.port, listen.path) == ("127.0.0.1", 8090, "/whatsapp/webhook")
    assert listen.max_body_bytes == 3 * 1024 * 1024 and not adapter.send_only
    for shown in (repr(adapter), repr(adapter.cloud)):
        assert TOKEN not in shown and APP_SECRET not in shown
    missing = replace(platform_settings(), credentials={})
    with pytest.raises(ValueError, match="WHATSAPP_CLOUD_ACCESS_TOKEN"):
        create_adapter(AdapterConfig("whatsapp_cloud", missing, scope, fake_clock))
    with pytest.raises(ValueError, match="webhook_public"):
        create_adapter(
            AdapterConfig(
                "whatsapp_cloud", platform_settings(webhook_host="0.0.0.0"), scope, fake_clock
            )
        )
    with pytest.raises(ValueError, match="api_version"):
        create_adapter(
            AdapterConfig(
                "whatsapp_cloud", platform_settings(api_version="latest"), scope, fake_clock
            )
        )


def test_indus_send_builds_a_send_only_adapter(
    scope: Scope, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(fake_clock)
    wire: WhatsAppCloudWire = env.wire
    monkeypatch.setattr(cloud_mod, "CLIENT_FACTORY", lambda cloud, proxy: wire.client())
    registry = PlatformRegistry(builtins=())
    registry.register(
        PlatformEntry(
            "whatsapp_cloud",
            "WhatsApp Cloud",
            factory=create_adapter,
            check_fn=lambda: True,
            required_env=("WHATSAPP_CLOUD_ACCESS_TOKEN", "WHATSAPP_CLOUD_PHONE_NUMBER_ID"),
        )
    )
    adapter = send_command.build_adapter(
        scope, "whatsapp_cloud", platform_settings(base_url=wire.base_url), registry
    )
    assert isinstance(adapter, WhatsAppCloudAdapter) and adapter.send_only

    async def main() -> Any:
        await adapter.connect()
        result = await adapter.send(ANA, "deploy done")
        await adapter.disconnect()
        return result

    assert run(main()).ok
    assert env.platform.texts(ANA) == ["deploy done"]
    assert not adapter.listener.accepting


def test_every_graph_route_has_a_fake() -> None:
    rig = Rig()

    async def main() -> list[int]:
        await rig.adapter.connect()
        client = rig.wire.client()
        auth = {"Authorization": f"Bearer {TOKEN}"}
        values = {"version": VERSION, "phone_number_id": PHONE_NUMBER_ID, "media_id": "m1"}
        rig.wire.media["m1"] = (b"x", "image/png", "m1")
        codes = []
        for method, template in GRAPH_ROUTES:
            path = template.format(**values)
            answer = await client.request(method, path, headers=auth, json={})
            codes.append(answer.status_code)
            assert "no such endpoint" not in answer.text, (method, template)
        await client.aclose()
        await rig.adapter.disconnect()
        return codes

    run(main())


# -- privacy and markup --------------------------------------------------------------------------


def test_numbers_and_tokens_are_masked_in_errors_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    rig = Rig()
    caplog.set_level(logging.DEBUG)
    rig.platform.fail(FailureKind.CHAT_NOT_FOUND, op="send")

    async def main() -> Any:
        await rig.adapter.connect()
        result = await rig.adapter.send(BEN, "hello")
        await rig.inbound(text_message(BEN, "group", "wamid.G9", group_id="1203"))
        await rig.adapter.disconnect()
        return result

    result = run(main())
    assert result.kind is ErrorKind.NOT_FOUND and result.chat_unreachable
    assert rig.adapter.scrub(
        f"to +{BEN} with {TOKEN}"
    ) == "to +*******0300 with " + rig.adapter.scrub(TOKEN)
    assert TOKEN not in rig.adapter.scrub(TOKEN)
    assert BEN not in caplog.text and TOKEN not in caplog.text and APP_SECRET not in caplog.text
    assert mask_number("+1 (555) 000-0300") == "+* (***) ***-0300"


@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        ("**bold** and *it* and _it_", "*bold* and _it_ and _it_"),
        ("__also bold__ ~~gone~~", "*also bold* ~gone~"),
        ("**a *b* c**", "*a _b_ c*"),
        ("`**code**` stays", "`**code**` stays"),
        ("```\n**fenced**\n```", "```\n**fenced**\n```"),
        ("## **Title**", "*Title*"),
        (
            "[docs](https://x.test/d) and [https://y.test](https://y.test)",
            "docs (https://x.test/d) and https://y.test",
        ),
        ("* bullet\n- dash\n2*3*4", "* bullet\n- dash\n2*3*4"),
        ("", ""),
    ],
)
def test_markdown_becomes_whatsapp_markup(markdown: str, expected: str) -> None:
    assert to_whatsapp(markdown) == expected
    assert len(to_whatsapp(markdown)) <= len(markdown)


def test_the_sender_formats_tables_and_markup_for_whatsapp(scope: Scope) -> None:
    rig = Rig()

    async def main() -> Any:
        await rig.adapter.connect()
        sender = OutboundSender(rig.adapter, scope=scope)
        text = "# Report\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n**done**"
        report = await sender.deliver(ANA, FinalReply(text))
        await rig.adapter.disconnect()
        return report

    report = run(main())
    assert report.outcome == "sent"
    (sent,) = rig.platform.texts(ANA)
    assert sent.startswith("*Report*") and "*done*" in sent and "**" not in sent
