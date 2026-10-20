"""The Teams adapter over the fake Bot Framework (P16-11): the row's Verify cases and the rules
the shared contract suite does not reach."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.gateway.adapters._http import ListenerSettings
from indus.gateway.adapters.base import (
    AdapterConnectError,
    ApprovalAction,
    ApprovalPrompt,
    Fetcher,
    approval_callback,
)
from indus.gateway.adapters.teams import create_adapter
from indus.gateway.adapters.teams.adapter import (
    MAX_BODY_BYTES,
    WEBHOOK_PATH,
    TeamsAdapter,
    TeamsOptions,
    media_access,
)
from indus.gateway.adapters.teams.conversations import (
    ConversationBook,
    conversation_ok,
    service_url_ok,
)
from indus.gateway.config import AdapterConfig, PlatformSettings
from indus.gateway.core.events import Attachment, InboundEvent, MessageKind
from indus.gateway.core.send import ErrorKind
from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import FailureKind, FakeChatPlatform
from tests.fakes.platform.teams_wire import (
    APP_ID,
    BOT_NAME,
    FILES_HOST,
    MAX_LEN,
    SERVICE_URL,
    TENANT_ID,
    TeamsWire,
    secret,
)

EMEA = "https://smba.trafficmanager.net/emea/"
CHANNEL = "19:ops0001@thread.tacv2"
ANA = "aad-ana"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("real network used in a Teams adapter test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield


def run(coro: Awaitable[Any]) -> Any:
    return asyncio.run(coro)  # pyright: ignore[reportArgumentType]


class Rig:
    def __init__(
        self,
        *,
        book: Path | None = None,
        send_only: bool = False,
        options: TeamsOptions | None = None,
        app_secret: str | None = None,
    ) -> None:
        self.clock = FakeClock(datetime(2026, 3, 1, 9, 0, tzinfo=UTC))
        self.platform = FakeChatPlatform(
            "teams", clock=self.clock, max_len=MAX_LEN, bot_user_id=f"28:{APP_ID}"
        )
        self.wire = TeamsWire(self.platform)
        self.adapter = TeamsAdapter(
            clock=self.clock,
            app_id=APP_ID,
            app_secret=app_secret or self.wire.secret,
            tenant_id=TENANT_ID,
            listener=ListenerSettings(port=0, max_body_bytes=MAX_BODY_BYTES, path=WEBHOOK_PATH),
            options=options,
            book=ConversationBook(book),
            transport=self.wire.transport(),
            serve=False,
            send_only=send_only,
        )
        self.wire.attach(self.adapter.listener.app)
        self.events: list[InboundEvent] = []
        self.adapter.set_sink(self.sink)

    async def sink(self, event: InboundEvent) -> None:
        self.events.append(event)

    async def connect(self) -> None:
        await self.adapter.connect()

    async def deliver(self, activity: dict[str, Any], authorization: str | None = None) -> Any:
        answer = await self.wire.deliver(self.wire.signed(activity, authorization=authorization))
        await self.adapter.listener.idle()
        return answer

    def activity(self, text: str = "hello", **over: Any) -> dict[str, Any]:
        self.platform.dm(ANA, text, user_name="Ana")
        item = self.platform.drain_inbound()[-1]
        activity = self.wire.activity(item)
        activity.update(over)
        return activity


# -- the row's Verify -----------------------------------------------------------------------------


def test_a_token_for_another_app_is_refused_with_401_and_nothing_is_parsed() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        bad = rig.wire.bearer(aud="00000000-0000-4000-8000-0000000000ff")
        answer = await rig.deliver(rig.activity(), authorization=bad)
        assert answer.status_code == 401
        assert rig.events == [] and len(rig.adapter.book) == 0
        # The same activity with a token for us goes through.
        answer = await rig.deliver(rig.activity("again"))
        assert answer.status_code == 200
        assert [e.text for e in rig.events] == ["again"]
        await rig.adapter.disconnect()

    run(main())


@pytest.mark.parametrize(
    "authorization",
    ["", "Bearer", "Bearer x.y.z", "Basic Zm9vOmJhcg=="],
)
def test_requests_without_a_valid_bot_framework_token_are_refused(authorization: str) -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        answer = await rig.deliver(rig.activity(), authorization=authorization)
        assert answer.status_code == 401 and rig.events == []
        await rig.adapter.disconnect()

    run(main())


def test_a_service_url_off_the_allow_list_is_never_stored_or_called() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        evil = "https://evil.example/teams/"
        # Even with a token whose claim matches, the activity is dropped and nothing remembered.
        answer = await rig.deliver(
            rig.activity(serviceUrl=evil), authorization=rig.wire.bearer(serviceUrl=evil)
        )
        assert answer.status_code == 400
        assert rig.events == [] and len(rig.adapter.book) == 0
        # A token minted for one service URL cannot carry an activity naming another.
        answer = await rig.deliver(rig.activity(serviceUrl=EMEA))
        assert answer.status_code == 401
        # Outbound: a configured service URL off the list is refused at build time.
        with pytest.raises(ValueError, match="service_url"):
            TeamsOptions.read({"service_url": evil})
        await rig.adapter.disconnect()

    run(main())


@pytest.mark.parametrize(
    "url",
    [
        "http://smba.trafficmanager.net/teams/",
        "https://smba.trafficmanager.net:8443/teams/",
        "https://user:pw@smba.trafficmanager.net/teams/",
        "https://smba.trafficmanager.net.evil.example/teams/",
        "https://evil-smba.trafficmanager.net/teams/",
        "https://attacker.trafficmanager.net/teams/",
        "https://127.0.0.1/teams/",
        "https://169.254.169.254/latest/",
        "https://smba.trafficmanager.net/teams/../../x/",
        "https://smba.trafficmanager.net/teams/?x=1",
        "",
    ],
)
def test_service_url_allow_list_refuses_lookalikes(url: str) -> None:
    assert service_url_ok(url) is None


def test_service_url_allow_list_accepts_microsoft_hosts_and_operator_hosts() -> None:
    assert service_url_ok("https://smba.trafficmanager.net/emea") == EMEA
    assert service_url_ok("https://SMBA.trafficmanager.net/amer/") == (
        "https://smba.trafficmanager.net/amer/"
    )
    assert service_url_ok("https://smba.infra.gov.teams.microsoft.us/")
    assert service_url_ok("https://bots.example.cn/x/") is None
    assert service_url_ok("https://bots.example.cn/x/", ["bots.example.cn"]) == (
        "https://bots.example.cn/x/"
    )
    with pytest.raises(ValueError):
        TeamsOptions.read({"extra_service_hosts": ["bad host/"]})


def test_proactive_send_uses_the_stored_reference_after_a_restart(tmp_path: Path) -> None:
    book = tmp_path / "gateway" / "teams_conversations.json"

    async def main() -> None:
        rig = Rig(book=book)
        await rig.connect()
        activity = rig.activity("hi from emea", serviceUrl=EMEA)
        activity["conversation"]["id"] = CHANNEL
        activity["conversation"]["conversationType"] = "channel"
        answer = await rig.deliver(activity, authorization=rig.wire.bearer(serviceUrl=EMEA))
        assert answer.status_code == 200
        await rig.adapter.disconnect()
        assert book.stat().st_mode & 0o777 == 0o600
        stored = json.loads(book.read_text())
        assert stored[0]["conversation_id"] == CHANNEL
        assert stored[0]["service_url"] == EMEA

        # A new process (cron, `indus send`): send-only, the reference read from disk.
        later = Rig(book=book, send_only=True)
        await later.connect()
        result = await later.adapter.send(CHANNEL, "nightly report")
        assert result.ok
        assert later.wire.regions == ["emea"]
        assert later.platform.texts(CHANNEL) == ["nightly report"]
        # A conversation never seen goes through the configured default service URL.
        assert (await later.adapter.send("a:1unknown", "hello")).ok
        assert later.wire.regions == ["emea", "teams"]
        assert not later.adapter.listening
        await later.adapter.disconnect()

    run(main())


# -- inbound ---------------------------------------------------------------------------------------


def test_inbound_shapes_mentions_threads_and_identity() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        p = rig.platform
        p.group(CHANNEL, ANA, "/status please", mention=True)
        p.thread(CHANNEL, "1700", ANA, "in the chain")
        await rig.wire.pump()
        await rig.adapter.listener.idle()
        command, threaded = rig.events
        assert command.text == "/status please" and command.metadata["mentioned"] is True
        assert command.source.chat_type == "group"
        assert command.source.user_id == ANA and command.source.user_id_alt == f"29:{ANA}"
        assert command.source.scope_id == TENANT_ID
        assert threaded.source.chat_type == "thread"
        assert threaded.source.chat_id == CHANNEL and threaded.source.thread_id == "1700"
        assert threaded.source.parent_chat_id == CHANNEL
        # The answer goes into the same reply chain.
        sent = await rig.adapter.send(CHANNEL, "done", meta={"thread_id": "1700"})
        assert sent.ok
        assert p.messages(CHANNEL)[-1].thread_id == "1700"
        await rig.adapter.disconnect()

    run(main())


def test_our_own_messages_duplicates_and_other_activity_types_are_dropped() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        activity = rig.activity("once")
        assert (await rig.deliver(activity)).status_code == 200
        assert (await rig.deliver(activity)).status_code == 200  # a Bot Connector retry
        echo = rig.activity("echo")
        echo["from"] = {"id": f"28:{APP_ID}", "name": BOT_NAME}
        await rig.deliver(echo)
        update = rig.activity("joined", type="conversationUpdate")
        await rig.deliver(update)
        assert [e.text for e in rig.events] == ["once"]
        # conversationUpdate still refreshed the reference used for proactive sends.
        assert rig.adapter.book.get(ANA) is not None
        await rig.adapter.disconnect()

    run(main())


def test_a_key_not_endorsed_for_the_channel_is_refused() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        answer = await rig.deliver(rig.activity(channelId="skype"))
        assert answer.status_code == 401 and rig.events == []
        await rig.adapter.disconnect()

    run(main())


def test_a_card_press_is_acknowledged_and_handed_to_the_core() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        data = approval_callback("req9", "deny")
        rig.platform.button_press(ANA, ANA, data)
        answers = await rig.wire.pump()
        await rig.adapter.listener.idle()
        assert answers[0].status_code == 200
        assert answers[0].json()["type"] == "application/vnd.microsoft.activity.message"
        (press,) = rig.events
        assert press.metadata["button"] == data and press.user_id == ANA
        # Somebody else's data, or none, is answered but never becomes an event.
        rig.platform.button_press(ANA, ANA, "x" * 80)
        rig.platform.button_press(ANA, ANA, "")
        await rig.wire.pump()
        await rig.adapter.listener.idle()
        assert len(rig.events) == 1
        await rig.adapter.disconnect()

    run(main())


def test_attachments_become_media_on_microsoft_hosts_only() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        activity = rig.activity("files")
        activity["attachments"] = [
            {"contentType": "text/html", "content": "<p>files</p>"},
            {
                "contentType": "image/png",
                "contentUrl": f"{SERVICE_URL}v3/attachments/a1/views/original",
            },
            {"contentType": "image/png", "contentUrl": "https://evil.example/a.png"},
            {"contentType": "image/png", "contentUrl": "http://smba.trafficmanager.net/x.png"},
            {
                "contentType": "application/vnd.microsoft.teams.file.download.info",
                "name": "plan.docx",
                "content": {"downloadUrl": f"{FILES_HOST}/sites/files/plan", "fileType": "docx"},
            },
            {"contentType": "application/vnd.microsoft.card.adaptive", "content": {}},
        ]
        await rig.deliver(activity)
        (event,) = rig.events
        assert event.kind is MessageKind.DOCUMENT  # a document wins over a picture
        assert [a.ref for a in event.media] == [
            f"{SERVICE_URL}v3/attachments/a1/views/original",
            f"{FILES_HOST}/sites/files/plan",
        ]
        await rig.adapter.disconnect()

    run(main())


def test_the_fetcher_sends_the_bot_token_only_to_bot_framework_hosts() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        rig.wire.files["a1"] = b"PNGDATA"
        registered: dict[str, Fetcher] = {}

        class Seat:
            def register_fetcher(self, platform: str, fetcher: Fetcher) -> Callable[[], None]:
                registered[platform] = fetcher

                def undo() -> None:
                    registered.pop(platform, None)

                return undo

        undo = rig.adapter.register_fetchers(Seat())
        assert undo is not None
        fetch = registered["teams"]

        async def read(ref: str) -> bytes:
            return b"".join([chunk async for chunk in fetch(Attachment(ref=ref))])

        assert await read(f"{SERVICE_URL}v3/attachments/a1/views/original") == b"PNGDATA"
        assert await read(f"{FILES_HOST}/sites/files/plan") == b"%PDF-1.7 fake"
        bot, files = rig.wire.downloads
        assert bot.headers["authorization"].startswith("Bearer ")
        assert "authorization" not in files.headers
        for ref in ("https://evil.example/x", "http://contoso.sharepoint.com/x", "file:///etc/x"):
            with pytest.raises(ValueError):
                await read(ref)
        assert len(rig.wire.downloads) == 2
        undo()
        assert registered == {}
        await rig.adapter.disconnect()

    run(main())


def test_media_access_rules() -> None:
    assert media_access("https://smba.trafficmanager.net/teams/v3/attachments/x") == "token"
    assert media_access("https://us-api.asm.skype.com/v1/objects/x") == "token"
    assert media_access("https://contoso.sharepoint.com/x") == "open"
    assert media_access("https://contoso.sharepoint.com.evil.example/x") is None
    assert media_access("https://smba.trafficmanager.net:444/x") is None
    assert media_access("https://u@contoso.sharepoint.com/x") is None


def test_conversation_ids_cannot_escape_the_url_path() -> None:
    assert conversation_ok("19:abc@thread.tacv2;messageid=1712")
    assert conversation_ok("a:1Ab-Cd_e.f")
    for bad in ("../x", "a/b", "a?b", "a#b", "19:x;messageid=abc", "a b", "", "a%2Fb"):
        assert not conversation_ok(bad)

    async def main() -> None:
        rig = Rig()
        await rig.connect()
        result = await rig.adapter.send("../../admin", "x")
        assert not result.ok and rig.wire.bodies == []
        result = await rig.adapter.edit(ANA, "../1", "x", final=True)
        assert not result.ok
        await rig.adapter.disconnect()

    run(main())


# -- outbound --------------------------------------------------------------------------------------


def test_approval_cards_carry_our_callbacks_and_fall_back_to_text() -> None:
    prompt = ApprovalPrompt(
        request_id="req1",
        session_key="k",
        text="Run `rm -rf build`?",
        fallback_text="Run `rm -rf build`?\nReply /approve or /deny.",
        actions=(
            ApprovalAction("Allow once", "once", "primary"),
            ApprovalAction("Deny", "deny", "danger"),
        ),
    )

    async def main() -> None:
        rig = Rig()
        await rig.connect()
        result = await rig.adapter.send_approval(ANA, prompt)
        assert result.ok
        _, body = rig.wire.bodies[-1]
        card = body["attachments"][0]["content"]
        assert card["version"] == "1.4" and card["fallbackText"] == prompt.fallback_text
        assert [a["style"] for a in card["actions"]] == ["positive", "destructive"]
        assert [a["data"]["indus_callback"] for a in card["actions"]] == [
            approval_callback("req1", "once"),
            approval_callback("req1", "deny"),
        ]
        rig.wire.refuse_cards = True
        result = await rig.adapter.send_approval(ANA, prompt)
        assert result.ok
        assert "/approve" in rig.platform.texts(ANA)[-1]
        await rig.adapter.disconnect()

    run(main())


def test_edits_typing_and_long_replies() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        sent = await rig.adapter.send(ANA, "draft")
        assert sent.ok and sent.message_id
        edited = await rig.adapter.edit(ANA, sent.message_id, "final", final=True)
        assert edited.ok and rig.platform.message(sent.message_id).text == "final"  # type: ignore[union-attr]
        await rig.adapter.typing_start(ANA)
        assert [op.kind for op in rig.platform.ops].count("typing") == 1
        long = " ".join(f"w{n}" for n in range(12_000))
        result = await rig.adapter.send(ANA, long)
        assert result.ok and len(result.ids) >= 2
        for _, body in rig.wire.bodies:
            assert len(body.get("text") or "") <= MAX_LEN
        await rig.adapter.disconnect()

    run(main())


def test_images_go_inline_and_other_files_are_refused(tmp_path: Path) -> None:
    picture = tmp_path / "chart.png"
    picture.write_bytes(b"\x89PNG\r\n" + b"0" * 100)
    big = tmp_path / "big.png"
    big.write_bytes(b"0" * (1024 * 1024 + 1))
    doc = tmp_path / "notes.pdf"
    doc.write_bytes(b"%PDF")

    async def main() -> None:
        rig = Rig()
        await rig.connect()
        sent = await rig.adapter.send_image(ANA, str(picture), caption="the chart")
        assert sent.ok
        _, body = rig.wire.bodies[-1]
        attachment = body["attachments"][0]
        assert attachment["contentType"] == "image/png"
        assert attachment["contentUrl"].startswith("data:image/png;base64,")
        assert rig.platform.ops_of("media")[-1].text == "the chart"
        too_big = await rig.adapter.send_image(ANA, str(big))
        assert not too_big.ok and "too large" in (too_big.error or "")
        refused = await rig.adapter.send_document(ANA, str(doc))
        assert refused.unsupported
        await rig.adapter.disconnect()

    run(main())


def test_a_reply_to_our_message_is_marked_as_addressed_to_us() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        sent = await rig.adapter.send(CHANNEL, "posted by the bot")
        assert sent.ok and sent.message_id
        rig.platform.thread(CHANNEL, sent.message_id, ANA, "answering the bot")
        rig.platform.thread(CHANNEL, "999", ANA, "someone else's chain")
        await rig.wire.pump()
        await rig.adapter.listener.idle()
        ours, other = rig.events
        assert ours.reply_to_is_own and not other.reply_to_is_own
        await rig.adapter.disconnect()

    run(main())


def test_errors_are_classified_from_the_bot_framework_codes() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        rig.platform.fail(FailureKind.CHAT_NOT_FOUND, op="send")
        gone = await rig.adapter.send(ANA, "x")
        assert gone.kind is ErrorKind.NOT_FOUND and gone.chat_unreachable
        rig.platform.fail(FailureKind.THREAD_NOT_FOUND, op="send")
        sub = await rig.adapter.send(ANA, "x")
        assert sub.kind is ErrorKind.NOT_FOUND and not sub.chat_unreachable
        rig.platform.fail(FailureKind.RATE_LIMIT, op="send", retry_after=9)
        limited = await rig.adapter.send(ANA, "x")
        assert limited.kind is ErrorKind.RATE_LIMITED and limited.retry_after == 9
        await rig.adapter.disconnect()

    run(main())


def test_the_token_is_cached_and_refreshed_once_after_a_401() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        assert rig.wire.token_requests == 1
        for n in range(3):
            assert (await rig.adapter.send(ANA, f"m{n}")).ok
        assert rig.wire.token_requests == 1
        rig.wire.issued.append("revoked")  # the held token no longer matches
        assert (await rig.adapter.send(ANA, "after rotation")).ok
        assert rig.wire.token_requests == 2
        # Near the end of its life the token is replaced before use.
        rig.clock.advance(3599 - 299)
        assert (await rig.adapter.send(ANA, "later")).ok
        assert rig.wire.token_requests == 3
        await rig.adapter.disconnect()

    run(main())


def test_concurrent_sends_share_one_token_request() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        rig.adapter.credentials.invalidate()
        results = await asyncio.gather(*(rig.adapter.send(f"chat{n}", "hi") for n in range(5)))
        assert all(r.ok for r in results)
        assert rig.wire.token_requests == 2  # connect, then one refresh for all five
        await rig.adapter.disconnect()

    run(main())


def test_wrong_credentials_are_fatal_at_connect() -> None:
    async def main() -> None:
        rig = Rig(app_secret="".join(["not", "-the-", "secret", "1234"]))
        with pytest.raises(AdapterConnectError) as caught:
            await rig.adapter.connect()
        assert not caught.value.retryable and caught.value.code == "teams_auth"
        assert "invalid_client" in str(caught.value)
        assert not rig.adapter.connected

    run(main())


def test_secrets_and_tokens_never_reach_logs_errors_or_reprs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="httpx")
    caplog.set_level(logging.DEBUG, logger="httpcore")

    async def main() -> tuple[Rig, list[str]]:
        rig = Rig()
        await rig.connect()
        texts: list[str] = []
        texts.append(repr(rig.adapter))
        texts.append(repr(rig.adapter.credentials))
        rig.platform.fail(FailureKind.FORBIDDEN, op="send")
        failed = await rig.adapter.send(ANA, "x")
        texts.append(str(failed.error))
        texts.append(rig.adapter.scrub(f"oops {rig.wire.issued[-1]}"))
        await rig.deliver(rig.activity(), authorization=rig.wire.bearer(aud="other"))
        await rig.adapter.disconnect()
        return rig, texts

    rig, texts = run(main())
    blob = "\n".join([*texts, caplog.text])
    assert rig.wire.secret not in blob
    for issued in rig.wire.issued:
        assert issued not in blob


# -- building it -----------------------------------------------------------------------------------


def _config(scope: Scope, **extra: Any) -> AdapterConfig:
    from indus.foundation.config.schema import PlatformSection

    settings = PlatformSettings("teams", True, "credentials", PlatformSection(extra=extra))
    return AdapterConfig("teams", settings, scope, scope.clock)


def test_create_adapter_reads_the_secrets_and_refuses_without_them(scope: Scope) -> None:
    with pytest.raises(ValueError, match="TEAMS_CLIENT_ID"):
        create_adapter(_config(scope))
    secrets: Any = scope.secrets
    secrets.set("TEAMS_CLIENT_ID", APP_ID)
    secrets.set("TEAMS_CLIENT_SECRET", secret())
    secrets.set("TEAMS_TENANT_ID", TENANT_ID)
    adapter = create_adapter(_config(scope))
    assert adapter.app_id == APP_ID
    assert adapter.listener.settings.port == 3978 and adapter.listener.settings.host == "127.0.0.1"
    assert adapter.book.path == scope.home / "gateway" / "teams_conversations.json"
    with pytest.raises(ValueError):
        create_adapter(_config(scope, webhook_host="0.0.0.0"))
    with pytest.raises(ValueError):
        create_adapter(_config(scope, service_url="https://evil.example/"))
    secrets.set("TEAMS_SERVICE_URL", EMEA)
    assert create_adapter(_config(scope)).options.service_url == EMEA
    secrets.set("TEAMS_SERVICE_URL", "https://evil.example/")
    with pytest.raises(ValueError):
        create_adapter(_config(scope))


def test_indus_send_over_the_wire_does_no_inbound_work(
    capsys: pytest.CaptureFixture[str], scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    from indus.gateway.delivery import send_command
    from indus.gateway.registry import PlatformEntry, PlatformRegistry, modules_installed
    from tests.unit.cli.commands.cmdrun import run_cli, write_settings

    write_settings(scope, "platforms:\n  teams:\n    enabled: true\n")
    clock: Any = scope.clock
    platform = FakeChatPlatform("teams", clock=clock, max_len=MAX_LEN)
    wire = TeamsWire(platform)
    secrets: Any = scope.secrets
    secrets.set("TEAMS_CLIENT_ID", APP_ID)
    secrets.set("TEAMS_CLIENT_SECRET", wire.secret)
    secrets.set("TEAMS_TENANT_ID", TENANT_ID)
    built: list[TeamsAdapter] = []
    real_build = send_command.build_adapter

    def factory(config: AdapterConfig) -> TeamsAdapter:
        adapter = create_adapter(config, transport=wire.transport())
        built.append(adapter)
        return adapter

    # The built-in row's shape with the transport swapped in (the real row: delivery tests).
    row = PlatformEntry(
        name="teams",
        label="Microsoft Teams",
        factory=factory,
        check_fn=modules_installed("httpx"),
        required_env=("TEAMS_CLIENT_ID", "TEAMS_CLIENT_SECRET", "TEAMS_TENANT_ID"),
        max_len=MAX_LEN,
    )

    def build(build_scope: Scope, name: str, settings: PlatformSettings, *_: Any) -> Any:
        return real_build(build_scope, name, settings, PlatformRegistry([row]))

    monkeypatch.setattr(send_command, "ADAPTER_BUILDER", build)
    ran = run_cli(capsys, scope, "send", "teams:a1conversation", "deploy done")
    assert ran.code == 0, ran.err
    assert platform.texts("a1conversation") == ["deploy done"]
    assert [op.kind for op in platform.ops] == ["connect", "send", "disconnect"]
    assert built[0].send_only and not built[0].listening
    assert wire.key_requests == 0
