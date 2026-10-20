"""P17-24: profile binding on attach — a client talks only to its own profile's daemon, every daemon
RPC runs under the daemon's one scope, and launch options never reach a daemon.

Real control sockets live in short temp homes under /tmp (a Unix socket path is short) and are
stopped by every test; the probe process the launch-env test starts exits by itself. No network.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from indus.cli.commands import gateway_grant
from indus.cli.commands.gateway_grant import EXIT_RETRY, GrantRefused
from indus.foundation.scope import Scope, current_scope
from indus.foundation.secrets import open_secret_store
from indus.gateway.control_socket import ControlServer
from indus.gateway.daemon.attach_verbs import AttachVerbs
from indus.gateway.ws.server import WsEndpoint
from indus.protocol.errors import PROFILE_MISMATCH
from indus.service import gateway_control, gateway_ensure
from indus.service.remote import profile_guard
from indus.service.remote.channel import Channel, Dialed, DialRefused, grant_for
from indus.service.remote.profile_guard import (
    CLOSE_PROFILE_MISMATCH,
    FOREIGN_LISTENER,
    ProfileMismatch,
    ScopeNotBound,
    bind_daemon_scope,
    check_described,
    check_profile,
    check_ready,
    expected_profile_id,
    listener_is_ours,
    socket_peer_uid,
)
from indus.service.rpc import MethodTable
from indus.service.ws_auth import SUBPROTOCOL, TICKET_PROTOCOL_PREFIX, WS_INTERACTIVE, TicketBook
from indus.state import registry
from indus.state.registry import StoreHandle
from tests.fakes.agent import eventually
from tests.fakes.asgi_ws import AsgiWsClient
from tests.fakes.clock import FakeClock
from tests.fakes.session_host import open_host
from tests.unit.backends._aio import aio
from tests.unit.cli.commands.cmdrun import run_cli
from tests.unit.service.remote.wire import ScriptedTransport

WS_URL = "ws://127.0.0.1:47012/ws"


@pytest.fixture
def store(scope: Scope) -> Iterator[StoreHandle]:
    handle = registry.open_unshared(scope)
    yield handle
    handle.close()


def _short_home(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix, dir="/tmp"))


@pytest.fixture
def homes() -> Iterator[tuple[Path, Path]]:
    """Two profile homes, A (the client's) and B (another profile's daemon)."""
    a, b = _short_home("igw-a-"), _short_home("igw-b-")
    try:
        yield a, b
    finally:
        shutil.rmtree(a, ignore_errors=True)
        shutil.rmtree(b, ignore_errors=True)


class Recording(AttachVerbs):
    """The real attach verbs of a daemon, remembering which verbs were asked."""

    def __init__(self, home: Path) -> None:
        super().__init__(home, TicketBook.for_home(home), ws_url=lambda: WS_URL, version="9.9.9")
        self.asked: list[str] = []
        self.minted = 0

    async def describe(self, params: dict[str, Any]) -> dict[str, Any]:
        self.asked.append("describe")
        return await super().describe(params)

    async def session_ticket(self, params: dict[str, Any]) -> dict[str, Any]:
        self.asked.append("session-ticket")
        answer = await super().session_ticket(params)
        self.minted += 1
        return answer


def serve(home: Path, verbs: Recording, body: Callable[[], Awaitable[None]]) -> None:
    """A real control server for ``home`` with ``verbs`` while ``body`` runs; always stopped."""

    async def main() -> None:
        server = ControlServer(home, status=lambda: {"state": "running"}, env={})
        verbs.install(server)
        await server.start()
        try:
            await body()
        finally:
            await server.stop()
            verbs.close()

    asyncio.run(main())


def point_at(home: Path, target_socket: Path) -> None:
    """Make ``home``'s control socket lead to ``target_socket`` (the long-path pointer file)."""
    pointer = gateway_control.pointer_path(home)
    pointer.write_text(str(target_socket), encoding="utf-8")
    pointer.chmod(0o600)


# -- the client side -------------------------------------------------------------------------------


