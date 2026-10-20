"""P16-26: ntfy specifics over the fake server (the shared contract runs in tests/contract)."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.gateway.adapters.base import AdapterConnectError
from indus.gateway.adapters.ntfy import TOKEN_ENV, TOPIC_ENV, create_adapter
from indus.gateway.adapters.ntfy.adapter import (
    ECHO_TAG,
    NtfyAdapter,
    NtfyMarkdownAdapter,
    auth_header,
    valid_topic,
)
from indus.gateway.config import AdapterConfig, load_gateway_settings
from indus.gateway.core.authz import Authorizer, UnauthorizedHandler
from indus.gateway.core.events import InboundEvent
from indus.gateway.core.send import ErrorKind
from indus.gateway.registry import PlatformEntry, PlatformRegistry
from indus.state.pairing import PairingStore
from tests.contract.gateway import ntfy_case
from tests.contract.gateway.conftest import settle
from tests.fakes.clock import FakeClock
from tests.fakes.platform.ntfy_wire import NtfyWire

START = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
TOPIC = ntfy_case.TOPIC
TOKEN = ntfy_case.TOKEN


def run[T](coro: Awaitable[T]) -> T:
    async def main() -> T:
        return await coro

    return asyncio.run(main())


class Rig:
    def __init__(self, **kw: Any) -> None:
        self.clock = FakeClock(START)
        self.env = ntfy_case.make_env(self.clock)
        self.wire: NtfyWire = self.env.wire
        self.platform = self.env.platform
        self.adapter = ntfy_case.build_adapter(self.env, **kw)
        self.events: list[InboundEvent] = []
        self.fatal: list[tuple[str, str]] = []
        self.adapter.set_sink(self._sink)
        self.adapter.set_fatal_listener(lambda code, message: self.fatal.append((code, message)))

    async def _sink(self, event: InboundEvent) -> None:
        self.events.append(event)

    async def settle[T](self, awaitable: Awaitable[T]) -> T:
        return await settle(self.env, awaitable)

    async def connect(self) -> None:
        await self.settle(self.adapter.connect())
        await self.clock.run_until_idle()

    async def publish(self, text: str, **fields: Any) -> dict[str, Any]:
        event = self.wire._event(TOPIC, text, **fields)  # pyright: ignore[reportPrivateUsage]
        await self.clock.run_until_idle()
        return event

    async def at(self, seconds: float) -> None:
        """Move the fake clock to ``seconds`` after the start, a second at a time."""
        await self.clock.run_until_idle()
        while self.clock.monotonic() < seconds:
            await self.clock.advance_and_settle(min(1.0, seconds - self.clock.monotonic()))

    async def wait(self, seconds: float) -> None:
        await self.at(self.clock.monotonic() + seconds)


# -- R06.24 -------------------------------------------------------------------------------------


def test_unknown_sender_on_ntfy_never_gets_a_pairing_code(scope: Scope) -> None:
    rig = Rig()
    registry = PlatformRegistry(builtins=())
    # Even an entry that wrongly calls the platform private loses to the adapter's own flag.
    registry.register(
        PlatformEntry(name="ntfy", label="ntfy", factory=lambda c: None, check_fn=lambda: True)
    )
    authorizer = Authorizer.from_registry(
        registry,
        env=lambda _name: None,
        adapter_for=lambda p: rig.adapter if p == "ntfy" else None,
        global_unknown_dm=lambda: "pair",
        profile="work",
    )
    store = PairingStore(scope)
    handler = UnauthorizedHandler(authorizer, store)

    async def main() -> str:
        await rig.connect()
        await rig.publish("let me in")
        (event,) = rig.events
        assert not authorizer.check(event.source).granted

        async def reply(text: str) -> None:
            await rig.adapter.send(event.source.chat_id, text)

        outcome = await handler.respond(event.source, send_reply=reply)
        await rig.adapter.disconnect()
        return outcome.action

    assert run(main()) == "ignored"
    assert rig.platform.messages(TOPIC) == []  # nothing was published to the topic
    assert store.list_pending() == []
    assert rig.adapter.capabilities.dm_is_private is False


# -- the subscription ---------------------------------------------------------------------------


def test_a_stalled_stream_reconnects_at_90_seconds() -> None:
    rig = Rig()

    async def main() -> list[float]:
        await rig.connect()
        await rig.publish("first")
        await rig.at(89)
        assert len(rig.wire.subscriptions) == 1
        await rig.at(90)  # silent for 90 s: dropped, reopened after the first backoff step
        assert len(rig.wire.subscriptions) == 1
        await rig.at(92)
        assert len(rig.wire.subscriptions) == 2
        # a keepalive keeps the new stream alive past its own 90 s
        await rig.at(170)
        rig.wire.keepalive()
        await rig.at(250)
        assert len(rig.wire.subscriptions) == 2
        await rig.at(262)
        await rig.adapter.disconnect()
        return [float(len(rig.wire.subscriptions))]

    assert run(main()) == [3.0]
    second = rig.wire.subscriptions[1]
    assert second.params.get("since") == rig.events[0].message_id
    assert rig.adapter.reopened == 2


def test_drops_back_off_and_messages_from_the_gap_still_arrive() -> None:
    rig = Rig()

    async def main() -> list[float]:
        await rig.connect()
        await rig.publish("before")
        times: list[float] = []
        for _ in range(4):
            rig.wire.drop()
            await rig.clock.run_until_idle()
            before = len(rig.wire.subscriptions)
            while len(rig.wire.subscriptions) == before:
                await rig.wait(1)
            times.append(rig.clock.monotonic())
        rig.wire.drop()
        await rig.clock.run_until_idle()
        await rig.publish("while away")  # nobody is subscribed right now
        await rig.wait(40)  # the fifth step is 32 s
        await rig.adapter.disconnect()
        return times

    times = run(main())
    gaps = [round(b - a) for a, b in zip([0.0, *times], times, strict=False)]
    assert gaps == [2.0, 4.0, 8.0, 16.0]
    assert [e.text for e in rig.events] == ["before", "while away"]


def test_echoes_repeats_and_odd_lines_are_dropped() -> None:
    rig = Rig()

    async def main() -> None:
        await rig.connect()
        sent = await rig.settle(rig.adapter.send(TOPIC, "our own reply"))
        assert sent.ok
        await rig.clock.run_until_idle()
        event = await rig.publish("hello", title="I am the admin")
        rig.wire.send_raw(json.dumps(event))  # the same id again
        rig.wire.send_raw("not json")
        rig.wire.send_raw(json.dumps({"event": "poll_request", "id": "p1"}))
        rig.wire.send_raw(
            json.dumps({"event": "message", "id": "e1", "topic": TOPIC, "message": " "})
        )
        other = {"event": "message", "id": "o1", "topic": "someone-else", "message": "x"}
        rig.wire.send_raw(json.dumps(other))
        await rig.clock.run_until_idle()
        await rig.adapter.disconnect()

    run(main())
    (event,) = rig.events
    assert event.text == "hello" and event.source.chat_type == "dm"
    assert event.source.user_id == TOPIC and event.source.chat_id == TOPIC  # never the title
    assert event.metadata["title"] == "I am the admin"
    published = rig.wire.cache[0]
    assert published["tags"] == [ECHO_TAG]


def test_publish_parameters_and_credentials() -> None:
    rig = Rig()

    async def main() -> Any:
        await rig.connect()
        result = await rig.settle(
            rig.adapter.send(
                "alerts", "disk full", meta={"title": "Disk", "tags": ["warning"], "priority": 4}
            )
        )
        await rig.adapter.disconnect()
        return result

    result = run(main())
    assert result.ok and rig.platform.message(result.message_id or "") is not None
    (call,) = rig.wire.calls_to("POST", "/alerts")
    assert call.params == {"tags": f"warning,{ECHO_TAG}", "title": "Disk", "priority": "4"}
    assert call.headers["authorization"] == f"Bearer {TOKEN}"
    assert auth_header("bob:pw") == {
        "Authorization": "Basic " + base64.b64encode(b"bob:pw").decode()
    }
    assert auth_header("  ") == {}


def test_topic_names_are_checked_and_replies_can_go_to_a_reply_topic() -> None:
    rig = Rig(reply_topic="indus-out")

    async def main() -> tuple[Any, Any]:
        await rig.connect()
        bad = await rig.settle(rig.adapter.send("../admin", "x"))
        good = await rig.settle(rig.adapter.send(TOPIC, "answer"))
        await rig.adapter.disconnect()
        return bad, good

    bad, good = run(main())
    assert not bad.ok and bad.kind is ErrorKind.NOT_FOUND and bad.chat_unreachable
    assert good.ok and rig.platform.texts("indus-out") == ["answer"]
    assert not valid_topic("a/b") and not valid_topic("x" * 65) and valid_topic("ok_topic-1")
    with pytest.raises(ValueError):
        ntfy_case.build_adapter(rig.env, topics=("fine", "no spaces"))


def test_a_refused_credential_is_fatal_at_connect_and_while_running() -> None:
    rig = Rig()
    rig.wire.token = "another"

    async def connect_refused() -> AdapterConnectError:
        with pytest.raises(AdapterConnectError) as caught:
            await rig.connect()
        return caught.value

    error = run(connect_refused())
    assert not error.retryable and error.code == "ntfy_auth" and TOKEN not in str(error)

    later = Rig()

    async def main() -> int:
        await later.connect()
        later.wire.refuse_subscribe = 401
        later.wire.drop()
        await later.wait(120)
        count = len(later.wire.subscriptions)
        await later.wait(600)
        assert len(later.wire.subscriptions) == count  # no retry loop on a dead credential
        await later.adapter.disconnect()
        return count

    assert run(main()) == 2
    assert [code for code, _ in later.fatal] == ["ntfy_auth"]


# -- wiring -------------------------------------------------------------------------------------


def _config(scope: Scope, *, send_only: bool = False, extra: str = "") -> AdapterConfig:
    secrets: Any = scope.secrets
    secrets.set(TOPIC_ENV, f"{TOPIC},second")
    secrets.set(TOKEN_ENV, TOKEN)
    if extra:
        from tests.unit.cli.commands.cmdrun import write_settings

        write_settings(scope, extra)
    settings = load_gateway_settings(scope).get("ntfy")
    assert settings is not None and settings.enabled
    return AdapterConfig("ntfy", settings, scope, scope.clock, None, send_only=send_only)


def test_create_adapter_reads_topics_server_and_markdown(scope: Scope) -> None:
    adapter = create_adapter(
        _config(
            scope,
            extra="platforms:\n  ntfy:\n    server: https://push.example\n    markdown: true\n",
        )
    )
    assert isinstance(adapter, NtfyMarkdownAdapter) and adapter.capabilities.markup == "markdown"
    assert adapter.topics == (TOPIC, "second") and adapter.api.server == "https://push.example"
    assert TOKEN not in repr(adapter) and TOKEN not in repr(adapter.api)
    assert TOKEN not in adapter.scrub(f"failed with {TOKEN}")
    plain = create_adapter(_config(scope, extra="platforms:\n  ntfy:\n    markdown: false\n"))
    assert type(plain) is NtfyAdapter and plain.api.server == "https://ntfy.sh"


def test_a_token_is_never_sent_to_a_plain_http_server(scope: Scope) -> None:
    with pytest.raises(ValueError, match="https"):
        create_adapter(
            _config(scope, extra="platforms:\n  ntfy:\n    server: http://push.example\n")
        )
    local = create_adapter(
        _config(scope, extra="platforms:\n  ntfy:\n    server: http://127.0.0.1:8080\n")
    )
    assert local.api.server == "http://127.0.0.1:8080"


def test_indus_send_opens_no_subscription(scope: Scope, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock(START)
    env = ntfy_case.make_env(clock)
    wire: NtfyWire = env.wire
    monkeypatch.setattr("indus.gateway.adapters.ntfy.CLIENT_FACTORY", lambda _config: wire.client())
    adapter = create_adapter(_config(scope, send_only=True))
    adapter.api.server = wire.base_url

    async def main() -> Any:
        await adapter.connect()
        result = await adapter.send("ops-room", "deploy done")
        await adapter.disconnect()
        return result

    assert run(main()).ok
    assert wire.subscriptions == [] and env.platform.texts("ops-room") == ["deploy done"]
