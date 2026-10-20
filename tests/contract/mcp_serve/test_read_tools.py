"""P19-14: ``indus mcp serve`` — the server, its tool list and the read tools.

Every tool call goes through the MCP SDK's in-memory client against the real server object; the
store is written with the real state API, the way the gateway writes it.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from indus.cli import app as app_mod
from indus.cli.args import CliState
from indus.foundation.scope import Scope
from indus.mcp_serve.read_tools import coerce_int
from indus.mcp_serve.server import (
    APPROVAL_TOOLS,
    INSTRUCTIONS,
    SERVER_NAME,
    TOOL_ORDER,
    open_bridge,
)
from indus.mcp_serve.store import StoreReader
from indus.state import connection
from indus.state import messages as message_rows
from tests.contract.mcp_serve.conftest import World, call, connected, tool_names
from tests.fakes.clock import FakeClock
from tests.unit.backends._aio import aio

REPO = Path(__file__).resolve().parents[3]


# -- the server ---------------------------------------------------------------------------------


@aio
async def test_tool_list_order_is_exact_and_approval_tools_are_absent_without_a_daemon(
    scope: Scope,
) -> None:
    bridge = await open_bridge(scope, start_events=False)
    try:
        async with connected(bridge) as client:
            names = await tool_names(client)
            assert names == list(TOOL_ORDER)
            assert not set(APPROVAL_TOOLS) & set(names)
            for tool in (await client.list_tools()).tools:
                assert tool.description and tool.description.strip(), tool.name
            assert client.instructions == INSTRUCTIONS
            info = client.server_info
            assert info is None or info.name == SERVER_NAME
    finally:
        await bridge.aclose()


def test_integers_are_clamped_and_non_numbers_take_the_default() -> None:
    cases: list[tuple[object, int]] = [
        (999, 200),
        (0, 1),
        (-5, 1),
        ("x", 50),
        (None, 50),
        (True, 50),
        ("7", 7),
        (" 12 ", 12),
        ("3.9", 3),
        (2.5, 2),
        (float("nan"), 50),
        ("inf", 50),
    ]
    for value, expected in cases:
        assert coerce_int(value, default=50, low=1, high=200) == expected, value


# -- conversations --------------------------------------------------------------------------------


@aio
async def test_conversations_are_listed_newest_first_with_filters(
    scope: Scope, world: World, fake_clock: FakeClock
) -> None:
    world.conversation("agent:main:telegram:dm:1", display="Alice", user_name="alice")
    fake_clock.advance(10)
    world.conversation(
        "agent:main:discord:group:2", platform="discord", chat_type="group", chat_name="General"
    )
    fake_clock.advance(10)
    world.conversation("agent:main:Telegram:dm:3", platform="Telegram", display="Bob")
    bridge = await open_bridge(scope, start_events=False)
    try:
        async with connected(bridge) as client:
            listed = await call(client, "conversations_list")
            keys = [c["session_key"] for c in listed["conversations"]]
            assert keys == [
                "agent:main:Telegram:dm:3",
                "agent:main:discord:group:2",
                "agent:main:telegram:dm:1",
            ]
            first = listed["conversations"][2]
            assert first["display_name"] == "Alice" and first["user_name"] == "alice"
            assert first["platform"] == "telegram" and first["updated_at"].startswith("2026-")
            tele = await call(client, "conversations_list", {"platform": "TELEGRAM"})
            assert tele["count"] == 2
            found = await call(client, "conversations_list", {"search": "general"})
            assert [c["session_key"] for c in found["conversations"]] == [
                "agent:main:discord:group:2"
            ]
            by_key = await call(client, "conversations_list", {"search": "DM:1"})
            assert by_key["count"] == 1
            assert (await call(client, "conversations_list", {"limit": "1"}))["count"] == 1
            assert (await call(client, "conversations_list", {"limit": "x"}))["count"] == 3
            assert (await call(client, "conversations_list", {"limit": 999}))["count"] == 3
            none = await call(client, "conversations_list", {"search": "nobody"})
            assert none == {"count": 0, "conversations": []}
    finally:
        await bridge.aclose()


@aio
async def test_conversation_get_has_ids_times_and_token_totals(scope: Scope, world: World) -> None:
    sid = world.conversation(
        "agent:main:slack:group:C1:T9", platform="slack", chat_id="C1", thread_id="T9"
    )
    raw = sqlite3.connect(str(connection.default_store_path(scope)))
    with raw:
        raw.execute(
            "UPDATE sessions SET input_tokens = 120, output_tokens = 30 WHERE id = ?", (sid,)
        )
    raw.close()
    bridge = await open_bridge(scope, start_events=False)
    try:
        async with connected(bridge) as client:
            got = await call(
                client, "conversation_get", {"session_key": "agent:main:slack:group:C1:T9"}
            )
            assert got["session_id"] == sid and got["chat_id"] == "C1" and got["thread_id"] == "T9"
            assert (got["input_tokens"], got["output_tokens"], got["total_tokens"]) == (
                120,
                30,
                150,
            )
            assert got["created_at"] and got["updated_at"]
            missing = await call(client, "conversation_get", {"session_key": "nope"})
            assert set(missing) == {"error"} and "nope" in missing["error"]
    finally:
        await bridge.aclose()


# -- messages -------------------------------------------------------------------------------------


@aio
async def test_messages_read_shows_user_and_assistant_text_in_id_order(
    scope: Scope, world: World
) -> None:
    sid = world.conversation("k1")
    base = 1_767_250_000.0  # inside the store's accepted window (2026-01-01)
    world.message(sid, "user", "first", timestamp=base + 50)
    world.message(sid, "assistant", "", tool_calls=[{"id": "t1", "type": "function"}])
    world.message(sid, "tool", "tool output", tool_call_id="t1", timestamp=base + 40)
    world.message(sid, "system", "a system row", timestamp=base + 30)
    world.message(
        sid,
        "assistant",
        [{"type": "text", "text": "second"}, {"type": "image_url", "image_url": {"url": "x"}}],
        timestamp=base + 20,  # the clock went backwards: order stays the row id
    )
    world.message(sid, "user", "y" * 2500, timestamp=base + 10)
    bridge = await open_bridge(scope, start_events=False)
    try:
        async with connected(bridge) as client:
            read = await call(client, "messages_read", {"session_key": "k1"})
            assert read["total_in_session"] == 3 and read["count"] == 3
            assert [m["content"][:6] for m in read["messages"]] == ["first", "second", "yyyyyy"]
            assert [m["role"] for m in read["messages"]] == ["user", "assistant", "user"]
            ids = [int(m["id"]) for m in read["messages"]]
            assert ids == sorted(ids)
            assert len(read["messages"][2]["content"]) == 2000
            assert read["messages"][0]["timestamp"].startswith("2026-")
            last = await call(client, "messages_read", {"session_key": "k1", "limit": 1})
            assert [m["content"][:1] for m in last["messages"]] == ["y"]
            assert last["total_in_session"] == 3
            loose = await call(client, "messages_read", {"session_key": "k1", "limit": "x"})
            assert loose["count"] == 3
            missing = await call(client, "messages_read", {"session_key": "nope"})
            assert "error" in missing
    finally:
        await bridge.aclose()


@aio
async def test_messages_read_caps_the_limit_at_200(scope: Scope, world: World) -> None:
    sid = world.conversation("busy")
    message_rows.append_batch(
        world.store, sid, [{"role": "user", "content": f"m{i}"} for i in range(205)]
    )
    bridge = await open_bridge(scope, start_events=False)
    try:
        async with connected(bridge) as client:
            read = await call(client, "messages_read", {"session_key": "busy", "limit": 999})
            assert read["count"] == 200 and read["total_in_session"] == 205
            assert read["messages"][-1]["content"] == "m204"
    finally:
        await bridge.aclose()


# -- attachments ----------------------------------------------------------------------------------


@aio
async def test_attachments_follow_the_media_path_policy(
    scope: Scope, world: World, tmp_path: Path
) -> None:
    cached = scope.home / "cache" / "images" / "photo.png"
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(b"png")
    stray = tmp_path / "elsewhere" / "notes.txt"
    stray.parent.mkdir(parents=True)
    stray.write_text("private")
    sid = world.conversation("k1")
    text = (
        f"here you go MEDIA:{cached}\nand MEDIA:{stray}\nalso MEDIA:/etc/passwd\n"
        "not this one: `MEDIA:/etc/shadow`"
    )
    mid = world.message(
        sid,
        "assistant",
        [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            {"type": "input_audio", "format": "wav"},
        ],
    )
    bridge = await open_bridge(scope, start_events=False)
    try:
        async with connected(bridge) as client:
            got = await call(
                client, "attachments_fetch", {"session_key": "k1", "message_id": str(mid)}
            )
            assert got["message_id"] == str(mid)
            kinds = [(a["type"], a.get("url") or a.get("path")) for a in got["attachments"]]
            assert ("image", "https://example.test/a.png") in kinds
            assert ("media", os.path.realpath(cached)) in kinds
            inline = [a for a in got["attachments"] if a.get("inline")]
            assert inline and inline[0]["media_type"] == "image/png" and "url" not in inline[0]
            assert any(a["type"] == "input_audio" for a in got["attachments"])
            refused = {r["path"]: r["reason"] for r in got["refused"]}
            assert refused[str(stray)] == "not in a folder media may be sent from"
            assert refused["/etc/passwd"] == "not allowed"
            assert all("/etc/shadow" not in json.dumps(a) for a in got["attachments"])
            assert "/etc/shadow" not in refused
            assert got["count"] == len(got["attachments"])
            missing = await call(
                client, "attachments_fetch", {"session_key": "k1", "message_id": "999999"}
            )
            assert "error" in missing
            no_conv = await call(
                client, "attachments_fetch", {"session_key": "zz", "message_id": str(mid)}
            )
            assert "error" in no_conv
    finally:
        await bridge.aclose()


# -- channels -------------------------------------------------------------------------------------


@aio
async def test_channels_come_from_the_directory_else_from_the_routes(
    scope: Scope, world: World
) -> None:
    world.conversation("a", platform="telegram", chat_id="111", display="Family", chat_type="group")
    world.conversation("b", platform="telegram", chat_id="111", display="Family again")
    world.conversation("c", platform="discord", chat_id="222", chat_name="general")
    bridge = await open_bridge(scope, start_events=False)
    try:
        async with connected(bridge) as client:
            derived = await call(client, "channels_list")
            assert derived["count"] == 2
            assert {c["target"] for c in derived["channels"]} == {"telegram:111", "discord:222"}
            only = await call(client, "channels_list", {"platform": "Discord"})
            assert [c["target"] for c in only["channels"]] == ["discord:222"]

            directory = scope.home / "gateway" / "channel_directory.json"
            directory.parent.mkdir(parents=True, exist_ok=True)
            directory.write_text("{not json")
            assert (await call(client, "channels_list"))["count"] == 2  # corrupt: the routes
            directory.write_text(
                json.dumps(
                    {
                        "updated_at": "2026-01-01T00:00:00+00:00",
                        "platforms": {
                            "slack": [
                                {"id": "C9", "name": "engineering", "type": "channel"},
                                {"id": "C9", "name": "eng-thread", "thread_id": "T1"},
                            ],
                            "telegram": [{"id": "-100", "name": "Ops", "type": "group"}],
                        },
                    }
                )
            )
            listed = await call(client, "channels_list")
            assert [c["target"] for c in listed["channels"]] == [
                "slack:C9",
                "slack:C9:T1",
                "telegram:-100",
            ]
            assert listed["channels"][0] == {
                "target": "slack:C9",
                "platform": "slack",
                "name": "engineering",
                "chat_type": "channel",
            }
            slack = await call(client, "channels_list", {"platform": "SLACK"})
            assert slack["count"] == 2
    finally:
        await bridge.aclose()


# -- the store ------------------------------------------------------------------------------------


@aio
async def test_a_missing_store_is_an_error_answer_and_is_never_created(scope: Scope) -> None:
    path = connection.default_store_path(scope)
    assert not path.exists()
    bridge = await open_bridge(scope, start_events=False)
    try:
        async with connected(bridge) as client:
            for tool, args in (
                ("conversations_list", {}),
                ("conversation_get", {"session_key": "k"}),
                ("messages_read", {"session_key": "k"}),
                ("attachments_fetch", {"session_key": "k", "message_id": "1"}),
            ):
                answer = await call(client, tool, args)
                assert set(answer) == {"error"} and "no session store yet" in answer["error"]
            assert (await call(client, "events_poll"))["events"] == []
    finally:
        await bridge.aclose()
    assert not path.exists()


def test_the_store_is_opened_read_only(scope: Scope, world: World) -> None:
    world.conversation("k1")
    reader = StoreReader(scope)
    try:
        assert (
            reader.read(
                lambda conn: conn.execute("SELECT COUNT(*) FROM gateway_routes").fetchone()[0]
            )
            == 1
        )

        def write(conn: sqlite3.Connection) -> Any:
            return conn.execute("DELETE FROM gateway_routes")

        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.read(write)
        # the reader reopens after a failure and still reads
        assert (
            reader.read(
                lambda conn: conn.execute("SELECT COUNT(*) FROM gateway_routes").fetchone()[0]
            )
            == 1
        )
    finally:
        reader.close()


# -- the command ----------------------------------------------------------------------------------


def _frame(line: str) -> dict[str, Any]:
    loaded = json.loads(line)
    assert isinstance(loaded, dict) and loaded.get("jsonrpc") == "2.0", line
    return loaded


def test_stdout_carries_only_mcp_frames(scope: Scope, world: World, tmp_path: Path) -> None:
    sid = world.conversation("k1", display="Alice")
    world.message(sid, "user", "hello")
    env = {k: v for k, v in os.environ.items() if not k.startswith("INDUS_")}
    env.update(INDUS_HOME=str(scope.home), HOME=str(tmp_path), PYTHONPATH=str(REPO / "src"))
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "conversations_list", "arguments": {"limit": "x"}},
        },
    ]
    proc = subprocess.Popen(
        [sys.executable, "-m", "indus", "mcp", "serve"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(tmp_path),
    )
    try:
        assert proc.stdin is not None and proc.stdout is not None
        frames: list[dict[str, Any]] = []
        for request in requests:
            proc.stdin.write((json.dumps(request) + "\n").encode())
            proc.stdin.flush()
            if "id" in request:
                while True:
                    line = proc.stdout.readline().decode()
                    assert line, "the server closed stdout early"
                    frame = _frame(line)
                    frames.append(frame)
                    if frame.get("id") == request["id"]:
                        break
        proc.stdin.close()
        rest, errors = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    for line in rest.decode().splitlines():
        if line.strip():
            _frame(line)
    assert proc.returncode == 0, errors.decode()
    by_id = {f["id"]: f for f in frames if "id" in f}
    assert by_id[1]["result"]["serverInfo"]["name"] == SERVER_NAME
    assert [t["name"] for t in by_id[2]["result"]["tools"]] == list(TOOL_ORDER)
    answer = json.loads(by_id[3]["result"]["content"][0]["text"])
    assert answer["count"] == 1 and answer["conversations"][0]["display_name"] == "Alice"


def test_a_missing_sdk_prints_a_hint_and_exits_1(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "mcp.server", None)
    state = CliState(scope=scope, stdin_isatty=lambda: False, stdout_isatty=lambda: False)
    result = CliRunner().invoke(app_mod.cli, ["mcp", "serve"], obj=state)
    assert result.exit_code == 1
    assert "needs the MCP SDK" in result.stderr and "pip install" in result.stderr
    assert result.stdout == ""
