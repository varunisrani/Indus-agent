"""The Raft adapter (P16-30): the loopback wake endpoint for Raft's bridge."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import stat
from collections.abc import AsyncIterator, Awaitable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from indus.foundation.config.schema import PlatformSection
from indus.foundation.scope import Scope
from indus.gateway.adapters.raft import create_adapter
from indus.gateway.adapters.raft.adapter import (
    CONTENT_MAX,
    QUEUE_CAP,
    TOKEN_HEADER,
    ActivityError,
    ActivityQueue,
    RaftAdapter,
    port_file,
    validate_activity,
    wake_prompt,
)
from indus.gateway.config import AdapterConfig, PlatformSettings
from indus.gateway.core.authz import Authorizer
from indus.gateway.core.events import InboundEvent
from indus.gateway.registry import PlatformRegistry
from tests.fakes.clock import FakeClock

LOCAL = ("127.0.0.1", 50123)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("real network used in a Raft adapter test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield


def run[T](coro: Awaitable[T]) -> T:
    return asyncio.run(coro)  # pyright: ignore[reportArgumentType]


def _token() -> str:
    return "".join(["raft", "-bridge-", "9f8e" * 6])


class Rig:
    def __init__(self, home: Path | None, *, sink: bool = True, **kw: Any) -> None:
        self.clock = FakeClock(datetime(2026, 1, 1, 9, 0, tzinfo=UTC))
        kw.setdefault("token", _token())
        kw.setdefault("serve", False)
        self.adapter = RaftAdapter(clock=self.clock, home=home, profile="ops-agent", **kw)
        self.events: list[InboundEvent] = []
        self.gate: asyncio.Event | None = None
        if sink:
            self.adapter.set_sink(self.sink)

    async def sink(self, event: InboundEvent) -> None:
        self.events.append(event)
        if self.gate is not None:
            await self.gate.wait()

    async def post(
        self,
        path: str,
        body: Any = None,
        *,
        client: tuple[str, int] = LOCAL,
        token: str | None = "default",
        content: Any = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        listener = self.adapter.listener
        assert listener is not None
        transport = httpx.ASGITransport(app=listener.app, client=client)  # pyright: ignore[reportArgumentType]
        sent = dict(headers or {})
        if token is not None:
            sent[TOKEN_HEADER] = self.adapter.token if token == "default" else token
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as http:
            if content is None:
                content = json.dumps(body).encode() if body is not None else b""
            return await http.post(path, content=content, headers=sent)

    async def get(self, path: str) -> httpx.Response:
        listener = self.adapter.listener
        assert listener is not None
        transport = httpx.ASGITransport(app=listener.app, client=LOCAL)  # pyright: ignore[reportArgumentType]
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as http:
            return await http.get(path, headers={TOKEN_HEADER: self.adapter.token})

    async def settle(self) -> None:
        await self.clock.run_until_idle()


# -- the row's Verify ------------------------------------------------------------------------------


def test_a_non_loopback_request_is_refused_before_anything_is_read(tmp_path: Path) -> None:
    async def main() -> None:
        rig = Rig(tmp_path)
        await rig.adapter.connect()
        far = await rig.post("/wake", {"eventId": "e1"}, client=("203.0.113.7", 4000))
        assert far.status_code == 403 and far.json() == {"error": "loopback only"}
        lan = await rig.post("/activity/drain", {}, client=("10.0.0.5", 4000))
        assert lan.status_code == 403
        await rig.settle()
        assert rig.events == [] and len(rig.adapter.wakes) == 0
        for host in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            near = await rig.post("/wake", {"eventId": f"e-{host}"}, client=(host, 4000))
            assert near.status_code == 202, host
        await rig.adapter.disconnect()

    run(main())


def test_the_501st_queued_wake_drops_the_oldest_with_a_log_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async def main() -> None:
        rig = Rig(tmp_path)
        rig.gate = asyncio.Event()  # the gateway is slow: the first wake's turn holds the pump
        await rig.adapter.connect()
        assert (await rig.post("/wake", {"eventId": "w0"})).status_code == 202
        await rig.settle()
        assert [e.metadata["raft_wakes"] for e in rig.events] == [["w0"]]
        for n in range(1, QUEUE_CAP + 2):
            answer = await rig.post("/wake", {"eventId": f"w{n}"})
            assert answer.status_code == 202
        assert len(rig.adapter.wakes) == QUEUE_CAP and rig.adapter.wakes.dropped == 1
        dropped = [r for r in caplog.records if "dropped the oldest wake" in r.getMessage()]
        assert len(dropped) == 1 and "w1" in dropped[0].getMessage()
        rig.gate.set()
        await rig.settle()
        second = rig.events[1].metadata["raft_wakes"]
        assert second[0] == "w2" and second[-1] == f"w{QUEUE_CAP + 1}" and len(second) == 500
        assert len(rig.events) == 2  # the waiting wakes were folded into one turn
        await rig.adapter.disconnect()

    caplog.set_level(logging.WARNING)
    run(main())


def test_an_oversized_field_is_413(tmp_path: Path) -> None:
    async def main() -> None:
        rig = Rig(tmp_path)
        await rig.adapter.connect()
        long = "x" * 121
        for body in (
            {"eventId": long},
            {"meta": {"attempt": {"note": long}}},
            {"tags": ["ok", long]},
            {long: 1},
        ):
            answer = await rig.post("/wake", body)
            assert answer.status_code == 413 and answer.json()["error"] == "field_too_large"
        exact = await rig.post("/wake", {"eventId": "y" * 120})
        assert exact.status_code == 202
        await rig.adapter.disconnect()

    run(main())


# -- the endpoint ----------------------------------------------------------------------------------


def test_an_oversized_body_is_413_without_being_read(tmp_path: Path) -> None:
    async def main() -> None:
        rig = Rig(tmp_path)
        await rig.adapter.connect()
        declared = await rig.post("/wake", content=b"{" + b" " * 16 * 1024 + b"}")
        assert declared.status_code == 413
        pulled: list[int] = []

        async def chunks() -> AsyncIterator[bytes]:
            for n in range(64):
                pulled.append(n)
                yield b" " * 1024

        streamed = await rig.post("/wake", content=chunks())
        assert streamed.status_code == 413 and len(pulled) < 64
        assert len(rig.adapter.wakes) == 0 and rig.events == []
        await rig.adapter.disconnect()

    run(main())


def test_content_bearing_and_malformed_wakes_are_refused(tmp_path: Path) -> None:
    async def main() -> None:
        rig = Rig(tmp_path)
        await rig.adapter.connect()
        for body in ({"text": "hello"}, {"data": {"Preview": "x"}}, {"items": [{"body": "x"}]}):
            answer = await rig.post("/wake", body)
            assert answer.status_code == 400 and answer.json()["error"] == "content_not_allowed"
        assert (await rig.post("/wake", content=b"{nope")).json()["error"] == "invalid_json"
        assert (await rig.post("/wake", [1, 2])).json()["error"] == "invalid_payload"
        deep: Any = {"a": 1}
        for _ in range(8):
            deep = {"a": deep}
        assert (await rig.post("/wake", deep)).json()["error"] == "invalid_payload"
        await rig.settle()
        assert rig.events == []
        await rig.adapter.disconnect()

    run(main())


def test_the_bridge_token_is_required(tmp_path: Path) -> None:
    async def main() -> None:
        rig = Rig(tmp_path)
        await rig.adapter.connect()
        for token in (None, "wrong", rig.adapter.token[:-1]):
            assert (await rig.post("/wake", {}, token=token)).status_code == 401
            assert (await rig.post("/activity", {}, token=token)).status_code == 401
        await rig.settle()
        assert rig.events == []
        await rig.adapter.disconnect()

    run(main())


def test_a_wake_becomes_one_internal_turn_with_our_prompt(tmp_path: Path) -> None:
    async def main() -> None:
        rig = Rig(tmp_path, runtime_session="team-a")
        await rig.adapter.connect()
        answer = await rig.post("/wake", {"eventId": "evt-7", "schema": "wake.v1"})
        assert answer.status_code == 202 and answer.json()["runtimeSession"] == "team-a"
        empty = await rig.post("/wake", content=b"")
        assert empty.status_code == 202
        await rig.settle()
        first, second = rig.events
        assert first.internal and not first.allow_control and first.command is None
        assert first.text == wake_prompt("ops-agent")
        assert "--profile ops-agent" in first.text and "raft manual get" in first.text
        assert first.message_id == "evt-7" and first.source.platform == "raft"
        assert first.source.chat_id == "team-a" and first.source.chat_type == "dm"
        assert second.message_id and second.message_id.startswith("raft-wake-")
        await rig.adapter.disconnect()

    run(main())


def test_wakes_before_the_gateway_is_ready_get_503(tmp_path: Path) -> None:
    async def main() -> None:
        rig = Rig(tmp_path, sink=False)
        await rig.adapter.connect()
        answer = await rig.post("/wake", {"eventId": "early"})
        assert answer.status_code == 503 and answer.json()["error"] == "not_ready"
        assert len(rig.adapter.wakes) == 0
        await rig.adapter.disconnect()

    run(main())


def test_the_port_file_is_owner_only_and_names_the_ephemeral_port(tmp_path: Path) -> None:
    async def main() -> None:
        home = tmp_path / "profile"
        home.mkdir()
        rig = Rig(home, serve=True, token=None)
        await rig.adapter.connect()
        listener = rig.adapter.listener
        assert listener is not None and listener.address is not None
        host, port = listener.address
        assert host == "127.0.0.1" and port > 0
        path = port_file(home)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        record = json.loads(path.read_text())
        assert record["port"] == port and record["url"] == f"http://127.0.0.1:{port}/wake"
        assert record["header"] == TOKEN_HEADER
        assert record["token"] == rig.adapter.token and len(record["token"]) >= 40
        assert rig.adapter.token not in rig.adapter.scrub(f"leak {rig.adapter.token}")
        await rig.adapter.disconnect()
        assert not path.exists()

    run(main())


def test_send_only_records_replies_and_opens_nothing(tmp_path: Path) -> None:
    async def main() -> None:
        rig = Rig(tmp_path, send_only=True)
        await rig.adapter.connect()
        assert rig.adapter.listener is None and not port_file(tmp_path).exists()
        result = await rig.adapter.send("default", "done, answered in Raft")
        assert result.ok and result.message_id
        assert list(rig.adapter.replies) == [("default", "done, answered in Raft")]
        await rig.adapter.disconnect()

    run(main())


# -- activity telemetry ----------------------------------------------------------------------------


def _activity(**kw: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "schema": "raft-activity.v1",
        "eventId": "ev-1",
        "sessionId": "sess-1",
        "hookEventName": "PostToolUse",
        "status": "ok",
        "occurredAt": "2026-01-01T09:00:00Z",
    }
    event.update(kw)
    return event


def test_activity_events_are_checked_cut_and_drained(tmp_path: Path) -> None:
    async def main() -> None:
        rig = Rig(tmp_path)
        await rig.adapter.connect()
        big = "o" * (CONTENT_MAX + 50)
        ok = await rig.post("/activity", _activity(toolName="terminal", toolOutput=big))
        assert ok.status_code == 202
        assert (await rig.post("/activity", _activity(schema="v0"))).status_code == 400
        assert (await rig.post("/activity", _activity(extra=1))).status_code == 400
        assert (await rig.post("/activity", _activity(toolName="t" * 121))).status_code == 413
        assert (await rig.post("/activity", _activity(status="maybe"))).status_code == 400
        assert (await rig.post("/activity", _activity(durationMs=-1))).status_code == 400
        assert rig.adapter.report_activity(_activity(eventId="ev-2"))
        assert not rig.adapter.report_activity({"schema": "nope"})
        drained = (await rig.get("/activity/drain?max=1")).json()
        assert drained["schema"] == "raft-activity-drain.v1" and drained["dropped"] == 0
        (event,) = drained["events"]
        assert len(event["toolOutput"]) == CONTENT_MAX
        assert event["toolOutputTruncated"] is True and event["truncated"] is True
        rest = (await rig.get("/activity/drain")).json()
        assert [e["eventId"] for e in rest["events"]] == ["ev-2"]
        await rig.adapter.disconnect()

    run(main())


def test_the_activity_queue_drops_the_oldest_and_counts_them() -> None:
    queue = ActivityQueue(cap=2)
    for n in range(5):
        queue.push(_activity(eventId=f"e{n}"))
    drained = queue.drain()
    assert [e["eventId"] for e in drained["events"]] == ["e3", "e4"]
    assert drained["dropped"] == 3 and queue.drain()["dropped"] == 0
    with pytest.raises(ActivityError):
        validate_activity(_activity(sessionId="has\nnewline"))


# -- a broadcast surface (R06.24) ------------------------------------------------------------------


def test_raft_never_gets_pairing_codes(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    assert RaftAdapter.capabilities.dm_is_private is False
    assert RaftAdapter.capabilities.push_capable is False
    authorizer = Authorizer.from_registry(
        PlatformRegistry(),
        env=lambda _name: None,
        adapter_for=lambda name: rig.adapter if name == "raft" else None,
    )
    assert authorizer.dm_is_private("raft") is False


# -- create_adapter --------------------------------------------------------------------------------


def test_create_adapter_reads_the_profile_token_and_port(scope: Scope) -> None:
    secrets: Any = scope.secrets
    secrets.set("RAFT_PROFILE", "ops-agent")
    secrets.set("RAFT_CHANNEL_TOKEN", _token())
    section = PlatformSection.model_validate({"port": 0, "runtime_session": "team-b"})
    settings = PlatformSettings("raft", True, "credentials", section)
    adapter = create_adapter(AdapterConfig("raft", settings, scope, scope.clock, None))
    assert adapter.profile == "ops-agent" and adapter.runtime_session == "team-b"
    assert adapter.home == scope.home
    assert _token() not in adapter.scrub("leak " + _token())
    assert repr(adapter) == "<RaftAdapter team-b disconnected>"
    bad = PlatformSettings("raft", True, "x", PlatformSection.model_validate({"port": 70000}))
    with pytest.raises(ValueError, match="port"):
        create_adapter(AdapterConfig("raft", bad, scope, scope.clock, None))
    unsafe = PlatformSettings(
        "raft", True, "x", PlatformSection.model_validate({"runtime_session": "a\nb"})
    )
    fallback = create_adapter(AdapterConfig("raft", unsafe, scope, scope.clock, None))
    assert fallback.runtime_session == "default"
