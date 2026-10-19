"""P15-27: the control socket — wire, limits, permissions, paths (server and client)."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import stat
import tempfile
import threading
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from indus.gateway.control_socket import ControlServer, ControlSocketError, supervisor_kind
from indus.service import gateway_control as wire


@pytest.fixture
def short_home() -> Iterator[Path]:
    """A home whose socket path fits (pytest's tmp paths can be too long for sun_path)."""
    home = Path(tempfile.mkdtemp(prefix="igw-", dir="/tmp"))
    try:
        yield home
    finally:
        shutil.rmtree(home, ignore_errors=True)


def run[T](coro: Awaitable[T]) -> T:
    async def main() -> T:
        return await coro

    return asyncio.run(main())


async def ask(home: Path, verb: str, **kw: Any) -> wire.ControlReply:
    return await asyncio.to_thread(wire.query, home, verb, **kw)


def raw_exchange(path: Path, payload: bytes, timeout: float = 3.0) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(path))
        sock.sendall(payload)
        data = b""
        while b"\n" not in data:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    return json.loads(data.partition(b"\n")[0])


def served(home: Path, body: Callable[[ControlServer], Awaitable[None]], **kw: Any) -> None:
    async def main() -> None:
        server = ControlServer(home, status=lambda: {"state": "running"}, env={}, **kw)
        await server.start()
        try:
            await body(server)
        finally:
            await server.stop()

    asyncio.run(main())


# -- wire -----------------------------------------------------------------------------------------


def line(**fields: Any) -> bytes:
    return json.dumps(fields).encode()


@pytest.mark.parametrize(
    ("request_", "code"),
    [
        (line(protocol=1, verb="identify", id=7), None),
        (line(protocol=1, verb="nope", id=7), "unknown_verb"),
        (line(protocol=1, verb="drain", id=7), "unsupported"),
        (line(protocol=1, verb="identify", id=7, extra=1), "bad_request"),
        (line(protocol=2, verb="identify", id=7), "bad_request"),
        (line(protocol=True, verb="identify", id=7), "bad_request"),
        (line(protocol=1, verb="", id=7), "bad_request"),
        (line(protocol=1, verb=5, id=7), "bad_request"),
        (line(protocol=1, verb="identify", id=7, params=[1]), "bad_request"),
        (line(protocol=1, verb="identify", id="x" * 129), "bad_request"),
        (b"not json", "bad_request"),
        (b"[1, 2]", "bad_request"),
        (b"\xff\xfe", "bad_request"),
    ],
)
def test_wire_table(request_: bytes, code: str | None) -> None:
    server = ControlServer(Path("/nowhere"), identity=lambda: {"pid": 1}, env={})
    answer = json.loads(run(server.handle_line(request_)))
    assert answer["protocol"] == 1
    if code is None:
        assert answer == {"ok": True, "protocol": 1, "result": {"pid": 1}, "id": 7}
    else:
        assert answer["ok"] is False and answer["error"] == code
    if code in ("unknown_verb", "unsupported"):
        assert answer["verbs"] == ["identify", "status"] and answer["id"] == 7
    if code == "bad_request" and b"extra" in request_:
        assert answer["id"] == 7 and "extra" in answer["detail"]


def test_status_answers_with_the_answering_pid() -> None:
    server = ControlServer(Path("/x"), status=lambda: {"state": "draining"}, env={})
    answer = json.loads(run(server.handle_line(line(protocol=1, verb="status", id=1))))
    result = answer["result"]
    assert result.pop("answered_at") > 0
    assert result == {"state": "draining", "answering_pid": os.getpid()}


