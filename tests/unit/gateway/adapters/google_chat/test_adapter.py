"""The Google Chat adapter (P16-13) over the fake Google."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from indus.foundation.config.schema import PlatformSection
from indus.foundation.scope import Scope
from indus.gateway.adapters._http import ListenerError, ListenerSettings
from indus.gateway.adapters.base import (
    AdapterConnectError,
    ApprovalAction,
    ApprovalPrompt,
    ClarifyPrompt,
    approval_callback,
)
from indus.gateway.adapters.google_chat import create_adapter
from indus.gateway.adapters.google_chat.adapter import (
    MAX_BODY_BYTES,
    GoogleChatAdapter,
    GoogleChatOptions,
    is_trusted_media_url,
)
from indus.gateway.adapters.google_chat.client import CHAT_ROUTES, PUBSUB_ROUTES
from indus.gateway.config import AdapterConfig, PlatformSettings
from indus.gateway.core.events import Attachment, InboundEvent, MessageKind
from indus.gateway.core.send import ErrorKind
from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import FailureKind, FakeChatPlatform
from tests.fakes.platform.google_chat_wire import (
    CALLBACK_PATH,
    ENDPOINT_URL,
    MAX_LEN,
    PROJECT_NUMBER,
    SUBSCRIPTION,
    GoogleChatWire,
    rsa_key,
    service_account_json,
    sign_jwt,
)

SPACE = "spaces/G300"
ANA = "users/200"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("real network used in a Google Chat adapter test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield


def _clock() -> FakeClock:
    return FakeClock(datetime(2026, 1, 1, 9, 0, tzinfo=UTC))


async def drive[T](clock: FakeClock, awaitable: Awaitable[T], *, step: float = 0.5) -> T:
    task = asyncio.ensure_future(awaitable)
    for _ in range(4000):
        await clock.run_until_idle()
        if task.done():
            return task.result()
        clock.advance(step)
    task.cancel()
    raise AssertionError("never finished on the fake clock")


async def settle(clock: FakeClock, rounds: int = 4) -> None:
    for _ in range(rounds):
        await clock.advance_and_settle(1.0)


def run[T](coro: Awaitable[T]) -> T:
    return asyncio.run(coro)  # pyright: ignore[reportArgumentType]


class Rig:
    def __init__(
        self,
        *,
        mode: str = "http",
        flavour: Any = "project_number",
        sink: bool = True,
        send_only: bool = False,
        options: GoogleChatOptions | None = None,
    ) -> None:
        self.clock = _clock()
        self.platform = FakeChatPlatform("google_chat", clock=self.clock, max_len=MAX_LEN)
        audience = PROJECT_NUMBER if flavour == "project_number" else ENDPOINT_URL
        self.wire = GoogleChatWire(self.platform, audience=audience, flavour=flavour)
        if options is None:
            options = (
                GoogleChatOptions(mode="pubsub", subscription=SUBSCRIPTION)
                if mode == "pubsub"
                else GoogleChatOptions(mode="http", audience=audience)
            )
        self.adapter = GoogleChatAdapter(
            clock=self.clock,
            account_json=self.wire.account_json,
            options=options,
            listener=ListenerSettings(port=0, max_body_bytes=MAX_BODY_BYTES, path=CALLBACK_PATH),
            transport=self.wire.transport(),
            serve=False,
            send_only=send_only,
            rng=lambda: 0.0,
        )
        if self.adapter.listener is not None:
            self.wire.attach(self.adapter.listener.app)
        self.events: list[InboundEvent] = []
        self.fatal: list[tuple[str, str]] = []
        self.adapter.set_fatal_listener(lambda code, message: self.fatal.append((code, message)))
        if sink:
            self.adapter.set_sink(self.sink)

    async def sink(self, event: InboundEvent) -> None:
        self.events.append(event)

    async def connect(self) -> None:
        await drive(self.clock, self.adapter.connect())

    async def close(self) -> None:
        await drive(self.clock, self.adapter.disconnect())

    async def post(self, event: dict[str, Any], *, token: str | None = None) -> httpx.Response:
        answer = await self.wire.deliver(self.wire.callback(event, token=token))
        assert self.adapter.listener is not None
        await self.adapter.listener.idle()
        return answer


def _message(
    *,
    name: str = f"{SPACE}/messages/m1",
    space_type: str = "SPACE",
    text: str = "hello",
    **message: Any,
) -> dict[str, Any]:
    space = {"name": SPACE, "spaceType": space_type, "displayName": "Ops"}
    sender = {"name": ANA, "displayName": "Ana", "type": "HUMAN", "email": "Ana@Example.org"}
    body = {"name": name, "sender": sender, "text": text, "argumentText": text, **message}
    return {"type": "MESSAGE", "space": space, "message": body, "user": sender}


# -- the row's Verify -----------------------------------------------------------------------------


def test_a_forged_token_is_refused_with_401_before_the_body_is_read() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        stranger = rsa_key("stranger")
        now = int(rig.clock.now().timestamp())
        forged = sign_jwt(
            stranger,
            {"alg": "RS256", "kid": "gk1"},
            {
                "iss": "chat@system.gserviceaccount.com",
                "aud": PROJECT_NUMBER,
                "iat": now,
                "exp": now + 600,
            },
        )
        answer = await rig.post(_message(), token=forged)
        assert answer.status_code == 401 and answer.json() == {"error": "unauthorized"}
        # Not even a malformed body is looked at without a good token.
        raw = await rig.wire.deliver(
            rig.wire.callback({}, token=forged).__class__(CALLBACK_PATH, b"{not json", {})
        )
        assert raw.status_code == 401
        assert rig.events == []
        good = await rig.post(_message())
        assert good.status_code == 200 and [e.text for e in rig.events] == ["hello"]
        await rig.close()

    run(main())


def test_while_the_gateway_drains_callbacks_get_503_and_the_redelivery_is_admitted() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        draining = ["maintenance"]
        rig.adapter.set_drain_probe(lambda: draining[0] if draining else None)
        refused = await rig.post(_message())
        assert refused.status_code == 503 and refused.headers["retry-after"] == "30"
        assert rig.events == []
        draining.clear()
        again = await rig.post(_message())
        assert again.status_code == 200 and [e.text for e in rig.events] == ["hello"]
        assert (await rig.post(_message())).status_code == 200
        assert len(rig.events) == 1  # the third delivery is a duplicate
        await rig.close()

    run(main())


def test_an_attachment_from_an_untrusted_host_is_never_fetched() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        for url in (
            "https://evil.example/file",
            "http://chat.googleapis.com/v1/media/x",
            "https://googleapis.com.evil.example/x",
            "https://user:pw@chat.googleapis.com/x",
            "ftp://drive.google.com/x",
        ):
            with pytest.raises(ValueError):
                _ = [c async for c in rig.adapter.fetch_media(Attachment(ref=url))]
        assert not any("evil" in host for host in rig.wire.hosts)
        assert set(rig.wire.hosts) == {"oauth2.googleapis.com"}
        await rig.close()

    run(main())
    assert is_trusted_media_url("https://lh3.googleusercontent.com/a/b")
    assert is_trusted_media_url("https://chat.google.com/api/get_attachment_url")
    assert not is_trusted_media_url("https://notgoogleapis.com/x")
    assert not is_trusted_media_url("https://storage.googleapis.com/someone/x")
    assert not is_trusted_media_url("https://sites.googleusercontent.com/x")


# -- inbound --------------------------------------------------------------------------------------


def test_message_shapes_threads_mentions_and_identities() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        thread = f"{SPACE}/threads/T1"
        mention = [
            {"type": "USER_MENTION", "userMention": {"user": {"name": "users/x", "type": "BOT"}}}
        ]
        await rig.post(
            _message(
                name=f"{SPACE}/messages/a",
                thread={"name": thread},
                createTime="2026-01-01T08:59:58.123456789Z",
            )
        )
        await rig.post(
            _message(
                name=f"{SPACE}/messages/b",
                thread={"name": thread},
                threadReply=True,
                text="in thread",
            )
        )
        await rig.post(
            _message(
                name=f"{SPACE}/messages/c",
                text="@Indus hi",
                argumentText=" hi",
                annotations=mention,
            )
        )
        await rig.post(_message(name="spaces/D1/messages/d", space_type="DIRECT_MESSAGE"))
        slash = [{"type": "SLASH_COMMAND", "slashCommand": {"commandName": "/status"}}]
        await rig.post(
            _message(
                name=f"{SPACE}/messages/e",
                text="/status now",
                argumentText="now",
                annotations=slash,
            )
        )
        top, threaded, mentioned, dm, command = rig.events
        assert top.source.chat_type == "group" and top.source.thread_id is None
        assert top.message_id == "a" and top.timestamp is not None
        assert top.source.user_id == ANA and top.source.user_id_alt == "ana@example.org"
        assert top.source.user_name == "Ana" and top.source.chat_name == "Ops"
        assert threaded.source.chat_type == "thread" and threaded.source.thread_id == thread
        assert threaded.source.chat_id == SPACE and threaded.source.parent_chat_id == SPACE
        assert mentioned.text == "hi" and mentioned.metadata == {"mentioned": True}
        assert dm.source.chat_type == "dm"
        assert command.text == "/status now" and command.command == "status"
        await rig.close()

    run(main())


def test_card_clicks_add_on_events_and_space_membership() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        data = approval_callback("req1", "once")
        click = {
            "type": "CARD_CLICKED",
            "space": {"name": SPACE, "spaceType": "SPACE"},
            "user": {"name": ANA, "type": "HUMAN"},
            "message": {
                "name": f"{SPACE}/messages/card1",
                "sender": {"name": "users/app", "type": "BOT"},
            },
            "action": {
                "actionMethodName": "indus.button",
                "parameters": [{"key": "data", "value": data}],
            },
        }
        assert (await rig.post(click)).json() == {}
        foreign = {**click, "action": {"actionMethodName": "other", "parameters": []}}
        await rig.post(foreign)
        addon = {
            "chat": {
                "user": {"name": ANA, "type": "HUMAN"},
                "messagePayload": {
                    "space": {"name": SPACE, "spaceType": "SPACE"},
                    "message": {
                        "name": f"{SPACE}/messages/x1",
                        "text": "via add-on",
                        "sender": {"name": ANA, "type": "HUMAN"},
                    },
                },
            }
        }
        await rig.post(addon)
        addon_click = {
            "chat": {
                "user": {"name": ANA, "type": "HUMAN"},
                "buttonClickedPayload": {
                    "space": {"name": SPACE},
                    "message": {"name": f"{SPACE}/messages/card2"},
                },
            },
            "commonEventObject": {"parameters": {"data": data}},
        }
        await rig.post(addon_click)
        await rig.post({"type": "ADDED_TO_SPACE", "space": {"name": SPACE}})
        await rig.post({"type": "REMOVED_FROM_SPACE", "space": {"name": SPACE}})
        await rig.post({"type": "SOMETHING_NEW"})
        pressed, addon_message, addon_pressed = rig.events
        assert pressed.metadata == {"button": data} and pressed.source.user_id == ANA
        assert not pressed.source.is_bot and pressed.message_id == "card1"
        assert addon_message.text == "via add-on"
        assert addon_pressed.metadata == {"button": data}
        await rig.close()

    run(main())


def test_bad_payloads_no_sink_and_bot_senders() -> None:
    async def main() -> None:
        rig = Rig(sink=False)
        await rig.connect()
        assert (await rig.post(_message())).status_code == 503
        rig.adapter.set_sink(rig.sink)
        token = rig.wire.id_token()
        headers = {"Authorization": f"Bearer {token}"}
        cb = rig.wire.callback({})
        for body in (b"{bad", b"[1, 2]"):
            answer = await rig.wire.deliver(cb.__class__(CALLBACK_PATH, body, headers))
            assert answer.status_code == 400
        bot = _message(name=f"{SPACE}/messages/bot")
        bot["message"]["sender"]["type"] = "BOT"
        await rig.post(bot)
        nameless = _message()
        del nameless["space"]
        nameless["message"].pop("space", None)
        await rig.post(nameless)
        assert [e.source.is_bot for e in rig.events] == [True]
        await rig.close()

    run(main())


def test_attachments_become_refs_and_the_fetcher_downloads_them() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        undo = rig.adapter.register_fetchers(_Seat())
        assert undo is not None
        rig.wire.media["res1"] = b"PNG bytes"
        attachments = [
            {"contentType": "image/png", "attachmentDataRef": {"resourceName": "res1"}},
            {"contentType": "application/pdf", "downloadUri": "https://chat.google.com/x"},
            {"contentType": "text/plain", "driveDataRef": {"driveFileId": "f"}},
        ]
        await rig.post(_message(text="", attachment=attachments))
        (event,) = rig.events
        assert event.kind is MessageKind.PHOTO
        assert [a.ref for a in event.media] == ["gchat-media:res1", "https://chat.google.com/x"]
        data = b"".join([c async for c in rig.adapter.fetch_media(event.media[0])])
        assert data == b"PNG bytes"
        small = GoogleChatOptions(mode="http", audience=PROJECT_NUMBER, max_media_bytes=4)
        rig.adapter.options = small
        from indus.gateway.adapters.google_chat.client import MediaTooLarge

        with pytest.raises(MediaTooLarge):
            _ = [c async for c in rig.adapter.fetch_media(event.media[0])]
        with pytest.raises(ValueError):
            _ = [c async for c in rig.adapter.fetch_media(Attachment(ref="gchat-media:../x"))]
        await rig.close()

    run(main())


class _Seat:
    def __init__(self) -> None:
        self.fetchers: dict[str, Any] = {}

    def register_fetcher(self, platform: str, fetcher: Any) -> Callable[[], None]:
        self.fetchers[platform] = fetcher
        return lambda: self.fetchers.pop(platform, None) and None


# -- Pub/Sub --------------------------------------------------------------------------------------


def test_pubsub_pulls_hands_on_and_acknowledges() -> None:
    async def main() -> None:
        rig = Rig(mode="pubsub")
        assert rig.adapter.listener is None
        await rig.connect()
        assert rig.adapter.pulling
        rig.platform.dm(ANA, "first")
        rig.platform.group(SPACE, ANA, "second")
        rig.wire.push_event(_message(name=f"{SPACE}/messages/m1", text="raw"))
        rig.wire.push_event(_message(name=f"{SPACE}/messages/m1", text="raw"))  # redelivered
        await settle(rig.clock)
        assert sorted(e.text for e in rig.events) == ["first", "raw", "second"]
        assert len(rig.wire.acked) == 4
        rig.adapter.set_sink(None)
        await settle(rig.clock)
        assert not rig.adapter.pulling
        await rig.close()

    run(main())


def test_pubsub_waits_while_draining_and_backs_off_on_failures() -> None:
    async def main() -> None:
        rig = Rig(mode="pubsub", sink=False)
        await rig.connect()
        draining = [True]
        rig.adapter.set_drain_probe(lambda: draining[0])
        rig.adapter.set_sink(rig.sink)
        await settle(rig.clock, 12)
        assert rig.wire.pull_calls == 0
        draining[0] = False
        rig.wire.pull_failures = [500, 503, httpx.ConnectError("down")]
        rig.platform.dm(ANA, "after")
        start = rig.clock.monotonic()
        while not rig.events and rig.clock.monotonic() - start < 60:
            await rig.clock.advance_and_settle(0.5)
        assert [e.text for e in rig.events] == ["after"]
        # 2 + 4 + 8 seconds of backoff before the fourth pull.
        assert rig.clock.monotonic() - start >= 14
        assert rig.fatal == []
        await rig.close()

    run(main())


def test_pubsub_refusals_and_repeated_failures_are_fatal() -> None:
    async def main() -> None:
        rig = Rig(mode="pubsub")
        rig.wire.pull_failures = [403]
        await rig.connect()
        await settle(rig.clock)
        assert rig.fatal and rig.fatal[0][0] == "google_chat_pubsub"
        assert not rig.adapter.pulling
        await rig.close()

        rig = Rig(mode="pubsub")
        rig.wire.pull_failures = [500] * 10
        await rig.connect()
        for _ in range(400):
            await rig.clock.advance_and_settle(5.0)
            if rig.fatal:
                break
        assert rig.fatal and "10 times" in rig.fatal[0][1]
        assert rig.wire.pull_calls == 10
        await rig.close()

    run(main())


def test_pubsub_connect_checks_the_subscription() -> None:
    async def main() -> None:
        for status, retryable in ((404, False), (403, False), (500, True)):
            rig = Rig(mode="pubsub")
            rig.wire.subscription_status = status
            with pytest.raises(AdapterConnectError) as caught:
                await rig.connect()
            assert caught.value.retryable is retryable
            assert not rig.adapter.connected

    run(main())


# -- outbound -------------------------------------------------------------------------------------


def test_threads_dm_targets_cards_edits_and_deletes() -> None:
    async def main() -> None:
        rig = Rig()
        rig.platform.add_chat(SPACE, "group", "Ops")
        await rig.connect()
        thread = f"{SPACE}/threads/T9"
        sent = await drive(
            rig.clock, rig.adapter.send(SPACE, "in a thread", meta={"thread_id": thread})
        )
        assert sent.ok
        op = rig.platform.ops_of("send")[-1]
        assert op.thread_id == thread
        assert op.extra["params"] == {"messageReplyOption": "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"}
        plain = await drive(rig.clock, rig.adapter.send(SPACE, "top level"))
        assert rig.platform.ops_of("send")[-1].extra["params"] == {}
        for _ in range(2):
            dm = await drive(rig.clock, rig.adapter.send(ANA, "hi there"))
            assert dm.ok
        assert rig.platform.texts(ANA) == ["hi there", "hi there"]
        finds = [c for c in rig.wire.calls if c.path.endswith(":findDirectMessage")]
        assert len(finds) == 1  # cached
        prompt = ApprovalPrompt(
            "r1",
            "k",
            "Run it?",
            "Run it? /approve or /deny",
            (ApprovalAction("Allow once", "once"), ApprovalAction("Deny", "deny")),
        )
        card = await drive(rig.clock, rig.adapter.send_approval(SPACE, prompt))
        assert card.ok and card.message_id
        message = rig.platform.message(card.message_id)
        assert message is not None
        assert [b.data for b in message.buttons] == [
            approval_callback("r1", "once"),
            approval_callback("r1", "deny"),
        ]
        body = rig.wire.calls_to("POST", f"/v1/{SPACE}/messages")[-1].json()
        button = body["cardsV2"][0]["card"]["sections"][0]["widgets"][1]["buttonList"]["buttons"][0]
        assert button["onClick"]["action"]["function"] == "indus.button"
        edited = await drive(
            rig.clock, rig.adapter.edit(SPACE, card.message_id, "Approved", final=True)
        )
        assert edited.ok and message.text == "Approved" and message.buttons == ()
        patch = rig.wire.calls_to("PATCH")[-1]
        assert patch.params["updateMask"] == "text,cardsV2"
        clarify = ClarifyPrompt("c1", "k", "Which?", ("a", "b" * 100))
        asked = await drive(rig.clock, rig.adapter.send_clarify(SPACE, clarify))
        labels = [b.label for b in rig.platform.message(asked.message_id or "").buttons]  # type: ignore[union-attr]
        assert labels[0] == "a" and len(labels[1]) == 80
        assert plain.message_id
        assert await drive(rig.clock, rig.adapter.delete(SPACE, plain.message_id))
        info = await drive(rig.clock, rig.adapter.chat_info(SPACE))
        assert info.type == "group"
        long = await drive(rig.clock, rig.adapter.edit(SPACE, plain.message_id, "x" * 4001))
        assert not long.ok and long.kind is ErrorKind.TOO_LONG
        await rig.close()

    run(main())


def test_send_errors_are_classified() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        bad = await drive(rig.clock, rig.adapter.send("rooms/../x", "hi"))
        assert bad.kind is ErrorKind.NOT_FOUND and bad.chat_unreachable
        rig.platform.fail(FailureKind.CHAT_NOT_FOUND, op="edit")
        missing = await drive(rig.clock, rig.adapter.edit(SPACE, "m9", "x", final=True))
        assert missing.kind is ErrorKind.NOT_FOUND
        await rig.close()

    run(main())


def test_a_401_refreshes_the_token_once_then_is_final() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        assert rig.wire.tokens_issued == 1
        rig.wire.access_token = "-".join(["ya29", "rotated", "token"])
        sent = await drive(rig.clock, rig.adapter.send(SPACE, "after rotation"))
        assert sent.ok and rig.wire.tokens_issued == 2
        original = rig.wire.access_token

        async def refuse(call: Any, space: str) -> httpx.Response:
            return httpx.Response(401, json={"error": {"code": 401, "message": "no"}})

        rig.wire._apis = [  # pyright: ignore[reportPrivateUsage]
            api
            if "messages" not in api.pattern.pattern or api.method != "POST"
            else api.__class__(api.method, api.pattern, refuse)
            for api in rig.wire._apis  # pyright: ignore[reportPrivateUsage]
        ]
        failed = await drive(rig.clock, rig.adapter.send_with_retry(SPACE, "x", rng=lambda: 0.0))
        assert not failed.ok and rig.adapter.is_final_failure(failed)
        assert not failed.chat_unreachable
        assert original not in (failed.error or "")
        await rig.close()

    run(main())


def test_media_sends_are_refused_cleanly(tmp_path: Any) -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        path = tmp_path / "a.png"
        path.write_bytes(b"x")
        result = await drive(rig.clock, rig.adapter.send_image(SPACE, str(path)))
        assert result.unsupported and rig.platform.ops_of("media") == []
        await rig.close()

    run(main())


# -- lifecycle ------------------------------------------------------------------------------------


def test_send_only_fetches_a_token_and_nothing_else() -> None:
    async def main() -> None:
        for mode in ("http", "pubsub"):
            rig = Rig(mode=mode, send_only=True)
            assert rig.adapter.listener is None
            await rig.connect()
            assert not rig.adapter.pulling
            await drive(rig.clock, rig.adapter.send(SPACE, "deploy done"))
            await rig.close()
            assert [op.kind for op in rig.platform.ops] == ["connect", "send", "disconnect"]
            assert not any(":pull" in c.path for c in rig.wire.calls)

    run(main())


def test_configuration_problems_are_fatal_connect_errors() -> None:
    async def main() -> None:
        cases = [
            GoogleChatOptions(mode="http", audience=""),
            GoogleChatOptions(mode="pubsub", subscription="my-subscription"),
        ]
        for options in cases:
            rig = Rig(options=options)
            with pytest.raises(AdapterConnectError) as caught:
                await rig.connect()
            assert not caught.value.retryable and caught.value.code == "config"
        rig = Rig()
        rig.adapter._account_json = "not json"  # pyright: ignore[reportPrivateUsage]
        with pytest.raises(AdapterConnectError) as caught:
            await rig.connect()
        assert not caught.value.retryable
        rig = Rig()
        rig.platform.fail_connect(FailureKind.CONNECT_TIMEOUT)
        with pytest.raises(AdapterConnectError) as caught:
            await rig.connect()
        assert caught.value.retryable

    run(main())


def test_the_url_flavour_end_to_end() -> None:
    async def main() -> None:
        rig = Rig(flavour="url")
        await rig.connect()
        assert (await rig.post(_message())).status_code == 200
        assert rig.wire.jwks_fetches == 1
        token = rig.wire.id_token(email="someone@example.org")
        assert (
            await rig.post(_message(name=f"{SPACE}/messages/z"), token=token)
        ).status_code == 401
        assert len(rig.events) == 1
        await rig.close()

    run(main())


def test_create_adapter_reads_secrets_and_settings(scope: Scope) -> None:
    secrets: Any = scope.secrets
    secrets.set("GOOGLE_CHAT_SERVICE_ACCOUNT_JSON", service_account_json())
    secrets.set("GOOGLE_CHAT_HTTP_EVENTS_AUDIENCE", PROJECT_NUMBER)
    section = PlatformSection.model_validate({"webhook_port": 9001, "token_email": "a@b.c, d@e.f"})
    settings = PlatformSettings("google_chat", True, "credentials", section)
    adapter = create_adapter(AdapterConfig("google_chat", settings, scope, scope.clock, None))
    assert adapter.options.mode == "http" and adapter.options.audience == PROJECT_NUMBER
    assert adapter.options.token_emails == ("a@b.c", "d@e.f")
    assert adapter.listener is not None and adapter.listener.settings.port == 9001
    assert adapter.listener.settings.host == "127.0.0.1"
    key = json.loads(service_account_json())["private_key"]
    assert key[30:90] not in adapter.scrub(f"oops {key}")
    sub = PlatformSection.model_validate({"subscription": SUBSCRIPTION})
    pubsub = create_adapter(
        AdapterConfig(
            "google_chat",
            PlatformSettings("google_chat", True, "x", sub),
            scope,
            scope.clock,
            None,
            send_only=True,
        )
    )
    assert pubsub.options.mode == "pubsub" and pubsub.send_only and pubsub.listener is None
    public = PlatformSection.model_validate({"webhook_host": "0.0.0.0"})
    with pytest.raises(ListenerError):
        create_adapter(
            AdapterConfig(
                "google_chat",
                PlatformSettings("google_chat", True, "x", public),
                scope,
                scope.clock,
                None,
            )
        )


def test_secrets_never_reach_logs_errors_or_reprs(caplog: pytest.LogCaptureFixture) -> None:
    async def main() -> tuple[str, str]:
        rig = Rig()
        await rig.connect()
        key = json.loads(rig.wire.account_json)["private_key"]
        token = rig.wire.access_token
        assert token not in repr(rig.adapter) and "PRIVATE" not in repr(rig.adapter)
        rig.platform.fail(FailureKind.FORBIDDEN, op="send")
        failed = await drive(rig.clock, rig.adapter.send(SPACE, "x"))
        assert failed.kind is ErrorKind.FORBIDDEN and token not in (failed.error or "")
        await rig.post(_message(), token="garbage")
        await rig.close()
        return key, token

    with caplog.at_level(logging.DEBUG):
        key, token = run(main())
    for record in caplog.records:
        text = record.getMessage()
        assert token not in text and key[40:80] not in text


def test_the_wire_fake_serves_every_client_route() -> None:
    used = GoogleChatAdapter.vendor_names
    assert set(used["indus.gateway.adapters.google_chat.client:ChatClient"]) <= set(CHAT_ROUTES)
    assert set(used["indus.gateway.adapters.google_chat.client:PubSubClient"]) == set(PUBSUB_ROUTES)
