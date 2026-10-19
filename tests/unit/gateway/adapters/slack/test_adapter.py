"""P15-35: the Slack adapter over the Slack wire fake (no Slack library needed)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from indus.core.persist import StateStore
from indus.core.turn_state import TurnState
from indus.foundation.config.schema import PlatformSection
from indus.foundation.scope import Scope
from indus.gateway.adapters.base import (
    AdapterConnectError,
    ApprovalAction,
    ApprovalPrompt,
    ClarifyPrompt,
    approval_callback,
)
from indus.gateway.adapters.slack import adapter as slack_mod
from indus.gateway.adapters.slack import blocks as kit
from indus.gateway.adapters.slack import create_adapter
from indus.gateway.adapters.slack.adapter import (
    DEFAULT_STATUS_TEXT,
    MAX_LEN,
    SlackAdapter,
    SlackSdk,
    is_slack_file_url,
)
from indus.gateway.config import AdapterConfig, PlatformSettings
from indus.gateway.core.admission import AdmissionPipeline, IntakeSettings
from indus.gateway.core.authz import Authorizer, UnauthorizedHandler
from indus.gateway.core.events import Attachment, InboundEvent, MessageKind
from indus.gateway.core.prompts import ChatPrompts
from indus.gateway.core.send import ErrorKind
from indus.gateway.core.session_keys import build_session_key
from indus.gateway.delivery import send_command
from indus.gateway.outbound.formatting import SLACK_MAX_BLOCKS, SLACK_MAX_SECTION_TEXT
from indus.gateway.outbound.replies import PlatformReplies
from indus.gateway.outbound.sender import OutboundSender
from indus.gateway.registry import PlatformRegistry
from indus.protocol.requests_port import RequestWithdrawn
from indus.protocol.server_requests import ApprovalAnswer
from indus.service.session_service import SessionService
from indus.state import registry as store_registry
from indus.state.pairing import PairingStore
from indus.tools.calls import ToolCall, ToolResult
from tests.contract.gateway.slack_case import wire_sdk
from tests.fakes.agent import fake_options
from tests.fakes.clock import FakeClock, checkpoint
from tests.fakes.model import FakeModelServer, fake_route, script_of, text_turn, tool_batch
from tests.fakes.platform.core import FailureKind
from tests.fakes.platform.slack_wire import SlackApiError, SlackPlatform, SlackResponse, SlackWire
from tests.unit.cli.commands.cmdrun import run_cli

BOT = "bot-" + "token-" + "x" * 12  # built at run time, not Slack-shaped
APP = "app-" + "token-" + "y" * 12


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def settings(**extra: Any) -> PlatformSettings:
    return PlatformSettings(
        "slack",
        True,
        "credentials",
        PlatformSection.model_validate({"extra": extra}),
        credentials={"SLACK_BOT_TOKEN": BOT, "SLACK_APP_TOKEN": APP},
    )


class Rig:
    """A Slack wire fake, an adapter over it and the events it emitted."""

    def __init__(self, clock: FakeClock, *, send_only: bool = False, **extra: Any) -> None:
        self.wire = SlackWire(
            SlackPlatform("slack", clock=clock, max_len=MAX_LEN, bot_user_id="U0BOT")
        )
        self.sockets: list[Any] = []
        base = wire_sdk(self.wire)

        def make_socket(app: Any, token: str, proxy: str | None) -> Any:
            handler = base.make_socket(app, token, proxy)
            self.sockets.append(handler)
            return handler

        self.adapter = SlackAdapter(
            clock=clock,
            bot_token=BOT,
            app_token=APP,
            settings=settings(**extra),
            send_only=send_only,
            sdk=SlackSdk(base.make_app, make_socket),
        )
        self.events: list[InboundEvent] = []

        async def sink(event: InboundEvent) -> None:
            self.events.append(event)

        self.adapter.set_sink(sink)

    @property
    def platform(self) -> SlackPlatform:
        platform = self.wire.platform
        assert isinstance(platform, SlackPlatform)
        return platform

    def calls(self, name: str) -> list[dict[str, Any]]:
        return [kw for n, kw in self.wire.client.calls if n == name]

    async def deliver(self, kind: str, event: dict[str, Any]) -> None:
        handler = self.wire.app.events[kind]
        await handler(event=event, say=None)


def message(
    text: str, *, channel: str = "C1", ts: str = "1700000001.000100", **kw: Any
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "message",
        "channel": channel,
        "channel_type": "channel",
        "user": "U7",
        "text": text,
        "ts": ts,
        "team": "T1",
    }
    event.update(kw)
    return event


@pytest.fixture
def rig(fake_clock: FakeClock) -> Rig:
    return Rig(fake_clock)


# -- inbound ---------------------------------------------------------------------------------------


def test_message_and_app_mention_with_one_ts_make_one_turn(rig: Rig) -> None:
    async def main() -> None:
        await rig.adapter.connect()
        rig.platform.add_chat("C9", "group", scope_id="T1")
        rig.platform.group("C9", "U7", "<@U0BOT> what is up", mention=True)
        await rig.wire.dispatch_pending()

    run(main())
    assert len(rig.events) == 1
    (event,) = rig.events
    assert event.text == "what is up"
    assert event.metadata["mentioned"] is True
    assert event.source.chat_type == "group" and event.source.scope_id == "T1"


def test_an_app_mention_alone_counts_as_mentioned_and_a_redelivery_is_dropped(rig: Rig) -> None:
    async def main() -> None:
        await rig.adapter.connect()
        mention = {**message("hello"), "type": "app_mention"}
        mention.pop("channel_type")
        await rig.deliver("app_mention", mention)
        await rig.deliver("app_mention", dict(mention))  # Socket Mode redelivery
        await rig.deliver("message", message("hello"))

    run(main())
    assert len(rig.events) == 1
    assert rig.events[0].metadata.get("mentioned") is True
    assert rig.events[0].source.chat_type == "group"


def test_own_posts_are_dropped_and_other_bots_are_marked(rig: Rig) -> None:
    async def main() -> None:
        await rig.adapter.connect()
        await rig.deliver("message", message("echo", user="U0BOT", ts="1700000001.000001"))
        await rig.deliver(
            "message",
            message("own app", ts="1700000001.000002", bot_id="BU0BOT", subtype="bot_message"),
        )
        other = message("beep", ts="1700000001.000003", subtype="bot_message", bot_id="B42")
        other.pop("user")
        await rig.deliver("message", other)

    run(main())
    assert [e.text for e in rig.events] == ["beep"]
    assert rig.events[0].source.is_bot and rig.events[0].source.user_id == "B42"


def test_edits_and_housekeeping(rig: Rig) -> None:
    async def main() -> None:
        await rig.adapter.connect()
        await rig.deliver("message", message("first", ts="1700000001.000010"))
        for ts, text in (("1700000001.000010", "first, edited"), ("1700000001.000011", "late")):
            await rig.deliver(
                "message",
                {
                    "type": "message",
                    "subtype": "message_changed",
                    "channel": "C1",
                    "team": "T1",
                    "ts": "1700000002.000000",
                    "message": message(text, ts=ts, edited={"ts": "1700000002.000000"}),
                },
            )
        await rig.deliver(
            "message", message("joined", ts="1700000001.000012", subtype="channel_join")
        )
        await rig.deliver(
            "message", message("gone", ts="1700000001.000013", subtype="message_deleted")
        )

    run(main())
    assert [(e.text, e.metadata.get("edited")) for e in rig.events] == [
        ("first", None),
        ("late", True),
    ]


def test_two_workspaces_with_one_channel_id_get_different_keys(rig: Rig) -> None:
    async def main() -> None:
        await rig.adapter.connect()
        await rig.deliver("message", message("hi", team="T1"))
        await rig.deliver("message", message("hi", team="T2"))

    run(main())
    first, second = (e.source for e in rig.events)
    assert (first.scope_id, second.scope_id) == ("T1", "T2")
    assert first.chat_id == second.chat_id == "C1"
    assert build_session_key(first) != build_session_key(second)
    assert ":T1:C1" in build_session_key(first)


def test_a_channel_message_and_its_thread_follow_up_share_one_session(rig: Rig) -> None:
    root = "1700000001.000500"

    async def main() -> None:
        await rig.adapter.connect()
        await rig.deliver("message", message("<@U0BOT> start", ts=root))
        await rig.deliver("message", message("more", ts="1700000001.000501", thread_ts=root))
        await rig.deliver(
            "message", message("other thread", ts="1700000001.000601", thread_ts="1.0")
        )

    run(main())
    top, follow_up, stranger = rig.events
    assert top.source.chat_type == "group" and top.source.prospective_thread_id == root
    assert follow_up.source.chat_type == "thread" and follow_up.source.thread_id == root
    assert build_session_key(top.source) == build_session_key(follow_up.source)
    # A follow-up in a thread we were summoned into counts as addressed; another thread does not.
    assert follow_up.metadata.get("mentioned") is True
    assert "mentioned" not in stranger.metadata


def test_without_reply_in_thread_a_channel_message_has_no_prospective_thread(
    fake_clock: FakeClock,
) -> None:
    rig = Rig(fake_clock, reply_in_thread=False)

    async def main() -> None:
        await rig.adapter.connect()
        await rig.deliver("message", message("hi"))
        await rig.adapter.send("C1", "answer", reply_to="1700000001.000100")

    run(main())
    assert rig.events[0].source.prospective_thread_id is None
    assert rig.calls("chat_postMessage")[0]["thread_ts"] is None


def test_files_become_attachments_with_their_kinds(rig: Rig) -> None:
    def file(name: str, mime: str, **kw: Any) -> dict[str, Any]:
        return {
            "name": name,
            "mimetype": mime,
            "url_private_download": f"https://files.slack.com/files-pri/T1-F1/download/{name}",
            **kw,
        }

    async def main() -> None:
        await rig.adapter.connect()
        cases = [
            file("clip.webm", "video/webm", subtype="slack_audio"),
            file("a.png", "image/png"),
            file("film.mp4", "video/mp4"),
            file("notes.pdf", "application/pdf"),
        ]
        for n, item in enumerate(cases):
            await rig.deliver(
                "message",
                message("", ts=f"1700000003.00000{n}", subtype="file_share", files=[item]),
            )

    run(main())
    assert [e.kind for e in rig.events] == [
        MessageKind.VOICE,
        MessageKind.PHOTO,
        MessageKind.VIDEO,
        MessageKind.DOCUMENT,
    ]
    assert rig.events[1].media[0].ref.endswith("/a.png")
    assert rig.events[1].media[0].mime == "image/png"


def test_ignored_channels_get_no_inbound_and_no_outbound(fake_clock: FakeClock) -> None:
    rig = Rig(fake_clock, ignored_channels="C1, C2")

    async def main() -> Any:
        await rig.adapter.connect()
        await rig.deliver("message", message("hi", channel="C2"))
        return await rig.adapter.send("C1", "hello")

    result = run(main())
    assert rig.events == []
    assert not result.ok and result.unsupported
    assert rig.calls("chat_postMessage") == []


def test_names_are_looked_up_once(rig: Rig) -> None:
    async def main() -> None:
        await rig.adapter.connect()
        rig.platform.add_chat("C1", "group", "general")
        await rig.deliver("message", message("a", ts="1700000001.000001"))
        await rig.deliver("message", message("b", ts="1700000001.000002"))

    run(main())
    assert [e.source.user_name for e in rig.events] == ["user-U7", "user-U7"]
    assert [e.source.chat_name for e in rig.events] == ["general", "general"]
    assert len(rig.calls("users_info")) == 1 and len(rig.calls("conversations_info")) == 1


# -- outbound --------------------------------------------------------------------------------------


def test_replies_thread_under_the_message_except_in_dms(rig: Rig) -> None:
    async def main() -> None:
        await rig.adapter.connect()
        await rig.deliver("message", message("dm", channel="D5", channel_type="im"))
        await rig.adapter.send("C1", "in channel", reply_to="1700000001.000100")
        await rig.adapter.send("C1", "in thread", meta={"thread_id": "1700000000.000009"})
        await rig.adapter.send("D5", "in dm", reply_to="1700000001.000100")

    run(main())
    threads = [(c["text"], c["thread_ts"]) for c in rig.calls("chat_postMessage")]
    assert threads == [
        ("in channel", "1700000001.000100"),
        ("in thread", "1700000000.000009"),
        ("in dm", None),
    ]


def test_a_user_id_target_opens_the_dm_once(rig: Rig) -> None:
    async def main() -> None:
        await rig.adapter.connect()
        for text in ("one", "two"):
            result = await rig.adapter.send("U9", text)
            assert result.ok

    run(main())
    assert len(rig.calls("conversations_open")) == 1
    assert rig.platform.texts("U9") == ["one", "two"]


def test_too_long_text_never_reaches_slack(rig: Rig) -> None:
    async def main() -> Any:
        await rig.adapter.connect()
        return await rig.adapter.send("C1", "x" * (MAX_LEN + 1))

    result = run(main())
    assert result.kind is ErrorKind.TOO_LONG and rig.calls("chat_postMessage") == []


def test_rate_limit_wait_comes_from_the_header_in_any_case(rig: Rig) -> None:
    async def limited(**_: Any) -> SlackResponse:
        response = SlackResponse({"ok": False, "error": "ratelimited"}, {"retry-after": "42"})
        raise SlackApiError("The request to the Slack API failed.", response)

    async def main() -> Any:
        await rig.adapter.connect()
        rig.wire.client.chat_postMessage = limited  # type: ignore[method-assign]
        return await rig.adapter.send("C1", "x")

    result = run(main())
    assert result.kind is ErrorKind.RATE_LIMITED and result.retry_after == 42
    assert result.is_flood


def test_refused_caller_blocks_are_dropped_once(rig: Rig) -> None:
    async def main() -> Any:
        await rig.adapter.connect()
        rig.platform.fail(FailureKind.PARSE_ERROR, op="send")
        table = [{"type": "table", "rows": [[{"type": "raw_text", "text": "a"}]]}]
        return await rig.adapter.send("C1", "fallback text", meta={"blocks": table})

    result = run(main())
    posts = rig.calls("chat_postMessage")
    assert result.ok and len(posts) == 2
    assert posts[0]["blocks"] and posts[1]["blocks"] is None


def test_caller_blocks_are_capped_at_fifty(rig: Rig) -> None:
    many = [kit.section(f"part {n}") for n in range(SLACK_MAX_BLOCKS + 10)]

    async def main() -> None:
        await rig.adapter.connect()
        await rig.adapter.send("C1", "text", meta={"blocks": many})

    run(main())
    assert len(rig.calls("chat_postMessage")[0]["blocks"]) == SLACK_MAX_BLOCKS


def test_plain_sends_turn_markup_off(rig: Rig) -> None:
    async def main() -> None:
        await rig.adapter.connect()
        await rig.adapter.send("C1", "*plain*", meta={"plain": True, "blocks": [kit.section("x")]})

    run(main())
    post = rig.calls("chat_postMessage")[0]
    assert post["mrkdwn"] is False and post["blocks"] is None


PROMPT = ApprovalPrompt(
    request_id="req7",
    session_key="key",
    text="Run `rm -rf build > log`?",
    fallback_text="Run it? Reply /approve or /deny.",
    actions=(
        ApprovalAction("Allow once", "once", "primary"),
        ApprovalAction("Deny", "deny", "danger"),
    ),
)


def test_an_approval_card_is_buttons_and_a_final_edit_removes_them(rig: Rig) -> None:
    async def main() -> Any:
        await rig.adapter.connect()
        card = await rig.adapter.send_approval("C1", PROMPT, {"thread_id": "1700000000.000001"})
        assert card.ok and card.message_id
        sent = rig.platform.message(card.message_id)
        assert sent is not None
        assert [b.data for b in sent.buttons] == ["apv:req7:once", "apv:req7:deny"]
        edited = await rig.adapter.edit("C1", card.message_id, "Approved once.", final=True)
        assert edited.ok
        return card.message_id

    card_id = run(main())
    post = rig.calls("chat_postMessage")[0]
    assert post["thread_ts"] == "1700000000.000001"
    section = post["blocks"][0]["text"]["text"]
    assert "&gt;" in section  # mrkdwn-escaped
    assert post["blocks"][1]["elements"][0]["style"] == "primary"
    message_after = rig.platform.message(card_id)
    assert message_after is not None and message_after.buttons == ()
    assert message_after.text == "Approved once."


def test_a_refused_card_fails_instead_of_losing_its_buttons(rig: Rig) -> None:
    async def main() -> Any:
        await rig.adapter.connect()
        rig.platform.fail(FailureKind.PARSE_ERROR, op="send")
        return await rig.adapter.send_approval("C1", PROMPT)

    result = run(main())
    assert not result.ok and len(rig.calls("chat_postMessage")) == 1


def test_clarify_with_many_choices_stays_within_the_block_cap(rig: Rig) -> None:
    prompt = ClarifyPrompt(
        clarify_id="c1",
        session_key="key",
        question="Which one?",
        choices=tuple(f"option {n}" for n in range(400)),
    )

    async def main() -> None:
        await rig.adapter.connect()
        assert (await rig.adapter.send_clarify("C1", prompt)).ok

    run(main())
    blocks = rig.calls("chat_postMessage")[0]["blocks"]
    assert len(blocks) == SLACK_MAX_BLOCKS
    assert all(len(b["elements"]) <= kit.BUTTONS_PER_BLOCK for b in blocks[1:])
    assert blocks[1]["elements"][1]["action_id"] == "clr:c1:1"


def test_sanitize_clamps_and_drops() -> None:
    long_text = "y" * (SLACK_MAX_SECTION_TEXT + 50)
    cleaned = kit.sanitize(
        [
            kit.section(long_text),
            {"type": "section", "text": {"type": "mrkdwn", "text": long_text}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "  "}},
            {"type": "header", "text": {"type": "plain_text", "text": "h" * 200}},
            {"type": "actions", "elements": []},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": long_text}]},
            "not a block",
            {"no": "type"},
        ]
    )
    assert cleaned is not None
    assert [b["type"] for b in cleaned] == ["section", "section", "header", "context"]
    assert all(len(b["text"]["text"]) == SLACK_MAX_SECTION_TEXT for b in cleaned[:2])
    assert len(cleaned[2]["text"]["text"]) == kit.HEADER_TEXT_MAX
    assert len(cleaned[3]["elements"][0]["text"]) == SLACK_MAX_SECTION_TEXT
    assert kit.sanitize([]) is None and kit.sanitize("x") is None


def test_private_notices_are_ephemeral(rig: Rig) -> None:
    async def main() -> Any:
        await rig.adapter.connect()
        return await rig.adapter.send_notice_private("C1", "U7", "only you")

    result = run(main())
    assert result.ok and rig.wire.ephemeral == [("C1", "U7", "only you")]
    assert rig.platform.texts("C1") == []


def test_media_is_uploaded_into_the_thread(rig: Rig, tmp_path: Path) -> None:
    report = tmp_path / "report.pdf"
    report.write_bytes(b"%PDF")

    async def main() -> Any:
        await rig.adapter.connect()
        return await rig.adapter.send_document(
            "C1", str(report), caption="the report", reply_to="1700000000.000100"
        )

    result = run(main())
    (upload,) = rig.calls("files_upload_v2")
    assert result.ok and result.message_id and upload["filename"] == "report.pdf"
    media = rig.platform.ops_of("media")[0]
    assert media.thread_id == "100" and media.text == "the report"


def test_typing_is_a_thread_status_and_goes_off_without_the_scope(rig: Rig) -> None:
    async def main() -> None:
        await rig.adapter.connect()
        await rig.adapter.typing_start("C1")  # no thread: nothing to show
        await rig.adapter.typing_start("C1", {"thread_id": "1700000000.000003"})
        await rig.adapter.typing_stop("C1")
        rig.platform.fail(FailureKind.FORBIDDEN, op="typing")

        async def no_scope(**_: Any) -> SlackResponse:
            raise SlackApiError("failed", SlackResponse({"ok": False, "error": "missing_scope"}))

        rig.wire.client.assistant_threads_setStatus = no_scope  # type: ignore[method-assign]
        await rig.adapter.typing_start("C1", {"thread_id": "1700000000.000003"})
        await rig.adapter.typing_start("C1", {"thread_id": "1700000000.000003"})

    run(main())
    statuses = [c["status"] for c in rig.calls("assistant_threads_setStatus")]
    assert statuses == [DEFAULT_STATUS_TEXT, ""]
    assert rig.adapter._status_off  # pyright: ignore[reportPrivateUsage]


def test_typing_status_text_comes_from_settings(fake_clock: FakeClock) -> None:
    rig = Rig(fake_clock, typing_status_text="is working on it…")

    async def main() -> None:
        await rig.adapter.connect()
        await rig.adapter.typing_start("C1", {"thread_id": "1700000000.000003"})

    run(main())
    assert rig.calls("assistant_threads_setStatus")[0]["status"] == "is working on it…"


# -- connect ---------------------------------------------------------------------------------------


def test_a_refused_token_is_not_retryable_and_never_shown(rig: Rig) -> None:
    async def refused(**_: Any) -> SlackResponse:
        raise SlackApiError(
            f"bad token {BOT}", SlackResponse({"ok": False, "error": "invalid_auth"})
        )

    async def main() -> AdapterConnectError:
        rig.wire.client.auth_test = refused  # type: ignore[method-assign]
        with pytest.raises(AdapterConnectError) as raised:
            await rig.adapter.connect()
        return raised.value

    error = run(main())
    assert not error.retryable and error.code == "auth_failed"
    assert BOT not in str(error) and not rig.adapter.connected


def test_a_failing_socket_closes_and_hides_the_app_token(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    async def main() -> AdapterConnectError:
        await rig.adapter.connect()
        await rig.adapter.disconnect()

        original = rig.wire.handler

        def broken(token: str) -> Any:
            handler = original(token)

            async def connect_async() -> None:
                raise ConnectionResetError(f"socket closed for {token}")

            handler.connect_async = connect_async
            return handler

        rig.wire.handler = broken  # type: ignore[method-assign]
        with pytest.raises(AdapterConnectError) as raised:
            await rig.adapter.connect()
        return raised.value

    with caplog.at_level(logging.DEBUG):
        error = run(main())
    assert error.retryable and APP not in str(error)
    assert APP not in caplog.text and BOT not in caplog.text
    assert not rig.adapter.connected


def test_a_missing_library_is_a_fatal_connect_error(
    fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing() -> SlackSdk:
        raise ImportError("No module named 'slack_bolt'")

    monkeypatch.setattr(slack_mod, "SDK_LOADER", missing)
    adapter = SlackAdapter(clock=fake_clock, bot_token=BOT, app_token=APP)

    async def main() -> AdapterConnectError:
        with pytest.raises(AdapterConnectError) as raised:
            await adapter.connect()
        return raised.value

    error = run(main())
    assert not error.retryable and error.code == "missing_dependency"
    assert "indus-agent[slack]" in str(error)


def test_send_only_registers_no_listener_and_opens_no_socket(fake_clock: FakeClock) -> None:
    rig = Rig(fake_clock, send_only=True)

    async def main() -> Any:
        await rig.adapter.connect()
        rig.platform.dm("U7", "nobody reads this")
        result = await rig.adapter.send("C1", "one-shot")
        await rig.adapter.disconnect()
        return result

    assert run(main()).ok
    assert rig.wire.app.events == {} and rig.wire.app.actions == {}
    assert rig.sockets == [] and rig.events == []
    assert rig.platform.next_inbound() is not None  # still waiting for the gateway
    assert rig.platform.texts("C1") == ["one-shot"]


def test_create_adapter_reads_the_config(scope: Scope, fake_clock: FakeClock) -> None:
    config = AdapterConfig("slack", settings(), scope, fake_clock, "http://proxy:3128", True)
    adapter = create_adapter(config)
    assert adapter.send_only and adapter.reply_in_thread
    assert adapter._proxy == "http://proxy:3128"  # pyright: ignore[reportPrivateUsage]
    no_app = replace(settings(), credentials={"SLACK_BOT_TOKEN": BOT})
    with pytest.raises(ValueError, match="SLACK_APP_TOKEN"):
        create_adapter(AdapterConfig("slack", no_app, scope, fake_clock))
    assert BOT not in repr(adapter)


def test_the_real_library_builds_offline() -> None:
    pytest.importorskip("slack_bolt")
    sdk = slack_mod.load_sdk()

    async def main() -> None:
        app = sdk.make_app(BOT, None)
        assert callable(app.client.auth_test)
        handler = sdk.make_socket(app, APP, None)
        await handler.close_async()

    run(main())


# -- `indus send` ----------------------------------------------------------------------------------


@dataclass
class SendRig:
    wire: SlackWire
    platform: SlackPlatform
    sockets: list[str] = field(default_factory=list[str])
    built: list[SlackAdapter] = field(default_factory=list[SlackAdapter])


@pytest.fixture
def slack_send(
    scope: Scope, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> Iterator[SendRig]:
    """Slack enabled by credentials in the secret store; adapters built by the real
    ``build_adapter`` (the registry's library probe passes) over the wire fake."""
    secrets: Any = scope.secrets
    secrets.set("SLACK_BOT_TOKEN", BOT)
    secrets.set("SLACK_APP_TOKEN", APP)
    platform = SlackPlatform("slack", clock=fake_clock, max_len=MAX_LEN, bot_user_id="U0BOT")
    rig = SendRig(SlackWire(platform), platform)
    registry = PlatformRegistry()
    entry = registry.get("slack")
    assert entry is not None
    registry.register(replace(entry, check_fn=lambda: True), replace_existing=True)
    base = wire_sdk(rig.wire)

    def make_socket(app: Any, token: str, proxy: str | None) -> Any:
        rig.sockets.append(token)
        return base.make_socket(app, token, proxy)

    monkeypatch.setattr(slack_mod, "SDK_LOADER", lambda: SlackSdk(base.make_app, make_socket))

    def build(scope_: Scope, name: str, settings_: PlatformSettings, *_: Any) -> Any:
        adapter = send_command.build_adapter(scope_, name, settings_, registry)
        assert isinstance(adapter, SlackAdapter)
        rig.built.append(adapter)
        return adapter

    monkeypatch.setattr(send_command, "ADAPTER_BUILDER", build)
    yield rig


def test_indus_send_slack_does_no_inbound_work(
    capsys: pytest.CaptureFixture[str], scope: Scope, slack_send: SendRig
) -> None:
    slack_send.platform.dm("U7", "for the gateway")
    ran = run_cli(capsys, scope, "send", "slack:C42", "deploy done")
    assert ran.code == 0, ran.err
    (adapter,) = slack_send.built
    assert adapter.send_only and not adapter.connected
    assert slack_send.platform.texts("C42") == ["deploy done"]
    assert slack_send.wire.app.events == {} and slack_send.wire.app.actions == {}
    assert slack_send.sockets == []
    assert slack_send.platform.next_inbound() is not None  # the queued DM was not consumed
    assert BOT not in ran.out + ran.err


# -- media fetcher ---------------------------------------------------------------------------------


def test_the_fetcher_sends_the_token_only_to_slack(fake_clock: FakeClock) -> None:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"file-bytes", headers={"content-type": "image/png"})

    adapter = SlackAdapter(
        clock=fake_clock, bot_token=BOT, app_token=APP, http_transport=httpx.MockTransport(answer)
    )
    registered: list[tuple[str, Any]] = []

    class Seat:
        def register_fetcher(self, platform: str, fetcher: Any) -> Callable[[], None]:
            registered.append((platform, fetcher))
            return lambda: None

    adapter.register_fetchers(Seat())
    ((platform, fetch),) = registered
    assert platform == "slack"

    async def collect(ref: str) -> bytes:
        chunks: AsyncIterator[bytes] = fetch(Attachment(ref=ref, mime="image/png"))
        return b"".join([c async for c in chunks])

    ok = "https://files.slack.com/files-pri/T1-F1/a.png"
    assert run(collect(ok)) == b"file-bytes"
    assert seen[0].headers["authorization"] == f"Bearer {BOT}"
    for bad in (
        "https://files.example/a.png",
        "http://files.slack.com/a.png",
        "https://slack.com.evil.io/x",
    ):
        with pytest.raises(PermissionError):
            run(collect(bad))
    assert len(seen) == 1
    assert is_slack_file_url("https://slack.com/x") and not is_slack_file_url("ftp://slack.com/x")