def test_handler_failures_are_structured() -> None:
    server = ControlServer(Path("/x"), env={})

    def broken(_params: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("secret detail")

    def slow(_params: dict[str, Any]) -> dict[str, Any]:
        threading.Event().wait(1.0)
        return {}

    async def slow_async(_params: dict[str, Any]) -> dict[str, Any]:
        await asyncio.Event().wait()
        return {}

    def not_a_mapping(_params: dict[str, Any]) -> Any:
        return [1]

    def not_json(_params: dict[str, Any]) -> dict[str, Any]:
        return {"x": object()}

    server.register("drain", broken)
    server.register("slow", slow, timeout=0.1)
    server.register("slow-async", slow_async, timeout=0.1)
    server.register("list", not_a_mapping)
    server.register("obj", not_json)

    def ask_line(verb: str) -> dict[str, Any]:
        return json.loads(run(server.handle_line(line(protocol=1, verb=verb, id=1))))

    failed = ask_line("drain")
    assert failed["error"] == "failed" and "secret" not in json.dumps(failed)
    assert ask_line("slow")["error"] == "timeout"
    assert ask_line("slow-async")["error"] == "timeout"
    assert ask_line("list")["error"] == "failed"
    assert ask_line("obj")["error"] == "failed"


def test_params_reach_the_handler() -> None:
    server = ControlServer(Path("/x"), env={})
    seen: list[dict[str, Any]] = []

    async def drain(params: dict[str, Any]) -> dict[str, Any]:
        seen.append(params)
        return {"draining": True}

    server.register("drain", drain)
    answer = json.loads(
        run(server.handle_line(line(protocol=1, verb="drain", id=2, params={"timeout": 5})))
    )
    assert answer["result"] == {"draining": True} and seen == [{"timeout": 5}]
    run(server.handle_line(line(protocol=1, verb="drain", id=3)))
    assert seen[-1] == {}


def test_oversized_result_is_response_too_large(short_home: Path) -> None:
    async def body(server: ControlServer) -> None:
        server.register("big", lambda _p: {"blob": "a" * (wire.MAX_RESPONSE_BYTES + 1)})
        reply = await ask(short_home, "big")
        assert not reply.ok and reply.error == "response_too_large" and reply.answered
        small = await ask(short_home, "identify")
        assert small.ok

    served(short_home, body)


# -- the real socket ------------------------------------------------------------------------------


def test_identify_round_trip_and_owner_only_socket(short_home: Path) -> None:
    async def body(server: ControlServer) -> None:
        path = server.path
        assert path is not None and path == short_home / wire.SOCKET_FILE
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600
        found = await asyncio.to_thread(wire.identify, short_home)
        assert found is not None
        assert found["pid"] == os.getpid() and found["home"] == str(short_home)
        assert found["profile"] == "default" and found["supervisor"] == "manual"
        assert isinstance(found["code_version"], str)
        status = await ask(short_home, "status")
        assert status.ok and status.result is not None
        assert status.result["state"] == "running"
        assert status.result["answering_pid"] == os.getpid()

    served(short_home, body)
    assert not (short_home / wire.SOCKET_FILE).exists()


def test_request_over_64_kib_is_refused(short_home: Path) -> None:
    async def body(server: ControlServer) -> None:
        assert server.path is not None
        big = json.dumps({"protocol": 1, "verb": "identify", "id": "x" * (65 * 1024)})
        answer = await asyncio.to_thread(raw_exchange, server.path, big.encode() + b"\n")
        assert answer == {"ok": False, "protocol": 1, "error": "request_too_large"}
        no_newline = await asyncio.to_thread(
            raw_exchange, server.path, b'{"protocol": 1, "verb": "identify"}', 3.0
        )
        assert no_newline["error"] in ("timeout", "bad_request")

    served(short_home, body, read_timeout=0.3)


def test_a_half_closed_request_without_newline_is_bad(short_home: Path) -> None:
    async def body(server: ControlServer) -> None:
        assert server.path is not None

        def send() -> dict[str, Any]:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(3)
                sock.connect(str(server.path))
                sock.sendall(b'{"protocol": 1}')
                sock.shutdown(socket.SHUT_WR)
                return json.loads(sock.recv(65536).partition(b"\n")[0])

        assert (await asyncio.to_thread(send))["error"] == "bad_request"

    served(short_home, body)


def test_identify_answers_while_a_turn_and_a_slow_verb_run(short_home: Path) -> None:
    async def body(server: ControlServer) -> None:
        release, entered = asyncio.Event(), asyncio.Event()

        async def slow_drain(_params: dict[str, Any]) -> dict[str, Any]:
            entered.set()
            await release.wait()
            return {"drained": True}

        server.register("drain", slow_drain, timeout=10)
        turn = asyncio.create_task(release.wait())  # a "turn" holding on
        pending = asyncio.create_task(ask(short_home, "drain"))
        await entered.wait()
        started = asyncio.get_running_loop().time()
        found = await asyncio.to_thread(wire.identify, short_home, timeout=2.0)
        assert found is not None
        assert asyncio.get_running_loop().time() - started < 2.0
        release.set()
        assert (await pending).result == {"drained": True}
        await turn

    served(short_home, body)


def test_another_users_peer_is_forbidden(short_home: Path) -> None:
    async def body(_server: ControlServer) -> None:
        reply = await ask(short_home, "identify")
        assert not reply.ok and reply.error == "forbidden"

    served(short_home, body, peer=lambda _sock: os.getuid() + 1)


def test_the_real_peer_check_sees_us(short_home: Path) -> None:
    from indus.gateway.control_socket import peer_uid

    seen: list[int | None] = []

    def spy(sock: socket.socket) -> int | None:
        uid = peer_uid(sock)
        seen.append(uid)
        return uid

    async def body(_server: ControlServer) -> None:
        assert (await ask(short_home, "identify")).ok

    served(short_home, body, peer=spy)
    if os.uname().sysname in ("Linux", "Darwin"):
        assert seen == [os.getuid()]


def test_stale_socket_is_replaced(short_home: Path) -> None:
    path = short_home / wire.SOCKET_FILE
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as leftover:
        leftover.bind(str(path))  # bound, never listening: a crash leftover
    assert stat.S_ISSOCK(path.lstat().st_mode)
    assert wire.query(short_home, "identify").error == "unreachable"

    async def body(_server: ControlServer) -> None:
        assert (await ask(short_home, "identify")).ok

    served(short_home, body)


def test_a_live_socket_is_never_taken_over(short_home: Path) -> None:
    async def body(_server: ControlServer) -> None:
        second = ControlServer(short_home, env={})
        with pytest.raises(ControlSocketError, match="already answers"):
            await second.start()
        assert (await ask(short_home, "identify")).ok

    served(short_home, body)


@pytest.mark.parametrize("kind", ["symlink", "file"])
def test_symlink_or_foreign_file_refused(short_home: Path, kind: str) -> None:
    path = short_home / wire.SOCKET_FILE
    if kind == "symlink":
        path.symlink_to(short_home / "elsewhere.sock")
    else:
        path.write_text("not a socket")

    async def main() -> None:
        with pytest.raises(ControlSocketError):
            await ControlServer(short_home, env={}).start()

    asyncio.run(main())
    assert path.is_symlink() if kind == "symlink" else path.read_text() == "not a socket"


def test_stop_removes_only_its_own_files(short_home: Path) -> None:
    path = short_home / wire.SOCKET_FILE

    async def body(server: ControlServer) -> None:
        path.unlink()
        path.write_text("someone else's")  # replaced behind our back

    served(short_home, body)
    assert path.read_text() == "someone else's"


def test_long_home_uses_a_pointer_file(tmp_path: Path) -> None:
    home = tmp_path / ("deep-" * 20) / "home"
    home.mkdir(parents=True)
    assert not wire.fits(home / wire.SOCKET_FILE)
    holder: dict[str, Path] = {}

    async def body(server: ControlServer) -> None:
        assert server.path is not None and wire.fits(server.path)
        holder["sock"] = server.path
        pointer = wire.pointer_path(home)
        assert stat.S_IMODE(pointer.stat().st_mode) == 0o600
        assert pointer.read_text() == str(server.path)
        assert stat.S_IMODE(server.path.parent.stat().st_mode) == 0o700
        assert wire.socket_path_for(home) == server.path
        found = await asyncio.to_thread(wire.identify, home)
        assert found is not None and found["home"] == str(home)
        pointer.chmod(0o644)  # a pointer others could have written is not followed
        assert (await ask(home, "identify")).error == "no_socket"
        pointer.chmod(0o600)

    served(home, body)
    assert not wire.pointer_path(home).exists()
    assert not holder["sock"].parent.exists()


def test_pointer_to_a_shared_directory_is_not_followed(tmp_path: Path) -> None:
    home = tmp_path / "owner-home"
    home.mkdir()
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o777)
    shared.chmod(0o777)
    target = shared / "gw.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(str(target))
        except OSError:
            pytest.skip("path too long for a socket here")
    pointer = wire.pointer_path(home)
    pointer.write_text(str(target))
    pointer.chmod(0o600)
    assert wire.socket_path_for(home) is None


