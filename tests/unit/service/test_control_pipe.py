"""The control channel on Windows: a named pipe (docs/notes/P25/windows-gateway.md).

The name rules and the pointer reading run everywhere; the real pipe cases need Windows.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from indus.gateway.control_socket import ControlServer, ControlSocketError
from indus.service import control_pipe
from indus.service import gateway_control as wire

windows_only = pytest.mark.platforms("windows")  # named pipes are Windows


# -- names and the pointer (every OS) --------------------------------------------------------


def test_a_new_name_is_well_formed_and_fresh() -> None:
    one, two = control_pipe.new_pipe_name(), control_pipe.new_pipe_name()
    assert control_pipe.is_pipe_name(one) and control_pipe.is_pipe_name(two)
    assert one != two
    assert one.startswith("\\\\.\\pipe\\indus-gw-")


@pytest.mark.parametrize(
    "text",
    [
        "",
        "C:\\Users\\me\\.indus\\gateway.sock",
        "\\\\.\\pipe\\indus-gw-",
        "\\\\.\\pipe\\indus-gw-" + "A" * 32,
        "\\\\.\\pipe\\indus-gw-" + "a" * 31,
        "\\\\.\\pipe\\indus-gw-" + "a" * 32 + "x",
        "\\\\.\\pipe\\other-" + "a" * 32,
        "//./pipe/indus-gw-" + "a" * 32,
        "\\\\server\\pipe\\indus-gw-" + "a" * 32,
    ],
)
def test_anything_else_is_not_a_pipe_name(text: str) -> None:
    assert not control_pipe.is_pipe_name(text)


def test_the_pointer_must_be_a_file_naming_a_pipe(tmp_path: Path) -> None:
    pointer = wire.pointer_path(tmp_path)
    assert wire.pipe_name_for(tmp_path) is None
    name = control_pipe.new_pipe_name()
    pointer.write_text(name + "\n", encoding="utf-8")
    assert wire.pipe_name_for(tmp_path) == name
    pointer.write_text("C:\\Windows\\System32\\cmd.exe", encoding="utf-8")
    assert wire.pipe_name_for(tmp_path) is None
    pointer.unlink()
    pointer.mkdir()
    assert wire.pipe_name_for(tmp_path) is None


# -- the real pipe (Windows) -----------------------------------------------------------------


def served(home: Path, body: Callable[[ControlServer], Awaitable[None]], **kw: Any) -> None:
    async def main() -> None:
        server = ControlServer(home, status=lambda: {"state": "running"}, env={}, **kw)
        await server.start()
        try:
            await body(server)
        finally:
            await server.stop()

    asyncio.run(main())


async def ask(home: Path, verb: str, **kw: Any) -> wire.ControlReply:
    return await asyncio.to_thread(wire.query, home, verb, **kw)


@windows_only
def test_identify_and_an_owner_only_verb_over_the_pipe(tmp_path: Path) -> None:
    async def main() -> None:
        server = ControlServer(tmp_path, env={})
        server.register("secret", lambda params: {"got": params}, owner_only=True)
        await server.start()
        try:
            name = wire.pipe_name_for(tmp_path)
            assert name is not None and server.path == wire.pointer_path(tmp_path)
            found = await asyncio.to_thread(wire.identify, tmp_path)
            assert found is not None and found["pid"] > 0
            # owner-only: the server reads our SID from the pipe and lets us in
            reply = await ask(tmp_path, "secret", params={"a": 1})
            assert reply.ok and reply.result == {"got": {"a": 1}}
            assert (await ask(tmp_path, "nope")).error == "unknown_verb"
            oversized = {"x": "y" * (wire.MAX_REQUEST_BYTES + 1)}
            assert (await ask(tmp_path, "secret", params=oversized)).error == "request_too_large"
        finally:
            await server.stop()

    asyncio.run(main())
    assert not wire.pointer_path(tmp_path).exists()
    assert wire.identify(tmp_path) is None


@windows_only
def test_a_stale_pointer_is_no_socket_and_is_replaced(tmp_path: Path) -> None:
    stale = control_pipe.new_pipe_name()
    wire.pointer_path(tmp_path).write_text(stale, encoding="utf-8")
    assert wire.query(tmp_path, "identify").error == "no_socket"

    async def body(_server: ControlServer) -> None:
        name = wire.pipe_name_for(tmp_path)
        assert name is not None and name != stale
        assert (await ask(tmp_path, "identify")).ok

    served(tmp_path, body)


@windows_only
def test_a_live_pipe_is_never_taken_over(tmp_path: Path) -> None:
    async def body(_server: ControlServer) -> None:
        with pytest.raises(ControlSocketError, match="already answers"):
            await ControlServer(tmp_path, env={}).start()
        assert (await ask(tmp_path, "identify")).ok  # the first one still answers

    served(tmp_path, body)


@windows_only
def test_a_slow_answer_times_out_on_the_client(tmp_path: Path) -> None:
    async def body(server: ControlServer) -> None:
        release = asyncio.Event()

        async def slow(_params: dict[str, Any]) -> dict[str, Any]:
            await release.wait()
            return {}

        server.register("slow", slow, timeout=5.0)
        started = time.monotonic()
        reply = await ask(tmp_path, "slow", timeout=0.3)
        release.set()
        assert reply.error == "timeout"
        assert time.monotonic() - started < 0.9

    served(tmp_path, body)