def test_the_fetcher_refuses_a_sign_in_page(fake_clock: FakeClock) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"})

    adapter = SlackAdapter(
        clock=fake_clock, bot_token=BOT, app_token=APP, http_transport=httpx.MockTransport(answer)
    )
    fetch = adapter.media_fetcher()

    async def collect() -> bytes:
        chunks = fetch(Attachment(ref="https://files.slack.com/f/a.png", mime="image/png"))
        return b"".join([c async for c in chunks])

    with pytest.raises(PermissionError):
        run(collect())


# -- an approval press through the real intake -----------------------------------------------------


class Asker:
    """An inline tool that asks for approval and records the answer."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def __call__(self, call: ToolCall, state: TurnState) -> ToolResult:
        broker = state.control.broker
        assert broker is not None
        try:
            answer = await broker.ask(
                "approval",
                {"command": "rm -r build", "description": "deletes files"},
                turn_id=state.turn_id,
            )
        except RequestWithdrawn as exc:
            self.seen.append(f"not answered: {exc.reason.value}")
            return ToolResult("not answered", is_error=True)
        assert isinstance(answer, ApprovalAnswer)
        self.seen.append(answer.choice.value)
        return ToolResult(f"answer {answer.choice.value}")


async def _step(rig: Rig, clock: FakeClock) -> None:
    await rig.wire.dispatch_pending()
    await checkpoint()
    idle = asyncio.get_running_loop().create_future()
    await asyncio.wait({idle}, timeout=0.002)
    idle.cancel()
    clock.advance(0.05)


async def until(rig: Rig, clock: FakeClock, check: Callable[[], object]) -> None:
    """Deliver inbound traffic and let tasks run (moving the fake clock) until ``check()``."""
    loop = asyncio.get_running_loop()
    end = loop.time() + 10.0
    while not check():
        if loop.time() > end:
            raise AssertionError("the condition never held")
        await _step(rig, clock)


async def settle(rig: Rig, clock: FakeClock, rounds: int = 40) -> None:
    """Run for a while, for checks that something did NOT happen."""
    for _ in range(rounds):
        await _step(rig, clock)


def test_an_approval_press_settles_the_request(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        asker = Asker()
        server = FakeModelServer(
            script_of(tool_batch(("ask", {})), text_turn("all done")), clock=fake_clock
        )
        store = store_registry.open_unshared(scope)
        service = SessionService(
            scope,
            fake_route(server),
            toolsets=[],
            options=fake_options(server, session_id=None, inline={"ask": asker}),
            store=StateStore(store),
            lock_dir=tmp_path / "locks",
        )
        rig = Rig(fake_clock)
        adapter = rig.adapter
        sender = PlatformReplies({"slack": OutboundSender(adapter, scope=scope)})
        env = {"INDUS_SLACK_ALLOWED_USERS": "U200"}
        pairing = PairingStore(scope)
        authorizer = Authorizer.from_registry(
            PlatformRegistry(),
            env=env.get,
            extra_for=lambda _p: {},
            is_paired=pairing.is_approved,
            profile="work",
        )
        pipeline = AdmissionPipeline(
            scope=scope,
            service=service,
            sender=sender,
            store=store,
            authorizer=authorizer,
            unauthorized=UnauthorizedHandler(authorizer, pairing),
            adapters={"slack": adapter},
            settings=lambda: IntakeSettings(profile="work"),
            extra_for=lambda _p: {},
            toolsets_for=lambda _p: None,
        )
        prompts = ChatPrompts(pipeline)
        prompts.install()
        pipeline.start()
        try:
            await adapter.connect()
            rig.platform.dm("U200", "clean up")
            await until(rig, fake_clock, lambda: bool(prompts.open_cards()))
            await until(rig, fake_clock, lambda: prompts.open_cards()[0].message_id is not None)
            card = prompts.open_cards()[0]
            assert card.buttons and card.message_id
            shown = rig.platform.message(card.message_id)
            assert shown is not None and len(shown.buttons) >= 2
            once = approval_callback(card.request_id, "once")
            rig.platform.button_press("U300", "U300", once, message_id=card.message_id)
            await settle(rig, fake_clock)
            assert asker.seen == [] and prompts.open_cards()  # a stranger's press settles nothing
            rig.platform.button_press("U200", "U200", once, message_id=card.message_id)
            await until(rig, fake_clock, lambda: asker.seen == ["once"])
            await until(rig, fake_clock, lambda: "all done" in rig.platform.texts("U200"))
            settled = rig.platform.message(card.message_id)
            assert settled is not None and settled.buttons == ()
        finally:
            await prompts.aclose()
            await pipeline.aclose()
            await adapter.disconnect()
            await service.aclose()
            store.close()

    run(main())


def test_app_posts_count_as_bots_and_the_team_can_come_from_the_envelope(rig: Rig) -> None:
    async def main() -> None:
        await rig.adapter.connect()
        app_post = message("from an app", ts="1700000001.000020", app_id="A1")
        app_post.pop("team")
        await rig.adapter._on_message(  # pyright: ignore[reportPrivateUsage]
            app_post, {"authorizations": [{"team_id": "T9"}]}
        )
        person = message("from a person", ts="1700000001.000021", client_msg_id="x", app_id="A1")
        await rig.deliver("message", person)
        await rig.deliver(
            "message", message("canvas", ts="1700000001.000022", subtype="document_mention")
        )

    run(main())
    assert [(e.text, e.source.is_bot) for e in rig.events] == [
        ("from an app", True),
        ("from a person", False),
        ("canvas", False),
    ]
    assert rig.events[0].source.scope_id == "T9"


# -- the core's outbound thread (Capabilities.reply_opens_thread) ---------------------------------


def test_every_piece_card_and_status_of_a_channel_reply_goes_into_the_thread(
    rig: Rig, scope: Scope
) -> None:
    """A top-level channel message keys its session on its own ts (the prospective thread), so the
    core addresses that thread for every piece of a long reply, the cards and the typing status:
    later pieces never fall back to the channel."""
    root = "1700000001.000700"
    from indus.gateway.core import prompts as prompts_mod
    from indus.gateway.outbound.ports import typing_meta

    async def main() -> None:
        await rig.adapter.connect()
        await rig.deliver("message", message("<@U0BOT> write a lot", ts=root))
        (event,) = rig.events
        assert event.source.thread_id is None and event.source.prospective_thread_id == root
        replies = PlatformReplies({"slack": OutboundSender(rig.adapter, scope=scope)})
        long = "\n".join(["many words go here"] * 5000)  # over the 39,000 cap: several pieces
        await replies.reply(event.source, long, reply_to=event.message_id, kind="final")
        meta = prompts_mod._meta(event.source, rig.adapter)  # pyright: ignore[reportPrivateUsage]
        assert meta == {"thread_id": root}
        assert typing_meta(rig.adapter, event.source) == {"thread_id": root}

    run(main())
    posts = rig.calls("chat_postMessage")
    assert len(posts) >= 2
    assert [p["thread_ts"] for p in posts] == [root] * len(posts)