def test_client_for_a_refuses_b_socket_before_any_ticket(homes: tuple[Path, Path]) -> None:
    home_a, home_b = homes
    verbs_b = Recording(home_b)
    seen: dict[str, Any] = {}

    async def body() -> None:
        found = gateway_control.socket_path_for(home_b)
        assert found is not None
        point_at(home_a, found)
        assert gateway_control.socket_path_for(home_a) == found  # A's path leads to B's daemon
        seen["described"] = await asyncio.to_thread(gateway_control.describe, home_a)
        with pytest.raises(DialRefused) as refused:
            await asyncio.to_thread(grant_for, home_a)
        seen["refused"] = refused.value
        seen["own"] = await asyncio.to_thread(grant_for, home_b)  # B's own client is served

    serve(home_b, verbs_b, body)
    assert seen["described"]["profile_id"] == expected_profile_id(home_b)
    refused: DialRefused = seen["refused"]
    assert refused.reason == "profile_mismatch" and refused.terminal
    # A asked B for its descriptor only: no ticket was asked for or minted on A's behalf.
    assert verbs_b.asked == ["describe", "describe", "describe", "session-ticket"]
    assert verbs_b.minted == 1 and seen["own"].profile_id == expected_profile_id(home_b)
    assert str(home_b) not in str(refused) and str(home_a) not in str(refused)


@aio
async def test_ready_naming_another_or_no_profile_is_refused_before_sending(
    fake_clock: FakeClock,
) -> None:
    for ready_params in ({"profile_id": "other"}, {}):
        transport = ScriptedTransport()
        frame = {"protocol_version": 1, "theme": "auto", "heartbeat": True, **ready_params}
        transport.push({"jsonrpc": "2.0", "method": "gateway.ready", "params": frame})

        async def dial(t: ScriptedTransport = transport) -> Dialed:
            return Dialed(t, "p1")

        channel = Channel(dial, clock=fake_clock)
        with pytest.raises(DialRefused) as refused:
            await channel.start()
        assert refused.value.reason == "profile_mismatch" and refused.value.terminal
        assert transport.sent == []  # not even client.capabilities
        assert transport.closed == CLOSE_PROFILE_MISMATCH == PROFILE_MISMATCH.code
        await channel.close()


def test_profile_checks_fail_closed(tmp_path: Path) -> None:
    home = tmp_path / "h"
    home.mkdir()
    mine = expected_profile_id(home)
    assert expected_profile_id(home / ".") == mine  # the resolved home decides
    assert check_profile(mine, mine, where="describe") == mine
    for got in ("other", "", None, 7):
        with pytest.raises(ProfileMismatch) as caught:
            check_profile(mine, got, where="describe")
        assert caught.value.code == "profile_mismatch" and "describe" in str(caught.value)
    assert check_described(home, {"profile_id": mine})["profile_id"] == mine
    with pytest.raises(ProfileMismatch):
        check_described(home, {"instance_id": "i"})
    check_ready(None, {})  # nothing to compare with (an explicit URL without a grant)
    with pytest.raises(ProfileMismatch):
        check_ready(mine, {"profile_id": expected_profile_id(tmp_path)})


