"""P18-09: ``/api/pty`` over a real loopback server — gates, failures, channels, resume, the
child's argv/env, keep-alive attach, resize and close codes.

Most cases use a fake bridge (no process); the last ones run the fake TUI child on a real PTY
(they need ``ptyprocess``, the ``dashboard`` extra). The app is the real one (``chat_rig``): every
socket passes the real WebSocket helper with a real credential.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.clock import SystemClock
from indus.foundation.scope import Scope
from indus.service.ws_auth import CredentialKind
from indus.web.pty import bridge as pty_bridge
from indus.web.pty.bridge import WINDOWS_NOTICE, PtyBridge
from indus.web.pty.channels import ChannelFiles
from indus.web.pty.registry import CLOSE_EXITED, CLOSE_SUPERSEDED, REDRAW, PtyRegistry
from indus.web.pty.resize import TermSize, resize_frame
from indus.web.ws.chat_gate import PURPOSE_PTY, purpose_of
from indus.web.ws.chat_routes import ChatSeams
from indus.web.ws.pty_route import (
    NODE_MISSING,
    PTY_HOST_ENV,
    SESSION_FILE_ENV,
    SIDECAR_CREDENTIAL_ENV,
    SIDECAR_URL_ENV,
    ChildRequest,
    ChildSpec,
    PtyRoute,
    TuiChild,
    lease_holder_pid,
)
from tests.contract.web.chat_rig import ChatServer, chat_server
from tests.fakes.pty.fake_bridge import FakeBridge
from tests.fakes.pty.ws_wire import WireClient
from tests.unit.backends._aio import aio

REPO = Path(__file__).resolve().parents[3]
SID = "20261020_101010_chat01"
RED = "\x1b[31m"


async def until(check: Any, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not check():
            await SystemClock().sleep(0.01)


class Rig:
    """The real app with fake machine pieces under ``/api/pty``; ``run()`` serves it."""

    def __init__(self, scope: Scope, tmp: Path, **kw: Any) -> None:
        self.scope = scope
        self.tmp = tmp
        self.base = ""
        self.gated = bool(kw.pop("gate", {}).get("gated", False))
        self.registry = PtyRegistry(clock=SystemClock(), **kw.pop("registry", {}))
        self.channels = ChannelFiles(tmp / "chat")
        self.bridges: list[FakeBridge] = []
        self.requests: list[ChildRequest] = []
        self.spawned: list[tuple[tuple[str, ...], Path, dict[str, str]]] = []
        self.seams = ChatSeams(
            child=kw.pop("child", self.child),
            spawn=kw.pop("spawn", self.spawn),
            unavailable=kw.pop("unavailable", lambda: None),
            lease_holder=kw.pop("lease_holder", lambda _scope, _sid: None),
            registry=self.registry,
            channels=self.channels,
        )
        self.server: ChatServer | None = None

    @contextlib.asynccontextmanager
    async def run(self) -> AsyncIterator[Rig]:
        async with chat_server(self.scope, gated=self.gated, seams=self.seams) as server:
            self.server, self.base = server, server.base
            yield self

    @property
    def route(self) -> PtyRoute:
        assert self.server is not None
        return self.server.chat.pty

    def child(self, request: ChildRequest) -> ChildSpec:
        self.requests.append(request)
        return ChildSpec(("fake-tui",), self.tmp, {"FAKE": "1"})

    def spawn(self, argv: tuple[str, ...], *, cwd: Path, env: dict[str, str]) -> FakeBridge:
        self.spawned.append((tuple(argv), cwd, dict(env)))
        bridge = FakeBridge()
        self.bridges.append(bridge)
        return bridge

    async def connect(self, query: str = "", kind: CredentialKind = "token") -> WireClient:
        assert self.server is not None
        url = f"{self.base}/api/pty" + (f"?{query}" if query else "")
        return await WireClient.connect(url, self.server.offer(kind, purpose=PURPOSE_PTY))

    async def finish(self, *clients: WireClient) -> None:
        for client in clients:
            await client.close()
        await self.registry.close_all()
        self.channels.close()


async def failure_text(client: WireClient) -> tuple[str, int]:
    text = ""
    while True:
        try:
            message = await client.recv()
        except Exception as exc:
            return text, int(getattr(exc, "code", 0))
        if isinstance(message, str):
            text += message


@aio
async def test_failures_show_red_text_and_close_1011(scope: Scope, tmp_path: Path) -> None:
    cases: list[tuple[dict[str, Any], str, str]] = [
        ({}, "profile=nope", "no such profile"),
        ({"unavailable": lambda: WINDOWS_NOTICE}, "", "WSL2"),
        ({"child": TuiChild(which=lambda _name, _path: None)}, "", NODE_MISSING),
        ({}, "cwd=relative/dir", "absolute path"),
        ({}, f"cwd={tmp_path}/missing", "does not exist"),
    ]
    for kw, query, expected in cases:
        rig = Rig(scope, tmp_path, **kw)
        async with rig.run():
            client = await rig.connect(query)
            text, code = await failure_text(client)
            assert code == 1011, (query, text)
            assert RED in text and expected in text
            await rig.finish(client)
        assert rig.bridges == []


@aio
async def test_the_gate_decides_first(scope: Scope, tmp_path: Path) -> None:
    rig = Rig(scope, tmp_path, gate={"gated": True})
    async with rig.run():
        client = await rig.connect("channel=c1")
        assert (await client.closed_with()).code == 4401  # loopback token refused in gated mode
        ticketed = await rig.connect("channel=c1", kind="ticket")
        await until(lambda: len(rig.bridges) == 1)
        nobody = await rig.connect("profile=nope", kind="ticket")
        text, code = await failure_text(nobody)
        assert code == 1011 and "no such profile" in text  # P18-09: red text, never a PTY
        internal = await rig.connect("channel=c2", kind="internal")
        assert (await internal.closed_with()).code == 4401  # no terminal for the server's child
        assert len(rig.bridges) == 1
        assert purpose_of(rig.route.endpoint) == PURPOSE_PTY
        await rig.finish(ticketed)


@aio
async def test_resize_frames_are_applied_never_written(scope: Scope, tmp_path: Path) -> None:
    rig = Rig(scope, tmp_path)
    async with rig.run():
        client = await rig.connect()
        await until(lambda: len(rig.bridges) == 1)
        bridge = rig.bridges[0]
        await client.send_text(resize_frame(5000, 0).decode())
        await client.send_bytes(b"hi")
        await client.send_bytes(resize_frame(120, 40) + b"x")  # not exactly the escape: input
        await until(lambda: len(bridge.written) == 2)
        assert bridge.sizes == [TermSize(2000, 1)]
        assert bridge.written == [b"hi", resize_frame(120, 40) + b"x"]
        bridge.emit(b"output")
        assert await client.recv() == b"output"
        bridge.exit()
        assert (await client.closed_with()).code == CLOSE_EXITED
        await rig.finish(client)


@aio
async def test_one_to_one_ends_the_child_and_drops_its_marker(scope: Scope, tmp_path: Path) -> None:
    rig = Rig(scope, tmp_path)
    async with rig.run():
        client = await rig.connect("channel=c1")
        await until(lambda: len(rig.bridges) == 1)
        assert rig.channels.known("c1")
        await client.close()
        await until(lambda: rig.bridges[0].closed)
        await until(lambda: not rig.channels.known("c1"))
        await rig.finish()


@aio
async def test_stalled_input_closes_1013(scope: Scope, tmp_path: Path) -> None:
    rig = Rig(scope, tmp_path)
    async with rig.run():
        client = await rig.connect("attach=tab1")
        await until(lambda: len(rig.bridges) == 1)
        rig.bridges[0].stall()
        await client.send_bytes(b"x")
        assert (await client.closed_with()).code == 1013
        await rig.finish(client)


@aio
async def test_reattach_supersedes_replays_and_redraws(scope: Scope, tmp_path: Path) -> None:
    rig = Rig(scope, tmp_path)
    async with rig.run():
        first = await rig.connect("attach=tab1&channel=c1")
        await until(lambda: len(rig.bridges) == 1)
        bridge = rig.bridges[0]
        bridge.emit(b"transcript")
        assert await first.recv() == b"transcript"
        second = await rig.connect("attach=tab1&channel=c2")
        assert (await first.closed_with()).code == CLOSE_SUPERSEDED
        assert await second.recv() == b"transcript"
        await until(lambda: bridge.written == [REDRAW])
        assert len(rig.bridges) == 1  # same PTY
        assert not rig.channels.known("c2")  # that PTY keeps its own channel file
        assert rig.channels.known("c1")
        await second.close()
        await until(lambda: not rig.registry.sessions()[0].attached)
        assert not bridge.closed  # a disconnect only detaches
        third = await rig.connect("attach=tab1&channel=c3")
        assert await third.recv() == b"transcript"
        bridge.exit()
        assert (await third.closed_with()).code == CLOSE_EXITED
        await rig.finish(third)
        assert not rig.channels.known("c1")  # released with its PTY


@aio
async def test_a_profile_switch_ends_the_token_s_old_pty(scope: Scope, tmp_path: Path) -> None:
    other = scope.root / "profiles" / "other"
    other.mkdir(parents=True)
    (other / "profile.yaml").touch()
    rig = Rig(scope, tmp_path)
    async with rig.run():
        first = await rig.connect("attach=tab1")
        await until(lambda: len(rig.bridges) == 1)
        second = await rig.connect("attach=tab1&profile=other")
        assert (await first.closed_with()).code == CLOSE_SUPERSEDED
        await until(lambda: len(rig.bridges) == 2 and rig.bridges[0].closed)
        assert rig.requests[-1].scope.profile_id == "other"
        assert rig.requests[-1].scope.home == other
        await rig.finish(second)


@aio
async def test_implicit_resume_is_announced_explicit_is_not(scope: Scope, tmp_path: Path) -> None:
    rig = Rig(scope, tmp_path)
    async with rig.run():
        first = await rig.connect("attach=tab1&channel=c1")
        await until(lambda: len(rig.bridges) == 1)
        marker = rig.channels.path("c1")
        assert marker is not None
        assert rig.requests[0].session_file == marker
        marker.write_text(f'{{"session_id": "{SID}"}}', encoding="utf-8")  # the TUI reports
        await first.close()

        again = await rig.connect("attach=tab2&channel=c1")
        announced = await again.recv()
        assert isinstance(announced, str)
        assert json.loads(announced) == {"type": "resume", "id": SID}
        await until(lambda: len(rig.requests) == 2)
        assert rig.requests[1].resume == SID
        assert rig.requests[1].cwd is None  # a resumed chat keeps its own folder

        explicit = await rig.connect(f"attach=tab3&channel=c1&resume=other_id&cwd={tmp_path}")
        await until(lambda: len(rig.requests) == 3)
        assert rig.requests[2].resume == "other_id"
        with pytest.raises(TimeoutError):
            await explicit.recv(timeout=0.3)  # nothing announced

        fresh = await rig.connect(f"attach=tab4&channel=c1&fresh=1&resume=x&cwd={tmp_path}")
        await until(lambda: len(rig.requests) == 4)
        assert rig.requests[3].resume is None
        assert rig.requests[3].cwd == tmp_path.resolve()
        assert rig.channels.read("c1") is None
        await rig.finish(again, explicit, fresh)


@aio
async def test_orphans_holding_the_resumed_chat_are_ended(scope: Scope, tmp_path: Path) -> None:
    asked: list[tuple[str, str]] = []

    def holder(lookup_scope: Scope, session_id: str) -> int | None:
        asked.append((lookup_scope.profile_id, session_id))
        return None

    rig = Rig(scope, tmp_path, lease_holder=holder)
    async with rig.run():
        old = await rig.connect(f"attach=old&resume={SID}")
        await until(lambda: len(rig.bridges) == 1)
        await old.close()
        new = await rig.connect(f"attach=new&resume={SID}")
        await until(lambda: len(rig.bridges) == 2)
        await until(lambda: rig.bridges[0].closed)
        assert asked[-1] == (scope.profile_id, SID)
        await rig.finish(new)


@aio
async def test_a_full_registry_explains_and_closes_1011(scope: Scope, tmp_path: Path) -> None:
    rig = Rig(scope, tmp_path, registry={"max_sessions": 1})
    async with rig.run():
        holder = await rig.connect("attach=one")
        await until(lambda: len(rig.bridges) == 1)
        other = await rig.connect("attach=two")
        text, code = await failure_text(other)
        assert code == 1011 and "close one" in text
        await rig.finish(holder)


def test_child_env_comes_from_the_builder(scope: Scope, tmp_path: Path) -> None:
    marker = tmp_path / "c1.json"
    leaky = "PROVIDER_" + "API_KEY"
    view = scope.env.with_overlay(
        {"OPENROUTER_API_KEY": "x" * 12, "INDUS_TUI_YOLO": "1", "HOME": str(tmp_path)}
    )
    request = ChildRequest(
        scope=scope.with_env(view),
        resume=SID,
        cwd=None,
        session_file=marker,
        sidecar_url="ws://127.0.0.1:9119/api/pub?channel=c1",
        sidecar_credential="internal-value",
    )
    spec = TuiChild(python="/py", which=lambda _name, _path: "/usr/bin/node")(request)
    assert spec.argv == ("/py", "-m", "indus", "--tui", "-r", SID)
    assert spec.cwd == tmp_path
    env = dict(spec.env)
    assert env["INDUS_HOME"] == str(scope.home)
    assert env[SESSION_FILE_ENV] == str(marker)
    assert env[SIDECAR_URL_ENV].endswith("/api/pub?channel=c1")
    assert env[SIDECAR_CREDENTIAL_ENV] == "internal-value"
    assert "internal-value" not in env[SIDECAR_URL_ENV]
    assert env[PTY_HOST_ENV] == "dashboard" and env["COLORTERM"] == "truecolor"
    assert env["INDUS_TUI_MOUSE"] == "off"
    assert "INDUS_TUI_YOLO" not in env  # nothing of the backend's own launch leaks in
    assert "OPENROUTER_API_KEY" not in env  # provider keys never ride along (R02.22)
    assert leaky not in env
    assert "credential" not in repr(spec) and "internal-value" not in repr(request)

    fresh = TuiChild(python="/py", which=lambda _name, _path: "/n")(
        ChildRequest(scope, None, tmp_path, None, None)
    )
    assert fresh.argv == ("/py", "-m", "indus", "--tui")
    assert fresh.cwd == tmp_path
    assert SIDECAR_URL_ENV not in fresh.env and SESSION_FILE_ENV not in fresh.env


def test_lease_holder_reads_browse_only(scope: Scope) -> None:
    assert lease_holder_pid(scope, SID) is None  # no store yet: nothing is created
    assert not (scope.home / "state.db").exists()


# -- a real PTY and the fake TUI child -------------------------------------------------------------

needs_pty = pytest.mark.skipif(
    sys.platform == "win32" or pty_bridge.unavailable() is not None,
    reason="needs a POSIX PTY and ptyprocess (the dashboard extra)",
)


@needs_pty
@aio
async def test_a_real_child_never_sees_the_resize_escape(scope: Scope, tmp_path: Path) -> None:
    started: list[PtyBridge] = []

    def child(request: ChildRequest) -> ChildSpec:
        env = {"PATH": os.defpath, "PYTHONPATH": str(REPO), "HOME": str(tmp_path)}
        return ChildSpec((sys.executable, "-m", "tests.fakes.pty.fake_tui"), tmp_path, env)

    def spawn(argv: tuple[str, ...], *, cwd: Path, env: dict[str, str]) -> PtyBridge:
        bridge = pty_bridge.spawn(argv, cwd=cwd, env=env)
        started.append(bridge)
        return bridge

    rig = Rig(scope, tmp_path, child=child, spawn=spawn)
    async with rig.run():
        client = await rig.connect()
        seen = b""

        async def read_until(needle: bytes) -> None:
            nonlocal seen
            async with asyncio.timeout(15):
                while needle not in seen:
                    message = await client.recv()
                    seen += message if isinstance(message, bytes) else message.encode()

        await read_until(b"ready")
        await client.send_text(resize_frame(120, 40).decode())
        await client.send_bytes(b"hello")
        await read_until(b"got:" + b"hello".hex().encode())
        await client.send_bytes(b"size?")
        await read_until(b"size 120x40")
        assert b"\x1b[RESIZE".hex().encode() not in seen
        await client.close()
        await until(lambda: started and not started[0].is_alive(), timeout=10)
        await rig.finish()


@aio
async def test_the_ticket_s_profile_decides_the_chat_s_profile(
    scope: Scope, tmp_path: Path
) -> None:
    from indus.service.ws_auth import dashboard_offer, stable_profile_id

    other = scope.root / "profiles" / "other"
    other.mkdir(parents=True)
    (other / "profile.yaml").touch()
    rig = Rig(scope, tmp_path, gate={"gated": True})
    async with rig.run():
        assert rig.server is not None
        book = rig.server.state.tickets
        bound = book.mint(PURPOSE_PTY, profile_id=stable_profile_id(other)).ticket
        client = await WireClient.connect(
            f"{rig.base}/api/pty?channel=c1", dashboard_offer("ticket", bound)
        )
        await until(lambda: len(rig.requests) == 1)
        assert rig.requests[0].scope.home == other  # not the backend's own profile
        crossed = book.mint(PURPOSE_PTY, profile_id=stable_profile_id(other)).ticket
        refused = await WireClient.connect(
            f"{rig.base}/api/pty?profile={scope.profile_id}", dashboard_offer("ticket", crossed)
        )
        assert (await refused.closed_with()).code == 4401  # wrong_profile
        await rig.finish(client)
