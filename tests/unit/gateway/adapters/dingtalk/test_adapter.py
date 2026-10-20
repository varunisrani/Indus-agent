"""The DingTalk adapter over the fake service (P16-16): the row's Verify cases and the rules the
shared contract suite does not reach."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from collections.abc import Awaitable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from indus.foundation.config.schema import PlatformSection
from indus.foundation.scope import Scope, make_scope
from indus.gateway.adapters.base import AdapterConnectError
from indus.gateway.adapters.dingtalk import create_adapter
from indus.gateway.adapters.dingtalk.adapter import (
    DingTalkAdapter,
    DingTalkOptions,
    download_ref,
    normalise_markdown,
    parse_download_ref,
)
from indus.gateway.adapters.dingtalk.stream import (
    BREAKER_WAIT_S,
    Breaker,
    LogCollapse,
    SecretQuery,
    StreamFrame,
    ack,
    socket_url,
)
from indus.gateway.config import AdapterConfig, PlatformSettings
from indus.gateway.core.events import InboundEvent, MessageKind
from indus.gateway.core.send import ErrorKind
from indus.gateway.registry import PlatformEntry, PlatformRegistry
from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import FailureKind
from tests.fakes.platform.dingtalk_wire import DingTalkWire

ANA = "staff-ana"
GROUP = "cidGroupOne=="


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("real network used in a DingTalk adapter test")

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


async def pump(clock: FakeClock, rounds: int = 4) -> None:
    for _ in range(rounds):
        await clock.advance_and_settle(1.0)


def run(coro: Awaitable[Any]) -> Any:
    return asyncio.run(coro)  # pyright: ignore[reportArgumentType]


class Rig:
    def __init__(self, *, options: DingTalkOptions | None = None, **kw: Any) -> None:
        self.clock = _clock()
        self.wire = DingTalkWire(clock=self.clock)
        self.adapter = self.wire.adapter(options=options, **kw)
        self.events: list[InboundEvent] = []
        self.lost: list[tuple[str, str]] = []
        self.adapter.set_sink(self.sink)
        self.adapter.set_fatal_listener(lambda code, message: self.lost.append((code, message)))

    async def sink(self, event: InboundEvent) -> None:
        self.events.append(event)

    async def connect(self) -> None:
        await drive(self.clock, self.adapter.connect())
        await pump(self.clock, 2)

    async def close(self) -> None:
        await drive(self.clock, self.adapter.disconnect())


# -- the Verify column -----------------------------------------------------------------------------


def test_an_expired_session_webhook_falls_back_to_the_robot_api() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        rig.wire.platform.dm(ANA, "hello")
        await pump(rig.clock)
        assert [e.text for e in rig.events] == ["hello"]
        assert (await drive(rig.clock, rig.adapter.send(ANA, "fresh"))).ok
        assert ("oapi.dingtalk.com/robot/sendBySession") in rig.wire.requests[-1][1]
        # 90 minutes of life, dropped 300 s before the stated expiry.
        rig.clock.advance(90 * 60 - 299)
        result = await drive(rig.clock, rig.adapter.send(ANA, "late"))
        assert result.ok
        assert rig.wire.requests[-1][1] == "api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
        body = rig.wire.bodies[-1][1]
        assert body["userIds"] == [ANA] and body["msgKey"] == "sampleMarkdown"
        assert json.loads(body["msgParam"])["text"] == "late"
        assert rig.wire.platform.texts(ANA) == ["fresh", "late"]
        await rig.close()

    run(main())


def test_a_refusing_webhook_is_dropped_and_the_api_takes_over() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        rig.wire.platform.group(GROUP, ANA, "hi bot", mention=True)
        await pump(rig.clock)
        rig.wire.sessions.clear()  # DingTalk revoked the session
        result = await drive(rig.clock, rig.adapter.send(GROUP, "answer"))
        assert result.ok and result.message_id == rig.wire.platform.messages(GROUP)[0].message_id
        assert rig.wire.bodies[-1][0] == "/v1.0/robot/groupMessages/send"
        assert rig.wire.bodies[-1][1]["openConversationId"] == GROUP
        hooks_before = sum(1 for _, url in rig.wire.requests if "sendBySession" in url)
        assert (await drive(rig.clock, rig.adapter.send(GROUP, "again"))).ok
        hooks_after = sum(1 for _, url in rig.wire.requests if "sendBySession" in url)
        assert hooks_after == hooks_before  # the dead webhook is not tried again
        await rig.close()

    run(main())


def test_the_breaker_trips_on_the_fifth_identical_failure() -> None:
    async def main() -> None:
        rig = Rig()
        rig.wire.socket_failures = [ConnectionResetError("refused")] * 7
        await rig.connect()
        for _ in range(2000):
            await rig.clock.advance_and_settle(5.0)
            if rig.wire.sockets:
                break
        times = rig.wire.socket_attempts
        gaps = [round(b - a) for a, b in zip(times, times[1:], strict=False)]
        # 2, 5, 10, 30 for the first four; the fifth identical failure trips 300 s waits.
        assert gaps[:4] == [2, 5, 10, 30]
        assert all(abs(g - BREAKER_WAIT_S) <= 5 for g in gaps[4:]) and len(gaps) == 7
        assert rig.adapter.stream_opens == 1 and not rig.adapter.breaker.tripped
        await rig.close()

    run(main())


def test_breaker_counts_only_identical_failures_in_a_row() -> None:
    breaker = Breaker()
    assert [breaker.failed("a") for _ in range(4)] == [2.0, 5.0, 10.0, 30.0]
    assert not breaker.tripped
    assert breaker.failed("a") == BREAKER_WAIT_S and breaker.tripped
    assert breaker.failed("b") == 60.0 and not breaker.tripped  # a new failure resets the count
    breaker.succeeded()
    assert breaker.failed("b") == 2.0


def test_repeated_log_lines_are_collapsed_within_300_seconds(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def main() -> None:
        rig = Rig()
        rig.wire.socket_failures = [ConnectionResetError("same")] * 12
        await rig.connect()
        for _ in range(4000):
            await rig.clock.advance_and_settle(10.0)
            if rig.wire.sockets:
                break
        await rig.close()

    with caplog.at_level(logging.WARNING, logger="indus.gateway.adapters.dingtalk.adapter"):
        run(main())
    lines = [r.getMessage() for r in caplog.records if "dingtalk" in r.getMessage()]
    # 12 identical failures over ~40 minutes: the first warning (its repeats within 300 s held
    # back) and the breaker's one line, then silence until the failure changes.
    assert len(lines) == 2
    assert "reopening in 2 s" in lines[0] and "failed 5 times the same way" in lines[1]


def test_log_collapse_counts_what_it_held_back() -> None:
    clock = _clock()
    collapse = LogCollapse(clock, window_s=300.0)
    assert collapse.note("x") == 0
    assert collapse.note("x") is None and collapse.note("x") is None
    assert collapse.note("y") == 0
    clock.advance(301)
    assert collapse.note("x") == 2


# -- the stream protocol ---------------------------------------------------------------------------


def test_pings_are_acked_with_their_data_and_callbacks_at_once() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        rig.wire.system("ping", {"opaque": "o-123"})
        rig.wire.platform.dm(ANA, "hello")
        await pump(rig.clock)
        assert len(rig.wire.acks) == 2
        ping = next(a for a in rig.wire.acks if a["headers"]["messageId"].startswith("sys"))
        callback = next(a for a in rig.wire.acks if a is not ping)
        assert ping["code"] == 200 and json.loads(ping["data"]) == {"opaque": "o-123"}
        assert callback["code"] == 200 and callback["headers"]["contentType"] == "application/json"
        await rig.close()

    run(main())


def test_a_disconnect_frame_opens_a_fresh_connection() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        rig.wire.system("disconnect")
        await pump(rig.clock)
        assert rig.adapter.stream_opens == 2 and len(rig.wire.tickets) == 2
        assert rig.wire.acks[0]["code"] == 200
        await rig.close()

    run(main())


def test_unknown_topics_are_acked_as_not_implemented() -> None:
    frame = StreamFrame.parse(
        json.dumps({"type": "EVENT", "headers": {"topic": "x", "messageId": "m1"}, "data": "{}"})
    )
    assert frame is not None
    assert json.loads(ack(frame, code=404, message="not implemented"))["code"] == 404
    assert StreamFrame.parse("not json") is None and StreamFrame.parse("[1]") is None


def test_a_refused_stream_is_fatal() -> None:
    async def main() -> None:
        rig = Rig()
        rig.wire.open_refusals = [403]
        await rig.connect()
        await pump(rig.clock, 6)
        assert [code for code, _ in rig.lost] == ["dingtalk_auth"]
        assert not rig.adapter.streaming and rig.wire.sockets == []
        await rig.close()

    run(main())


def test_a_missing_httpx_ws_is_fatal_with_the_install_hint() -> None:
    async def main() -> None:
        rig = Rig()
        rig.wire.socket_failures = [ModuleNotFoundError("no httpx_ws", name="httpx_ws")]
        await rig.connect()
        await pump(rig.clock)
        assert [code for code, _ in rig.lost] == ["missing_dependency"]
        assert "indus-agent[dingtalk]" in rig.lost[0][1]
        await rig.close()

    run(main())


def test_endpoints_off_dingtalk_are_refused() -> None:
    assert socket_url("wss://wss-open-connection.dingtalk.com:443/connect") == (
        "https://wss-open-connection.dingtalk.com:443/connect"
    )
    for bad in ("ws://x.dingtalk.com/c", "wss://evil.example/c", "wss://dingtalk.com.evil.io/c"):
        with pytest.raises(ValueError):
            socket_url(bad)


# -- inbound ---------------------------------------------------------------------------------------


def test_redelivered_messages_and_empty_ones_are_dropped() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        payload = {
            "msgId": "m-1",
            "conversationId": ANA,
            "conversationType": "1",
            "senderStaffId": ANA,
            "msgtype": "text",
            "text": {"content": "once"},
        }
        rig.wire.callback(payload)
        rig.wire.callback(payload)
        rig.wire.callback({**payload, "msgId": "m-2", "text": {"content": "  "}})
        await pump(rig.clock)
        assert [e.text for e in rig.events] == ["once"]
        assert len(rig.wire.acks) == 3  # every frame is acknowledged, even dropped ones
        await rig.close()

    run(main())


def test_group_gating_by_mention_and_chat() -> None:
    async def main() -> None:
        options = DingTalkOptions(
            require_mention=True,
            allowed_chats=frozenset({GROUP, "cidFree"}),
            free_response_chats=frozenset({"cidFree"}),
        )
        rig = Rig(options=options)
        await rig.connect()
        p = rig.wire.platform
        p.group(GROUP, ANA, "no mention")
        p.group(GROUP, ANA, "with mention", mention=True)
        p.group("cidFree", ANA, "free chat")
        p.group("cidOther", ANA, "elsewhere", mention=True)
        p.dm(ANA, "a DM")
        await pump(rig.clock)
        assert sorted(e.text for e in rig.events) == ["a DM", "free chat", "with mention"]
        mentioned = next(e for e in rig.events if e.text == "with mention")
        assert mentioned.metadata.get("mentioned") is True
        assert mentioned.source.chat_type == "group" and mentioned.user_id == ANA
        assert mentioned.source.user_id_alt == f"$:LWCP_v1:${ANA}"
        await rig.close()

    run(main())


def test_message_shapes_become_events_with_attachments() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        p = rig.wire.platform
        p.voice(ANA, ANA)
        p.photo(ANA, ANA, caption="look")
        p.photo(ANA, ANA)
        p.document(ANA, ANA, ref="report.pdf")
        await pump(rig.clock)
        kinds = [(e.kind, e.text, e.media[0].mime) for e in rig.events]
        assert kinds == [
            (MessageKind.VOICE, "", "audio/amr"),
            (MessageKind.PHOTO, "look", "image/jpeg"),
            (MessageKind.PHOTO, "", "image/jpeg"),
            (MessageKind.DOCUMENT, "", "application/pdf"),
        ]
        ref = rig.events[3].media[0].ref
        assert ref.startswith("dingtalk://download/") and parse_download_ref(ref)
        chunks = [c async for c in rig.adapter.fetch_media(rig.events[3].media[0])]
        assert b"".join(chunks) == b"bytes of report.pdf"
        assert rig.wire.bodies[-1][1]["robotCode"] == rig.wire.client_id
        await rig.close()

    run(main())


def test_download_refs_round_trip() -> None:
    ref = download_ref("code/with slash", "a b.txt")
    assert parse_download_ref(ref) == "code/with slash"
    assert parse_download_ref("https://x") is None


def test_webhooks_on_other_hosts_are_ignored() -> None:
    adapter = DingTalkWire(clock=_clock()).adapter()
    adapter.normalise(
        {
            "msgId": "m9",
            "conversationId": ANA,
            "senderStaffId": ANA,
            "msgtype": "text",
            "text": {"content": "x"},
            "sessionWebhook": "https://evil.example/robot/sendBySession?session=s",
        }
    )
    assert adapter._webhooks == {}  # pyright: ignore[reportPrivateUsage]


# -- outbound --------------------------------------------------------------------------------------


def test_long_replies_are_split_and_markdown_is_fixed_for_the_renderer() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        text = ("word " * 9000).strip()
        result = await drive(rig.clock, rig.adapter.send(GROUP, text))
        assert result.ok and len(result.ids) == 3
        assert all(len(t) <= 20_000 for t in rig.wire.platform.texts(GROUP))
        await rig.close()

    run(main())
    assert normalise_markdown("Steps:\n1. one\n2. two\n   ```py\nx\n   ```") == (
        "Steps:\n\n1. one\n2. two\n```py\nx\n```"
    )


def test_api_errors_are_classified() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        rig.wire.platform.fail(FailureKind.RATE_LIMIT, op="send", retry_after=9)
        limited = await drive(rig.clock, rig.adapter.send(GROUP, "x"))
        assert limited.kind is ErrorKind.RATE_LIMITED and limited.retry_after == 9
        rig.wire.platform.fail(FailureKind.FORBIDDEN, op="send")
        refused = await drive(rig.clock, rig.adapter.send(GROUP, "x"))
        assert refused.kind is ErrorKind.FORBIDDEN and refused.chat_unreachable
        await rig.close()

    run(main())


def test_an_expired_access_token_is_fetched_again_once() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        rig.wire.token = "rotated"  # the old token is refused from now on
        result = await drive(rig.clock, rig.adapter.send(GROUP, "after rotation"))
        assert result.ok
        token_calls = [p for _, p in rig.wire.requests if p.endswith("/oauth2/accessToken")]
        assert len(token_calls) == 2
        await rig.close()

    run(main())


# -- connecting ------------------------------------------------------------------------------------


def test_wrong_credentials_are_a_fatal_connect_error() -> None:
    async def main() -> None:
        clock = _clock()
        wire = DingTalkWire(clock=clock)
        adapter = DingTalkAdapter(
            clock=clock,
            client_id=wire.client_id,
            client_secret="wrong",
            transport=wire.transport(),
            opener=wire.open,
        )
        with pytest.raises(AdapterConnectError) as raised:
            await drive(clock, adapter.connect())
        assert not raised.value.retryable and raised.value.code == "auth_failed"
        missing = DingTalkAdapter(clock=clock, client_id="", client_secret="")
        with pytest.raises(AdapterConnectError, match="DINGTALK_CLIENT_ID and DINGTALK_CLIENT"):
            await drive(clock, missing.connect())

    run(main())


def test_a_send_only_adapter_opens_no_stream() -> None:
    async def main() -> None:
        rig = Rig(send_only=True)
        await rig.connect()
        assert (await drive(rig.clock, rig.adapter.send(GROUP, "deploy done"))).ok
        assert rig.wire.tickets == [] and not rig.adapter.streaming
        assert [op.kind for op in rig.wire.platform.ops] == ["connect", "send"]
        await rig.close()

    run(main())


def test_indus_send_builds_a_send_only_adapter(scope: Scope) -> None:
    from indus.gateway.delivery import send_command

    clock = _clock()
    wire = DingTalkWire(clock=clock)
    secrets: Any = scope.secrets
    secrets.set("DINGTALK_CLIENT_ID", wire.client_id)
    secrets.set("DINGTALK_CLIENT_SECRET", wire.secret)
    built: list[DingTalkAdapter] = []

    def factory(config: AdapterConfig) -> DingTalkAdapter:
        adapter = create_adapter(config, transport=wire.transport(), opener=wire.open)
        built.append(adapter)
        return adapter

    entry = PlatformEntry("dingtalk", "DingTalk", factory=factory, check_fn=lambda: True)
    settings = PlatformSettings("dingtalk", True, "credentials", PlatformSection())
    adapter = send_command.build_adapter(scope, "dingtalk", settings, PlatformRegistry([entry]))
    assert adapter is built[0] and built[0].send_only

    async def main() -> None:
        assert isinstance(adapter, DingTalkAdapter)
        await drive(clock, adapter.connect())
        assert (await drive(clock, adapter.send("user:" + ANA, "deploy done"))).ok
        await drive(clock, adapter.disconnect())

    run(main())
    assert wire.platform.texts(ANA) == ["deploy done"] and wire.tickets == []


def test_create_adapter_reads_secrets_and_options(tmp_path: Path) -> None:
    env = {
        "DINGTALK_CLIENT_ID": "app-from-env",
        "DINGTALK_CLIENT_SECRET": "s" * 32,
        "INDUS_DINGTALK_ALLOWED_CHATS": "cidA",
    }
    scope = make_scope(tmp_path / "home", env=env, clock=_clock())
    section = PlatformSection.model_validate(
        {"extra": {"robot_code": "robot-9", "require_mention": "true", "allowed_chats": ["cidB"]}}
    )
    settings = PlatformSettings("dingtalk", True, "credentials", section)
    adapter = create_adapter(AdapterConfig("dingtalk", settings, scope, scope.clock))
    assert adapter.robot_code == "robot-9" and adapter.options.require_mention
    assert adapter.options.allowed_chats == {"cidA", "cidB"}
    assert "s" * 32 not in adapter.scrub("secret " + "s" * 32)
    assert repr(adapter) == "<DingTalkAdapter disconnected>"


# -- secrets ---------------------------------------------------------------------------------------


def test_secrets_never_reach_logs_errors_or_the_logged_url(
    caplog: pytest.LogCaptureFixture,
) -> None:
    holder: dict[str, str] = {}

    async def main() -> None:
        rig = Rig()
        await rig.connect()
        rig.wire.platform.dm(ANA, "hello")
        await pump(rig.clock)
        assert (await drive(rig.clock, rig.adapter.send(ANA, "via webhook"))).ok
        session = next(iter(rig.wire.sessions))
        ticket = rig.wire.tickets[0]
        holder.update(session=session, ticket=ticket, token=rig.wire.token)
        holder["secret"] = rig.wire.secret
        # The far side got them: the transport added the query.
        assert any(f"session={session}" in url for url in rig.wire.urls)
        assert any(f"ticket={ticket}" in url for url in rig.wire.urls)
        rig.wire.platform.fail(FailureKind.FORBIDDEN, op="send")
        rig.wire.sessions.clear()
        failed = await drive(rig.clock, rig.adapter.send(ANA, f"leak {rig.wire.token}"))
        assert not failed.ok and rig.wire.token not in (failed.error or "")
        await rig.close()

    for name in ("httpx", "httpcore"):
        caplog.set_level(logging.DEBUG, logger=name)
    with caplog.at_level(logging.DEBUG):
        run(main())
    assert "HTTP Request" in caplog.text and "sendBySession" in caplog.text
    for value in holder.values():
        assert value not in caplog.text


def test_the_secret_query_transport_adds_parameters_inside() -> None:
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200)

    async def main() -> None:
        query = SecretQuery(httpx.MockTransport(handler))
        async with httpx.AsyncClient(transport=query) as client:
            response = await client.post(
                "https://oapi.dingtalk.com/robot/sendBySession",
                extensions={"indus.secret_query": {"session": "abc"}},
            )
            assert "session" not in str(response.request.url)
            query.expect("https://h.dingtalk.com/connect", {"ticket": "t1"})
            await client.get("https://h.dingtalk.com/connect")
            await client.get("https://h.dingtalk.com/connect")

    run(main())
    assert seen == [
        "https://oapi.dingtalk.com/robot/sendBySession?session=abc",
        "https://h.dingtalk.com/connect?ticket=t1",
        "https://h.dingtalk.com/connect",
    ]