def test_symlinked_home_keeps_its_profile(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    assert expected_profile_id(link) == expected_profile_id(real)


def test_a_listener_of_another_user_gets_nothing(
    homes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    home, _ = homes
    verbs = Recording(home)
    seen: dict[str, Any] = {}

    async def body() -> None:
        seen["ours"] = await asyncio.to_thread(gateway_control.query, home, "describe")
        monkeypatch.setattr(profile_guard, "socket_peer_uid", lambda _sock: os.getuid() + 1)
        seen["foreign"] = await asyncio.to_thread(gateway_control.query, home, "describe")
        monkeypatch.setattr(profile_guard, "socket_peer_uid", lambda _sock: None)
        seen["unknown"] = await asyncio.to_thread(gateway_control.query, home, "describe")

    serve(home, verbs, body)
    assert seen["ours"].ok and seen["unknown"].ok  # an OS that reports nothing is allowed
    assert not seen["foreign"].ok and seen["foreign"].error == FOREIGN_LISTENER
    assert verbs.asked == ["describe", "describe"]  # the foreign listener was told nothing


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="SO_PEERCRED is Linux")
def test_peer_uid_is_read_from_the_socket() -> None:
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    with left, right:
        assert socket_peer_uid(left) == os.getuid()
        assert listener_is_ours(left)


# -- the daemon side -------------------------------------------------------------------------------


def test_bind_daemon_scope_keeps_one_profile_together(scope: Scope, tmp_path: Path) -> None:
    mine = expected_profile_id(scope.home)
    assert bind_daemon_scope(scope, mine) is scope
    with pytest.raises(ProfileMismatch):
        bind_daemon_scope(scope, expected_profile_id(tmp_path))
    other_home = tmp_path / "other"
    other_home.mkdir()
    foreign = scope.with_secrets(open_secret_store(other_home, "other", mode="file"))
    with pytest.raises(ScopeNotBound, match="secret store"):
        bind_daemon_scope(foreign, mine)
    from dataclasses import replace

    with pytest.raises(ScopeNotBound, match="routed"):
        bind_daemon_scope(replace(scope, routed=True), mine)
    assert bind_daemon_scope(scope.with_secrets(None), mine).secrets is None


@aio
async def test_ws_endpoint_refuses_a_scope_of_another_profile(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    host = await open_host(scope, store, tmp_path)
    try:
        with pytest.raises(ProfileMismatch):
            WsEndpoint(host, TicketBook.for_home(tmp_path), scope=scope)
    finally:
        await host.aclose()


@aio
async def test_every_daemon_rpc_runs_under_the_daemon_scope(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    host = await open_host(scope, store, tmp_path)
    book = TicketBook.for_home(scope.home)
    daemon_scope = scope.with_secrets(scope.secrets)  # a scope object of its own
    endpoint = WsEndpoint(host, book, scope=daemon_scope, strict=True)
    seen: list[tuple[str, Scope]] = []
    base = endpoint.table
    probed = MethodTable[Any]()
    for name in base.names():
        handler = base.get(name)
        assert handler is not None

        async def wrapped(conn: Any, params: Any, _h: Any = handler, _n: str = name) -> Any:
            seen.append((_n, current_scope()))
            return await _h(conn, params)

        probed.add(name, wrapped)
    endpoint.table = probed
    client = AsgiWsClient(
        endpoint.app,
        subprotocols=[SUBPROTOCOL, TICKET_PROTOCOL_PREFIX + book.mint(WS_INTERACTIVE).ticket],
    )
    try:
        assert await client.open() == SUBPROTOCOL
        assert (await client.recv())["method"] == "gateway.ready"
        await client.request("client.capabilities", {"features": [], "surface": "cli"})
        created = await client.request("session.create", {})
        sid = created["result"]["session_id"]
        described = await client.request("runtime.describe", {})
        assert described["result"]["profile_id"] == expected_profile_id(scope.home)
        await client.request("events.since", {"session_id": sid, "cursor": 0})
        await client.request("gateway.ping", {})
        await client.close()
    finally:
        await endpoint.stop()
        await host.aclose()
    names = [name for name, _ in seen]
    assert {"client.capabilities", "session.create", "runtime.describe", "events.since"} <= set(
        names
    )
    assert "gateway.ping" in names
    assert all(found is daemon_scope for _, found in seen)  # home + secrets + policy together
    assert all(found.secrets is scope.secrets for _, found in seen)
    assert endpoint.violations == []


# -- launch options -------------------------------------------------------------------------------

_PROBE = (
    "import json, os, sys\n"
    "open(sys.argv[1] + '.tmp', 'w').write(json.dumps(sorted(os.environ)))\n"
    "os.replace(sys.argv[1] + '.tmp', sys.argv[1])\n"
)


def test_launch_env_never_reaches_a_daemon_the_grant_starts(
    scope: Scope,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = tmp_path / "daemon-env.json"
    started: list[int] = []

    def ensure_and_start(found: Scope, *, mode: str, timeout: float) -> dict[str, Any]:
        """The daemon start ``ensure`` makes, with a probe in place of the daemon."""
        del mode
        run = gateway_ensure._Run(  # pyright: ignore[reportPrivateUsage]
            home=found.home,
            env=found.env.to_dict(),
            deadline=time.monotonic() + timeout,
            version="0",
            monotonic=time.monotonic,
            pause=lambda _seconds: None,  # _spawn never waits through it
            argv=[sys.executable, "-I", "-c", _PROBE, str(out)],
        )
        started.append(gateway_ensure._spawn(run))  # pyright: ignore[reportPrivateUsage]
        raise GrantRefused("starting", "the probe is not a daemon")

    monkeypatch.setattr(gateway_grant, "ensure_endpoint", ensure_and_start)
    launch = {"INDUS_YOLO_MODE": "1", "INDUS_TUI_QUERY": "hi", "INDUS_MODEL": "m"}
    loaded = scope.with_env(scope.env.with_overlay(launch))
    ran = run_cli(capsys, loaded, "gateway", "grant", "--json")
    assert ran.code == EXIT_RETRY and started
    asyncio.run(eventually(out.exists, limit=20.0))
    names = set(json.loads(out.read_text(encoding="utf-8")))
    assert not names & set(launch)  # not visible to the process started as the daemon
    assert "INDUS_HOME" in names
    asyncio.run(eventually(lambda: _gone(started[0]), limit=20.0))  # the probe exits by itself


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False
