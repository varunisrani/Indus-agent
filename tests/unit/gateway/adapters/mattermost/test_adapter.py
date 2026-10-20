"""The Mattermost adapter over the fake server (P16-08): the row's Verify cases and the rules the
shared contract suite does not reach."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from collections.abc import Awaitable, Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from indus.foundation.config.schema import PlatformSection
from indus.foundation.scope import Scope
from indus.gateway.adapters.mattermost import create_adapter
from indus.gateway.adapters.mattermost.adapter import (
    MattermostAdapter,
    MattermostOptions,
    file_ref,
    parse_file_ref,
)
from indus.gateway.adapters.mattermost.client import ROUTES as CLIENT_ROUTES
from indus.gateway.adapters.mattermost.client import MediaTooLarge
from indus.gateway.config import AdapterConfig, PlatformSettings
from indus.gateway.core.events import Attachment, InboundEvent, MessageKind
from indus.gateway.core.send import ErrorKind
from indus.gateway.core.session_keys import build_session_key
from indus.gateway.registry import PlatformRegistry
from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import FailureKind
from tests.fakes.platform.mattermost_wire import (
    BASE_URL,
    BOT_ID,
    ROUTES,
    FakeUpgradeError,
    MattermostWire,
)

CHANNEL = "chan0000000000000000000001"
ANA = "user0000000000000000000ana"
BEN = "user0000000000000000000ben"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("real network used in a Mattermost adapter test")

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
    def __init__(self, *, options: MattermostOptions | None = None, listen: bool = True) -> None:
        self.clock = _clock()
        self.wire = MattermostWire(clock=self.clock)
        self.adapter = self.wire.adapter(options=options)
        self.events: list[InboundEvent] = []
        if listen:
            self.adapter.set_sink(self.sink)

    async def sink(self, event: InboundEvent) -> None:
        self.events.append(event)

    async def connect(self) -> None:
        await drive(self.clock, self.adapter.connect())
        await pump(self.clock, 1)

    async def close(self) -> None:
        await drive(self.clock, self.adapter.disconnect())


def _post(post_id: str, **fields: Any) -> dict[str, Any]:
    return {"id": post_id, "channel_id": CHANNEL, "user_id": ANA, "message": "", **fields}


# -- the row's Verify ------------------------------------------------------------------------------


def test_thread_replies_are_keyed_by_the_root_id() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        p = rig.wire.platform
        root = p.group(CHANNEL, ANA, "start here", mention=True)
        p.thread(CHANNEL, root.message_id, ANA, "first reply")
        p.thread(CHANNEL, root.message_id, BEN, "second reply")
        await pump(rig.clock)
        top, first, second = rig.events
        assert top.source.chat_type == "channel" and top.source.thread_id is None
        for event in (first, second):
            assert event.source.chat_type == "thread"
            assert event.source.thread_id == root.message_id
            assert event.source.parent_chat_id == CHANNEL
        assert build_session_key(first.source) == build_session_key(second.source)
        # The answer goes into the same thread.
        sent = await drive(
            rig.clock, rig.adapter.send(CHANNEL, "done", meta={"thread_id": root.message_id})
        )
        assert sent.ok
        assert rig.wire.bodies[-1][1]["root_id"] == root.message_id
        await rig.close()

    run(main())


def test_channel_not_in_the_allowlist_is_ignored() -> None:
    async def main() -> None:
        rig = Rig(options=MattermostOptions(allowed_channels=frozenset({CHANNEL})))
        await rig.connect()
        p = rig.wire.platform
        p.group(CHANNEL, ANA, "allowed", mention=True)
        p.group("other00000000000000000000", ANA, "elsewhere", mention=True)
        p.dm(ANA, "direct messages are exempt")
        await pump(rig.clock)
        assert sorted(e.text for e in rig.events) == ["allowed", "direct messages are exempt"]
        await rig.close()

    run(main())


def test_allowed_channels_come_from_config_and_env() -> None:
    options = MattermostOptions.read(
        {"allowed_channels": ["a", "b"], "reply_mode": "THREAD"},
        {"INDUS_MATTERMOST_ALLOWED_CHANNELS": "c, d"},
    )
    assert options.allowed_channels == {"a", "b", "c", "d"} and options.reply_mode == "thread"
    assert MattermostOptions.read({"reply_mode": "nested"}, {}).reply_mode == "off"


# -- inbound ---------------------------------------------------------------------------------------


def test_mentions_bots_system_posts_and_repeats() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        w = rig.wire
        w.push_frame(w.posted(_post("p1", message="@IndusBot: deploy please")))
        w.push_frame(w.posted(_post("p2", message="hello all"), mentions=json.dumps([BOT_ID])))
        w.push_frame(w.posted(_post("p3", message="no mention here")))
        w.push_frame(w.posted(_post("p8", message=f"@{BOT_ID}, by id")))
        w.push_frame(w.posted(_post("p4", message="beep", props={"from_bot": "true"})))
        w.push_frame(w.posted(_post("p5", message="joined", type="system_join_channel")))
        w.push_frame(w.posted(_post("p6", user_id=BOT_ID, message="our own echo")))
        w.push_frame(w.posted(_post("p2", message="hello all")))  # redelivered
        w.push_frame(w.posted(_post("p7", message="  /help"), channel_type="D"))
        w.push_frame("not json")
        w.push_frame({"event": "typing", "data": {"parent_id": ""}})
        await pump(rig.clock)
        seen = [(e.text, bool(e.metadata.get("mentioned")), e.source.is_bot) for e in rig.events]
        assert seen == [
            ("deploy please", True, False),
            ("hello all", True, False),
            ("no mention here", False, False),
            ("by id", True, False),
            ("beep", False, True),
            ("/help", False, False),
        ]
        assert rig.events[-1].command == "help" and rig.events[-1].source.chat_type == "dm"
        await rig.close()

    run(main())


def test_channel_types_and_thread_mode() -> None:
    async def main() -> None:
        rig = Rig(options=MattermostOptions(reply_mode="thread"))
        await rig.connect()
        w = rig.wire
        w.push_frame(w.posted(_post("g1", message="private"), channel_type="P"))
        w.push_frame(w.posted(_post("g2", message="group dm"), channel_type="G"))
        w.push_frame(w.posted(_post("d1", message="direct"), channel_type="D"))
        await pump(rig.clock)
        private, group, direct = rig.events
        # In thread mode every top-level post outside DMs opens its own thread.
        assert (private.source.chat_type, private.source.thread_id) == ("thread", "g1")
        assert (group.source.chat_type, group.source.thread_id) == ("thread", "g2")
        assert (direct.source.chat_type, direct.source.thread_id) == ("dm", None)
        await rig.close()

    run(main())


def test_reply_mode_thread_threads_replies_under_the_true_root() -> None:
    async def main() -> None:
        rig = Rig(listen=False, options=MattermostOptions(reply_mode="thread"))
        await rig.connect()
        p = rig.wire.platform
        root = await p.post(CHANNEL, "root")
        reply = await p.post(CHANNEL, "a reply", thread_id=root.message_id)
        result = await drive(
            rig.clock, rig.adapter.send(CHANNEL, "answer", reply_to=reply.message_id)
        )
        assert result.ok and rig.wire.bodies[-1][1]["root_id"] == root.message_id
        flat = Rig(listen=False)
        await flat.connect()
        top = await flat.wire.platform.post(CHANNEL, "root")
        await drive(flat.clock, flat.adapter.send(CHANNEL, "answer", reply_to=top.message_id))
        assert "root_id" not in flat.wire.bodies[-1][1]  # off: answers stay flat
        await rig.close()
        await flat.close()

    run(main())


def test_media_posts_and_the_fetcher() -> None:
    async def main() -> None:
        rig = Rig(options=MattermostOptions(max_media_bytes=64))
        await rig.connect()
        p = rig.wire.platform
        p.photo(CHANNEL, ANA, "cat.png", caption="look", chat_kind="group")
        p.voice(ANA, ANA, "memo.ogg")
        await pump(rig.clock)
        photo, voice = rig.events
        assert photo.kind is MessageKind.PHOTO and photo.text == "look"
        assert voice.kind is MessageKind.VOICE
        assert photo.media[0].ref.endswith("/cat.png") and photo.media[0].mime == "image/jpeg"
        data = b"".join([c async for c in rig.adapter.fetch_media(photo.media[0])])
        assert data == b"bytes of cat.png"
        rig.wire.files["big"] = ("big.bin", b"x" * 65, "application/octet-stream")
        with pytest.raises(MediaTooLarge):
            _ = [c async for c in rig.adapter.fetch_media(Attachment(ref=file_ref("big", "b")))]
        with pytest.raises(ValueError):
            _ = [c async for c in rig.adapter.fetch_media(Attachment(ref="https://x/y"))]
        await rig.close()

    run(main())


def test_file_refs_round_trip() -> None:
    ref = file_ref("abc123", "my report.pdf")
    assert ref == "mattermost://file/abc123/my%20report.pdf"
    assert parse_file_ref(ref) == "abc123" and parse_file_ref("mxc://x/y") is None


# -- outbound --------------------------------------------------------------------------------------


def test_outbound_text_rules() -> None:
    async def main() -> None:
        rig = Rig(listen=False)
        await rig.connect()
        text = "Hey @channel, see ![chart](https://img.example/c.png) and `@all` @hereby"
        result = await drive(rig.clock, rig.adapter.send(CHANNEL, text))
        assert result.ok
        sent = rig.wire.bodies[-1][1]["message"]
        assert sent == "Hey @​channel, see https://img.example/c.png and `@all` @hereby"
        await rig.close()

    run(main())


def test_long_text_is_split_at_4000_characters() -> None:
    async def main() -> None:
        rig = Rig(listen=False)
        await rig.connect()
        text = " ".join(f"word{n}" for n in range(2000))
        result = await drive(rig.clock, rig.adapter.send(CHANNEL, text))
        assert result.ok and len(result.ids) >= 3
        assert all(len(m.text) <= 4000 for m in rig.wire.platform.messages(CHANNEL))
        await rig.close()

    run(main())


def test_edit_delete_typing_and_reactions() -> None:
    async def main() -> None:
        rig = Rig(listen=False)
        await rig.connect()
        sent = await drive(rig.clock, rig.adapter.send(CHANNEL, "draft"))
        assert sent.message_id
        edited = await drive(rig.clock, rig.adapter.edit(CHANNEL, sent.message_id, "final"))
        assert edited.ok and rig.wire.platform.message(sent.message_id).text == "final"  # pyright: ignore[reportOptionalMemberAccess]
        missing = await drive(rig.clock, rig.adapter.edit(CHANNEL, "nope", "x", final=True))
        assert missing.kind is ErrorKind.NOT_FOUND and not missing.chat_unreachable
        await drive(rig.clock, rig.adapter.typing_start(CHANNEL, {"thread_id": sent.message_id}))
        assert rig.wire.typing_calls[-1] == {"channel_id": CHANNEL, "parent_id": sent.message_id}
        await drive(rig.clock, rig.adapter.processing_started(CHANNEL, sent.message_id))
        await drive(rig.clock, rig.adapter.processing_finished(CHANNEL, sent.message_id, ok=True))
        assert rig.wire.reactions == [
            ("add", sent.message_id, "eyes"),
            ("remove", sent.message_id, "eyes"),
            ("add", sent.message_id, "white_check_mark"),
        ]
        assert await drive(rig.clock, rig.adapter.delete(CHANNEL, sent.message_id))
        assert rig.wire.platform.message(sent.message_id).deleted  # pyright: ignore[reportOptionalMemberAccess]
        await rig.close()

    run(main())


def test_media_upload_then_post(tmp_path: Path) -> None:
    async def main() -> None:
        rig = Rig(listen=False)
        await rig.connect()
        path = tmp_path / "chart.png"
        path.write_bytes(b"\x89PNG fake")
        result = await drive(rig.clock, rig.adapter.send_image(CHANNEL, str(path), caption="Q3"))
        assert result.ok
        body = rig.wire.bodies[-1][1]
        assert body["message"] == "Q3" and len(body["file_ids"]) == 1
        name, data, mime = rig.wire.files[body["file_ids"][0]]
        assert (name, data, mime) == ("chart.png", b"\x89PNG fake", "image/png")
        message = rig.wire.platform.message(result.message_id or "")
        assert message is not None and message.media[0].kind == "photo"
        await rig.close()

    run(main())


def test_send_error_readings() -> None:
    async def main() -> None:
        rig = Rig(listen=False)
        await rig.connect()
        p = rig.wire.platform
        p.fail(FailureKind.RATE_LIMIT, op="send", retry_after=9)
        limited = await drive(rig.clock, rig.adapter.send(CHANNEL, "x"))
        assert limited.kind is ErrorKind.RATE_LIMITED and limited.retry_after == 9
        bad_root = await drive(
            rig.clock, rig.adapter.send(CHANNEL, "x", meta={"thread_id": "gone"})
        )
        assert bad_root.kind is ErrorKind.NOT_FOUND and not bad_root.chat_unreachable
        p.remove_chat(CHANNEL)
        gone = await drive(rig.clock, rig.adapter.send(CHANNEL, "x"))
        assert gone.kind is ErrorKind.NOT_FOUND and gone.chat_unreachable
        rig.wire.token = "rotated"
        refused = await drive(rig.clock, rig.adapter.send("other", "x"))
        assert not refused.chat_unreachable and rig.adapter.is_final_failure(refused)
        await rig.close()

    run(main())


def test_channel_info_and_directory() -> None:
    async def main() -> None:
        rig = Rig(listen=False)
        rig.wire.platform.add_chat(CHANNEL, "group", "Town Square")
        rig.wire.platform.add_chat(ANA, "dm", "ana")
        await rig.connect()
        info = await drive(rig.clock, rig.adapter.chat_info(CHANNEL))
        assert (info.name, info.type) == ("Town Square", "group")
        listed = await drive(rig.clock, rig.adapter.list_channels())
        assert listed is not None
        assert {(c.id, c.name, c.type, c.guild) for c in listed} == {
            (CHANNEL, "Town Square", "group", "Acme"),
            (ANA, "ana", "dm", "Acme"),
        }
        await rig.close()

    run(main())


# -- the event socket ------------------------------------------------------------------------------


def test_send_only_open_never_opens_the_socket() -> None:
    async def main() -> None:
        rig = Rig(listen=False)
        await rig.connect()
        assert (await drive(rig.clock, rig.adapter.send(CHANNEL, "deploy done"))).ok
        assert rig.wire.socket_urls == [] and not rig.adapter.listening
        rig.adapter.set_sink(rig.sink)
        await pump(rig.clock, 1)
        assert rig.wire.socket_urls == [f"{BASE_URL}/api/v4/websocket"]
        rig.adapter.set_sink(None)
        await pump(rig.clock, 1)
        assert not rig.adapter.listening and rig.wire.sockets[0].closed
        await rig.close()

    run(main())


def test_a_send_only_adapter_never_opens_the_socket_even_with_a_sink() -> None:
    async def main() -> None:
        clock = _clock()
        wire = MattermostWire(clock=clock)
        adapter = wire.adapter(send_only=True)
        events: list[InboundEvent] = []

        async def sink(event: InboundEvent) -> None:
            events.append(event)

        adapter.set_sink(sink)
        await drive(clock, adapter.connect())
        adapter.set_sink(sink)
        await pump(clock, 3)
        assert (await drive(clock, adapter.send(CHANNEL, "deploy done"))).ok
        assert wire.socket_urls == [] and not adapter.listening and events == []
        await drive(clock, adapter.disconnect())

    run(main())


def test_indus_send_over_the_wire_does_no_inbound_work(
    capsys: pytest.CaptureFixture[str], scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    from indus.gateway.delivery import send_command
    from tests.unit.cli.commands.cmdrun import run_cli, write_settings

    write_settings(scope, "platforms:\n  mattermost:\n    enabled: true\n")
    wire = MattermostWire(clock=scope.clock)  # pyright: ignore[reportArgumentType]
    secrets: Any = scope.secrets
    secrets.set("MATTERMOST_TOKEN", wire.token)
    secrets.set("MATTERMOST_URL", BASE_URL)
    built: list[MattermostAdapter] = []
    real_build = send_command.build_adapter

    def factory(config: AdapterConfig) -> MattermostAdapter:
        adapter = create_adapter(config, transport=wire.transport(), opener=wire.open)
        built.append(adapter)
        return adapter

    def build(build_scope: Scope, name: str, settings: PlatformSettings, *_: Any) -> Any:
        # The real builder (``AdapterConfig(send_only=True)``) over the real registry row; the
        # probe is relaxed because sending needs no httpx-ws (not installed in the gate).
        row = PlatformRegistry().get("mattermost")
        assert row is not None
        wired = replace(row, factory=factory, check_fn=lambda: True)
        return real_build(build_scope, name, settings, PlatformRegistry([wired]))

    monkeypatch.setattr(send_command, "ADAPTER_BUILDER", build)
    ran = run_cli(capsys, scope, "send", f"mattermost:{CHANNEL}", "deploy done")
    assert ran.code == 0, ran.err
    assert wire.platform.texts(CHANNEL) == ["deploy done"]
    assert wire.socket_urls == []
    assert [op.kind for op in wire.platform.ops] == ["connect", "send", "disconnect"]
    assert built[0].send_only and not built[0].listening


def test_the_socket_reopens_with_backoff_capped_at_sixty_seconds() -> None:
    async def main() -> None:
        rig = Rig(listen=False)
        await rig.connect()
        rig.wire.socket_failures = [ConnectionResetError("refused")] * 7
        rig.adapter.set_sink(rig.sink)
        for _ in range(300):
            await rig.clock.advance_and_settle(1.0)
            if len(rig.wire.sockets) == 1:
                break
        times = rig.wire.socket_attempts
        gaps = [round(b - a) for a, b in zip(times, times[1:], strict=False)]
        assert gaps == [2, 4, 8, 16, 32, 60, 60]
        # Traffic flows on the socket that finally opened; a drop opens a new one after 2 s.
        rig.wire.platform.dm(ANA, "back again")
        await pump(rig.clock)
        assert [e.text for e in rig.events] == ["back again"]
        rig.wire.drop_socket()
        await pump(rig.clock, 4)
        assert len(rig.wire.sockets) == 2
        await rig.close()

    run(main())


def test_a_refused_upgrade_is_fatal() -> None:
    async def main() -> None:
        rig = Rig(listen=False)
        await rig.connect()
        lost: list[tuple[str, str]] = []
        rig.adapter.set_fatal_listener(lambda code, message: lost.append((code, message)))
        response = httpx.Response(401, request=httpx.Request("GET", BASE_URL))
        rig.wire.socket_failures = [ExceptionGroup("upgrade", [FakeUpgradeError(response)])]
        rig.adapter.set_sink(rig.sink)
        await pump(rig.clock, 10)
        assert [code for code, _ in lost] == ["mattermost_auth"]
        assert "refused the event socket (HTTP 401)" in lost[0][1]
        assert len(rig.wire.socket_urls) == 1 and not rig.adapter.listening
        await rig.close()

    run(main())


def test_the_real_opener_speaks_to_an_asgi_server() -> None:
    """The production path (httpx-ws) against an in-process ASGI app: no socket involved."""
    pytest.importorskip("httpx_ws")
    from httpx_ws.transport import (  # pyright: ignore[reportMissingImports]
        ASGIWebSocketTransport,  # pyright: ignore[reportUnknownVariableType]
    )
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route, WebSocketRoute
    from starlette.websockets import WebSocket

    token = MattermostWire().token
    posted = {
        "event": "posted",
        "data": {
            "channel_type": "D",
            "sender_name": "@ana",
            "post": json.dumps(_post("w1", message="over a real socket")),
        },
    }

    async def me(request: Any) -> JSONResponse:
        return JSONResponse({"id": BOT_ID, "username": "indusbot"})

    async def events(websocket: WebSocket) -> None:
        if websocket.headers.get("authorization") != f"Bearer {token}":
            await websocket.close(code=1008)
            return
        await websocket.accept()
        await websocket.send_text(json.dumps({"event": "hello", "data": {}}))
        await websocket.send_text(json.dumps(posted))
        await websocket.receive_text()  # stay open until the client goes

    app = Starlette(
        routes=[Route("/api/v4/users/me", me), WebSocketRoute("/api/v4/websocket", events)]
    )

    async def main() -> list[InboundEvent]:
        got: list[InboundEvent] = []
        arrived = asyncio.Event()

        async def sink(event: InboundEvent) -> None:
            got.append(event)
            arrived.set()

        async with ASGIWebSocketTransport(app) as transport:  # pyright: ignore[reportUnknownVariableType]
            adapter = MattermostAdapter(
                clock=_clock(),
                base_url="http://mm.test",
                token=token,
                transport=transport,  # pyright: ignore[reportUnknownArgumentType]
            )
            adapter.set_sink(sink)
            await adapter.connect()
            await asyncio.wait_for(arrived.wait(), 10)
            await adapter.disconnect()
        return got

    (event,) = run(main())
    assert event.text == "over a real socket" and event.source.chat_type == "dm"


# -- configuration, secrets, routes ----------------------------------------------------------------


def test_connect_failures_are_classified() -> None:
    async def main() -> None:
        clock = _clock()
        wire = MattermostWire(clock=clock)
        bad = MattermostAdapter(
            clock=clock, base_url=BASE_URL, token="wrong", transport=wire.transport()
        )
        with pytest.raises(Exception) as refused:
            await drive(clock, bad.connect())
        assert getattr(refused.value, "code", "") == "auth_failed"
        assert getattr(refused.value, "retryable", True) is False
        unset = MattermostAdapter(clock=clock, base_url="", token="")
        with pytest.raises(Exception) as missing:
            await drive(clock, unset.connect())
        assert "MATTERMOST_URL and MATTERMOST_TOKEN" in str(missing.value)

    run(main())


def test_create_adapter_reads_settings_and_secrets(scope: Scope) -> None:
    secrets: Any = scope.secrets
    secrets.set("MATTERMOST_TOKEN", "k" * 26)
    secrets.set("MATTERMOST_URL", "https://from-secret.example")
    section = PlatformSection.model_validate(
        {"url": "https://mm.example.org/", "reply_mode": "thread"}
    )
    settings = PlatformSettings("mattermost", True, "enabled in config", section)
    adapter = create_adapter(AdapterConfig("mattermost", settings, scope, scope.clock, None))
    assert repr(adapter) == "<MattermostAdapter https://mm.example.org disconnected>"
    assert adapter.options.reply_mode == "thread"
    assert "k" * 26 not in adapter.scrub("token " + "k" * 26)


def test_the_token_never_shows_in_logs_errors_or_repr(caplog: pytest.LogCaptureFixture) -> None:
    async def main() -> str:
        rig = Rig()
        await rig.connect()
        token = rig.wire.token
        assert token not in repr(rig.adapter) and token not in repr(rig.adapter._client)
        rig.wire.platform.fail(FailureKind.FORBIDDEN, op="send")
        failed = await drive(rig.clock, rig.adapter.send(CHANNEL, f"leak {token}"))
        assert not failed.ok and token not in (failed.error or "")
        rig.wire.drop_socket()
        await pump(rig.clock)
        await rig.close()
        return token

    with caplog.at_level(logging.DEBUG):
        token = run(main())
    assert all(token not in r.getMessage() for r in caplog.records)


def test_the_wire_fake_serves_every_client_route() -> None:
    assert dict(CLIENT_ROUTES) == ROUTES
    used = MattermostAdapter.vendor_names[
        "indus.gateway.adapters.mattermost.client:MattermostClient"
    ]
    assert set(used) <= set(CLIENT_ROUTES)
