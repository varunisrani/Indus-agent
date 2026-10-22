"""The execute_code request pipeline and socket server (P10-08)."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
import tempfile
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.clock import SystemClock
from indus.tools.builtin.code.rpc import (
    NOT_RUNNING,
    RpcPipeline,
    SocketRpcServer,
    error_of,
    token_ok,
)

TOKEN = "t" * 43


def pipeline(
    max_calls: int = 50, allowed: frozenset[str] | None = None
) -> tuple[RpcPipeline, list[Any]]:
    seen: list[Any] = []

    async def invoke(name: str, args: dict[str, Any]) -> str:
        seen.append((name, args))
        if name == "write_file":
            return json.dumps({"error": "refused: read it first"})
        if name == "terminal" and args.get("command") == "boom":
            raise RuntimeError("exploded")
        return json.dumps({"ok": name})

    made = RpcPipeline(
        token=TOKEN,
        allowed=allowed
        if allowed is not None
        else frozenset({"read_file", "write_file", "terminal"}),
        max_calls=max_calls,
        invoke=invoke,
        clock=SystemClock(),
    )
    return made, seen


def ask(p: RpcPipeline, **body: Any) -> dict[str, str]:
    return asyncio.run(p.handle({"token": TOKEN, **body}))


def test_token_check_is_strict() -> None:
    assert token_ok(TOKEN, TOKEN)
    assert not token_ok("x" + TOKEN[1:], TOKEN)
    assert not token_ok(None, TOKEN)
    assert not token_ok("é", TOKEN)  # non-ASCII text never raises
    assert not token_ok("", "")  # an empty server token refuses everything


def test_wrong_token_is_refused_and_free() -> None:
    p, seen = pipeline()
    answer = asyncio.run(p.handle({"token": "nope", "tool": "read_file", "args": {}}))
    assert "Unauthorised" in answer["refused"]
    assert seen == [] and p.calls_made == 0


def test_unknown_tool_lists_what_is_offered() -> None:
    p, seen = pipeline()
    answer = ask(p, tool="delegate_task", args={})
    assert "cannot be called" in answer["refused"]
    assert "read_file, terminal, write_file" in answer["refused"]
    assert seen == [] and p.calls_made == 0


def test_budget_counts_only_dispatched_calls() -> None:
    p, seen = pipeline(max_calls=2)
    ask(p, tool="nothing", args={})
    assert "result" in ask(p, tool="read_file", args={"path": "a"})
    assert "result" in ask(p, tool="read_file", args={"path": "b"})
    third = ask(p, tool="read_file", args={"path": "c"})
    assert "already made its 2 tool calls" in third["refused"]
    assert len(seen) == 2 and p.calls_made == 2


@pytest.mark.parametrize(
    "flag",
    [
        "background",
        "pty",
        "notify",
        "watch_patterns",
        "heartbeat",
        "persist_on_release",
        "notify_on_complete",
    ],
)
def test_terminal_is_foreground_only(flag: str) -> None:
    p, seen = pipeline()
    answer = ask(p, tool="terminal", args={"command": "ls", flag: True})
    assert "foreground only" in answer["refused"] and flag in answer["refused"]
    assert seen == []


def test_terminal_false_flags_are_dropped_not_refused() -> None:
    p, seen = pipeline()
    assert "result" in ask(p, tool="terminal", args={"command": "ls", "background": False})
    assert seen == [("terminal", {"command": "ls"})]


def test_arguments_must_be_an_object() -> None:
    p, _ = pipeline()
    assert "JSON object" in ask(p, tool="read_file", args=["x"])["refused"]
    assert "JSON object" in asyncio.run(p.handle("not a dict"))["refused"]


def test_tool_errors_and_exceptions_are_logged() -> None:
    p, _ = pipeline()
    ask(p, tool="write_file", args={"path": "a", "content": "b"})
    crashed = ask(p, tool="terminal", args={"command": "boom"})
    assert "exploded" in json.loads(crashed["result"])["error"]
    assert p.tool_errors(5) == [
        {"tool": "write_file", "error": "refused: read it first"},
        {"tool": "terminal", "error": "terminal failed: RuntimeError: exploded"},
    ]
    assert p.calls_made == 2


def test_error_of_reads_only_json_error_objects() -> None:
    assert error_of('{"error": "x"}') == "x"
    assert error_of('{"ok": 1}') is None
    assert error_of("plain error text") is None
    assert error_of('["error"]') is None


# -- the socket server -----------------------------------------------------------------------------


def _send(endpoint: str, lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if endpoint.startswith("tcp://"):
        host, _, port = endpoint[len("tcp://") :].rpartition(":")
        conn = socket.create_connection((host, int(port)))
    else:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.connect(endpoint)
    reader = conn.makefile("rb")
    answers: list[dict[str, Any]] = []
    with conn, reader:
        for line in lines:
            conn.sendall((json.dumps(line) + "\n").encode())
            answers.append(json.loads(reader.readline()))
    return answers


async def _serve_and_ask(
    server: SocketRpcServer, p: RpcPipeline, lines: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    server.attach(p, asyncio.get_running_loop())
    try:
        return await asyncio.to_thread(_send, server.endpoint, lines)
    finally:
        server.detach()


def test_unix_socket_is_owner_only_and_removed_on_close(tmp_path: Path) -> None:
    server = SocketRpcServer(tmp_path)
    try:
        assert server.socket_path is not None
        mode = stat.S_IMODE(os.stat(server.socket_path).st_mode)
        assert mode == 0o600
        p, seen = pipeline()
        answers = asyncio.run(
            _serve_and_ask(
                server,
                p,
                [
                    {"token": TOKEN, "tool": "read_file", "args": {"path": "x"}},
                    {"token": "bad", "tool": "read_file"},
                ],
            )
        )
        assert json.loads(answers[0]["result"]) == {"ok": "read_file"}
        assert "Unauthorised" in answers[1]["refused"]
        assert seen == [("read_file", {"path": "x"})]
    finally:
        server.close()
    assert not os.path.exists(server.socket_path)
    server.close()  # twice is fine


def test_requests_between_calls_are_refused(tmp_path: Path) -> None:
    server = SocketRpcServer(tmp_path)
    try:
        answers = _send(server.endpoint, [{"token": TOKEN, "tool": "read_file", "args": {}}])
        assert answers == [{"refused": NOT_RUNNING}]
    finally:
        server.close()


def test_loopback_tcp_transport(tmp_path: Path) -> None:
    server = SocketRpcServer(tmp_path, use_tcp=True)
    try:
        assert server.endpoint.startswith("tcp://127.0.0.1:")
        p, _ = pipeline()
        answers = asyncio.run(
            _serve_and_ask(
                server, p, [{"token": TOKEN, "tool": "terminal", "args": {"command": "x"}}]
            )
        )
        assert "result" in answers[0]
    finally:
        server.close()


def test_long_socket_path_moves_to_a_short_private_dir(
    tmp_path: Path, short_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The product's short dir goes under this test's own short folder (not ``tmp_path``, which a
    # long ``--basetemp`` makes too long for any socket: W46 RS11).
    monkeypatch.setattr(tempfile, "tempdir", str(short_tmp))
    deep = tmp_path / ("d" * 60) / ("e" * 60)
    deep.mkdir(parents=True)
    server = SocketRpcServer(deep)
    try:
        assert server.socket_path is not None and len(server.socket_path) <= 100
        short_dir = os.path.dirname(server.socket_path)
        assert stat.S_IMODE(os.stat(short_dir).st_mode) == 0o700
    finally:
        server.close()
    assert not os.path.exists(short_dir)
