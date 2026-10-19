"""The stdio server (P12-28): framing over a pipe pair, the claimed descriptors, spurious EOF."""

from __future__ import annotations

import asyncio
import io
import json
import os
import socket
import struct
import subprocess
import sys
import textwrap
import threading
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.protocol.errors import (
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    PROVIDER_UNAVAILABLE,
    UNKNOWN_PARAM_KEY,
)
from indus.protocol.methods import PROTOCOL_VERSION
from indus.service.session_service import SessionService
from indus.tui_bridge.bridge import Bridge
from indus.tui_bridge.stdio_server import FrameWriter, LineReader, StdioServer
from tests.fakes.agent import eventually

ROOT = Path(__file__).resolve().parents[3]


def no_service(bridge: Bridge) -> SessionService:
    raise RuntimeError("no provider in this test")


def serve(scope: Scope, payload: bytes) -> list[dict[str, Any]]:
    """Feed ``payload`` through a pipe, close it, and return every frame written."""
    read_fd, write_fd = os.pipe()
    sink = io.BytesIO()
    writer = FrameWriter(sink)

    async def main() -> int:
        bridge = Bridge(scope, writer, make_service=no_service, strict=True)
        return await StdioServer(bridge, LineReader(read_fd), writer).serve()

    def feed() -> None:
        os.write(write_fd, payload)
        os.close(write_fd)

    feeder = threading.Thread(target=feed)
    feeder.start()
    try:
        code = asyncio.run(main())
    finally:
        feeder.join()
        os.close(read_fd)
    assert code == 0
    return [json.loads(line) for line in sink.getvalue().decode("utf-8").splitlines()]


def request(rid: int, method: str, **params: Any) -> bytes:
    return (
        json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\n"
    ).encode()


def by_id(frames: list[dict[str, Any]]) -> dict[Any, dict[str, Any]]:
    return {f["id"]: f for f in frames if "id" in f}


def test_ready_frame_comes_first(scope: Scope) -> None:
    frames = serve(scope, b"")
    assert frames == [
        {
            "jsonrpc": "2.0",
            "method": "gateway.ready",
            "params": {"theme": "auto", "heartbeat": False, "protocol_version": PROTOCOL_VERSION},
        }
    ]


def test_garbage_line_gets_parse_error_then_next_request_is_served(scope: Scope) -> None:
    payload = b"{not json\n\n   \n" + request(2, "config.get", key="theme")
    frames = serve(scope, payload)
    assert frames[1] == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {
            "code": PARSE_ERROR.code,
            "message": "parse error",
            "data": {"name": "PARSE_ERROR"},
        },
    }
    assert by_id(frames)[2]["result"] == {"key": "theme", "value": "auto"}
    assert len(frames) == 3  # blank lines got no answer


def test_frames_that_are_not_requests(scope: Scope) -> None:
    payload = (
        b"[1, 2]\n"
        b'{"jsonrpc": "2.0", "id": 5, "params": {}}\n'
        b'{"jsonrpc": "2.0", "method": "client.capabilities"}\n'
        b'{"jsonrpc": "2.0", "id": 6, "result": {}}\n' + request(7, "config.get", key="mtime")
    )
    frames = serve(scope, payload)
    errors = [f for f in frames if "error" in f]
    assert [e["id"] for e in errors] == [None, 5]
    assert all(e["error"]["code"] == INVALID_REQUEST.code for e in errors)
    assert by_id(frames)[7]["result"]["key"] == "mtime"


def test_unknown_method_and_unknown_key(scope: Scope) -> None:
    payload = request(1, "session.explode") + request(2, "config.get", key="x", colour="red")
    frames = by_id(serve(scope, payload))
    assert frames[1]["error"]["code"] == METHOD_NOT_FOUND.code
    assert frames[2]["error"]["code"] == UNKNOWN_PARAM_KEY.code
    assert frames[2]["error"]["data"]["path"] == "colour"