# -- client safety --------------------------------------------------------------------------------


def test_client_without_a_server(short_home: Path) -> None:
    reply = wire.query(short_home, "identify")
    assert reply == wire.ControlReply(False, error="no_socket") and not reply.answered
    assert wire.identify(short_home) is None
    assert wire.query(short_home / "missing", "identify").error == "unsafe_home"


def test_world_writable_home_is_refused(short_home: Path) -> None:
    short_home.chmod(0o777)
    assert not wire.home_is_safe(short_home)
    assert wire.query(short_home, "identify").error == "unsafe_home"


def test_group_writable_home_only_with_a_private_group(
    short_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    short_home.chmod(0o770)
    monkeypatch.setattr(wire, "_private_group", lambda _gid: False)
    assert not wire.home_is_safe(short_home)
    monkeypatch.setattr(wire, "_private_group", lambda _gid: True)
    assert wire.home_is_safe(short_home)
    short_home.chmod(0o700)
    monkeypatch.setattr(wire, "_private_group", lambda _gid: False)
    assert wire.home_is_safe(short_home)


def test_request_built_by_the_client_is_bounded(short_home: Path) -> None:
    reply = wire.query(short_home, "identify", params={"x": "a" * wire.MAX_REQUEST_BYTES})
    assert reply.error in ("no_socket", "request_too_large")
    (short_home / wire.SOCKET_FILE).touch()  # not a socket: ignored
    assert wire.socket_path_for(short_home) is None


@pytest.mark.parametrize(
    ("env", "kind"),
    [
        ({"INVOCATION_ID": "abc"}, "systemd"),
        ({"XPC_SERVICE_NAME": "ai.indus.gateway.1234abcd"}, "launchd"),
        ({"XPC_SERVICE_NAME": "com.apple.Terminal"}, "manual"),
        ({"INDUS_GATEWAY_SUPERVISED": "1"}, "external"),
        ({"INDUS_GATEWAY_SUPERVISED": "0"}, "manual"),
        ({}, "manual"),
    ],
)
def test_supervisor_kind(env: dict[str, str], kind: str) -> None:
    assert supervisor_kind(env) == kind


def test_windows_is_refused(short_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import indus.gateway.control_socket as module

    monkeypatch.setattr(module.sys, "platform", "win32")

    async def main() -> None:
        with pytest.raises(ControlSocketError, match="POSIX"):
            await ControlServer(short_home, env={}).start()

    asyncio.run(main())