def test_service_failure_is_answered_and_the_loop_goes_on(scope: Scope) -> None:
    payload = request(1, "session.create") + request(2, "config.get", key="theme")
    frames = by_id(serve(scope, payload))
    assert frames[1]["error"]["code"] == PROVIDER_UNAVAILABLE.code
    assert "result" in frames[2]


def test_nonblocking_flip_is_recovered() -> None:
    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)  # what a careless child does to a shared descriptor

    async def main() -> bytes | None:
        reader = LineReader(read_fd)
        reader.start(asyncio.get_running_loop())
        await eventually(lambda: reader.recovered >= 1)
        os.write(write_fd, b'{"late": true}\n')
        line = await asyncio.wait_for(reader.readline(), timeout=5)
        assert reader.recovered >= 1
        assert os.get_blocking(read_fd)
        os.close(write_fd)
        assert await asyncio.wait_for(reader.readline(), timeout=5) is None
        return line

    try:
        assert asyncio.run(main()) == b'{"late": true}'
    finally:
        os.close(read_fd)


@pytest.mark.skipif(not hasattr(socket, "socketpair"), reason="needs socket pairs")
def test_receive_timeout_on_a_socket_pair_is_recovered() -> None:
    ours, theirs = socket.socketpair()
    ours.setsockopt(socket.SOL_SOCKET, socket.SO_RCVTIMEO, struct.pack("ll", 0, 50_000))

    async def main() -> bytes | None:
        reader = LineReader(ours.fileno())
        reader.start(asyncio.get_running_loop())
        await eventually(lambda: reader.recovered >= 1)  # the timeout fired once, then cleared
        theirs.sendall(b"after the timeout\n")
        line = await asyncio.wait_for(reader.readline(), timeout=5)
        assert reader.recovered == 1
        return line

    try:
        assert asyncio.run(main()) == b"after the timeout"
    finally:
        theirs.close()
        ours.close()


def test_recovery_budget_turns_into_eof() -> None:
    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)

    async def main() -> bytes | None:
        reader = LineReader(read_fd, max_recoveries=0)
        reader.start(asyncio.get_running_loop())
        return await asyncio.wait_for(reader.readline(), timeout=5)

    try:
        assert asyncio.run(main()) is None
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_writer_reports_a_broken_pipe() -> None:
    class Broken(io.RawIOBase):
        def write(self, b: Any) -> int:
            raise BrokenPipeError

    writer = FrameWriter(Broken())  # pyright: ignore[reportArgumentType]
    assert writer.notify("x", {}) is False and writer.broken
    assert writer.write({"a": 1}) is False


@pytest.mark.skipif(os.name != "posix", reason="descriptor claims are POSIX here")
def test_claimed_stdio_keeps_noise_and_children_off_the_channel(tmp_path: Path) -> None:
    script = tmp_path / "claim.py"
    script.write_text(
        textwrap.dedent(
            """
            import os, subprocess, sys
            from indus.tui_bridge.stdio_server import FrameWriter, claim_stdio
            READ_STDIN = "import sys; sys.stderr.write('child read ' + repr(sys.stdin.read()))"
            rpc_in, rpc_out = claim_stdio()
            print("stray print")
            os.system("echo child-noise")
            child = subprocess.run(
                [sys.executable, "-c", READ_STDIN]
            )
            data = os.read(rpc_in, 100)
            FrameWriter(rpc_out).notify("seen", {"data": data.decode()})
            """
        ),
        encoding="utf-8",
    )
    done = subprocess.run(
        [sys.executable, str(script)],
        input=b"request bytes\n",
        capture_output=True,
        cwd=ROOT,
        timeout=60,
        check=True,
    )
    frames = [json.loads(line) for line in done.stdout.decode().splitlines()]
    assert frames == [{"jsonrpc": "2.0", "method": "seen", "params": {"data": "request bytes\n"}}]
    noise = done.stderr.decode()
    assert "stray print" in noise and "child-noise" in noise and "child read ''" in noise
