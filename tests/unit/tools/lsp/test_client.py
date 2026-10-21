"""P24-15: the LSP client against the fake server over stdio."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, cast

import pytest

from indus.foundation.clock import SystemClock
from indus.tools.lsp import client as client_mod
from indus.tools.lsp.client import (
    CONTENT_MODIFIED,
    INIT_TIMEOUT_S,
    MAX_OPEN_FILES,
    LspClient,
    LspError,
    LspProtocolError,
    LspStartError,
    encode_frame,
    read_frame,
)
from tests.conftest import FAKE_CLOCK_START
from tests.fakes.clock import FakeClock
from tests.unit.tools.lsp._helpers import drive, log_of, methods, pause, server_argv, wait_until


def make(tmp_path: Path, clock: object = None, **script: object) -> LspClient:
    return LspClient(
        "fake",
        str(tmp_path),
        server_argv(tmp_path, **cast(dict[str, Any], script)),
        env=dict(os.environ),
        init_options={"a": {"b": 7}},
        clock=clock or SystemClock(),  # type: ignore[arg-type]
    )


def test_constants_match_the_row() -> None:
    assert INIT_TIMEOUT_S == 45.0
    assert client_mod.STREAM_LIMIT == 16 * 1024 * 1024
    assert client_mod.CONTENT_MODIFIED_RETRIES == 3
    assert client_mod.RETRY_BASE_S == 0.5
    assert client_mod.SHUTDOWN_GRACE_S == 1.0
    assert MAX_OPEN_FILES == 64
    assert client_mod.STDERR_TAIL == 30


def test_frames_round_trip_and_limits() -> None:
    async def scenario() -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(encode_frame({"jsonrpc": "2.0", "id": 1, "result": "é"}))
        reader.feed_eof()
        assert await read_frame(reader) == {"jsonrpc": "2.0", "id": 1, "result": "é"}
        assert await read_frame(reader) is None

        big = asyncio.StreamReader()
        big.feed_data(b"Content-Length: " + str(17 * 1024 * 1024).encode() + b"\r\n\r\n")
        with pytest.raises(LspProtocolError, match="16 MiB"):
            await read_frame(big)

        headers = asyncio.StreamReader()
        headers.feed_data(b"X-Pad: " + b"a" * 9000 + b"\r\n")
        with pytest.raises(LspProtocolError, match="header"):
            await read_frame(headers)

        missing = asyncio.StreamReader()
        missing.feed_data(b"Content-Type: x\r\n\r\n{}")
        with pytest.raises(LspProtocolError, match="Content-Length"):
            await read_frame(missing)

    asyncio.run(scenario())


def test_sync_sends_open_then_change_with_versions(tmp_path: Path) -> None:
    log = tmp_path / "log.jsonl"
    target = tmp_path / "a.py"
    target.write_text("x = 1\n", encoding="utf-8")

    async def scenario() -> None:
        client = make(tmp_path, log=str(log))
        await client.start()
        assert client.state == "running"
        assert await client.sync(str(target), language="python") == 0
        assert await client.sync(str(target), "x = 2\r\n", language="python") == 1
        assert client.synced_text(str(target)) == "x = 2\n"
        await client.shutdown()

    asyncio.run(scenario())
    entries = log_of(log)
    init = next(e for e in entries if e.get("method") == "initialize")
    assert init["params"]["rootUri"] == tmp_path.as_uri()
    assert init["params"]["capabilities"]["textDocument"]["publishDiagnostics"]["versionSupport"]
    wanted = ("workspace/didChangeWatchedFiles", "textDocument/didOpen", "textDocument/didChange")
    sync = [e for e in entries if e.get("method") in wanted]
    assert any(e.get("method") == "workspace/didChangeConfiguration" for e in entries)
    kinds = [(e["method"], e["params"].get("textDocument", {}).get("version")) for e in sync]
    assert kinds[:4] == [
        ("workspace/didChangeWatchedFiles", None),
        ("textDocument/didOpen", 0),
        ("workspace/didChangeWatchedFiles", None),
        ("textDocument/didChange", 1),
    ]
    assert sync[0]["params"]["changes"][0]["type"] == 1
    assert sync[2]["params"]["changes"][0]["type"] == 2
    assert sync[1]["params"]["textDocument"]["languageId"] == "python"
    assert methods(log)[-2:] == ["shutdown", "exit"]


def test_pushed_diagnostics_for_the_current_version(tmp_path: Path) -> None:
    target = tmp_path / "a.py"

    async def scenario() -> None:
        client = make(tmp_path)
        await client.start()
        version = await client.sync(str(target), "ok\nx = BAD(name)\n")
        found = await client.diagnostics(str(target), version, timeout=10)
        assert found is not None
        assert [(d["message"], d["range"]["start"]["line"]) for d in found] == [("bad name", 1)]
        await client.shutdown()

    asyncio.run(scenario())


def test_stale_push_is_not_a_verdict(tmp_path: Path) -> None:
    target = tmp_path / "a.py"

    async def scenario() -> None:
        client = make(tmp_path, stale=True)
        await client.start()
        await client.sync(str(target), "BAD(one)\n")
        version = await client.sync(str(target), "fine\n")
        assert await client.diagnostics(str(target), version, timeout=0.3) is None
        assert client.current(str(target)) == []  # nothing stale passes for the current text
        await client.shutdown()

    asyncio.run(scenario())


def test_pulled_diagnostics(tmp_path: Path) -> None:
    target = tmp_path / "a.py"

    async def scenario() -> None:
        client = make(tmp_path, pull=True, push=False)
        await client.start()
        assert client.pull_supported
        version = await client.sync(str(target), "BAD(p)\nWARN(w)\n")
        found = await client.diagnostics(str(target), version, timeout=10)
        assert found is not None
        assert sorted(d["message"] for d in found) == ["bad p", "warn w"]
        await client.shutdown()

    asyncio.run(scenario())


def test_pull_unsupported_falls_back_to_push(tmp_path: Path) -> None:
    target = tmp_path / "a.py"

    async def scenario() -> None:
        client = make(tmp_path, pull_unsupported=True)
        await client.start()
        assert client.pull_supported
        version = await client.sync(str(target), "BAD(q)\n")
        found = await client.diagnostics(str(target), version, timeout=10)
        assert found is not None and found[0]["message"] == "bad q"
        assert not client.pull_supported
        await client.shutdown()

    asyncio.run(scenario())


class RecordingClock(SystemClock):
    """Real time, except that short waits (the retry backoff) are recorded and skipped."""

    def __init__(self) -> None:
        super().__init__()
        self.slept: list[float] = []

    async def sleep(self, seconds: float) -> None:
        if seconds < 5:
            self.slept.append(seconds)
            return
        await super().sleep(seconds)


@pytest.mark.parametrize(("errors", "succeeds"), [(3, True), (4, False)])
def test_content_modified_retried_three_times_with_doubling_delay(
    tmp_path: Path, errors: int, succeeds: bool
) -> None:
    target = tmp_path / "a.py"
    clock = RecordingClock()

    async def scenario() -> None:
        client = make(tmp_path, clock=clock, pull=True, push=False, content_modified=errors)
        await client.start()
        await client.sync(str(target), "BAD(r)\n")
        params = {"textDocument": {"uri": target.as_uri()}}
        if succeeds:
            result = await client.request_retrying("textDocument/diagnostic", params, timeout=10)
            assert result["items"][0]["message"] == "bad r"
        else:
            with pytest.raises(LspError) as caught:
                await client.request_retrying("textDocument/diagnostic", params, timeout=10)
            assert caught.value.code == CONTENT_MODIFIED
        assert clock.slept == [0.5, 1.0, 2.0]
        await client.shutdown()

    asyncio.run(scenario())


def test_initialize_times_out_after_45_seconds(tmp_path: Path) -> None:
    clock = FakeClock(FAKE_CLOCK_START)

    async def scenario() -> None:
        client = make(tmp_path, clock=clock, init="silent", stderr=["booting", "still booting"])
        task = asyncio.ensure_future(client.start())
        await wait_until(lambda: clock.pending >= 1 and len(client.stderr_tail) == 2)
        clock.advance(INIT_TIMEOUT_S - 0.5)
        await pause(0.05)
        assert not task.done()
        with pytest.raises(LspStartError) as caught:
            await drive(clock, task)
        text = str(caught.value)
        assert "within 45 s" in text and "still booting" in text
        assert "exited on signal 9" in text
        assert client.state == "failed"

    asyncio.run(scenario())


def test_crash_on_initialize_reports_exit_code_and_stderr(tmp_path: Path) -> None:
    async def scenario() -> None:
        client = make(tmp_path, init="crash")
        with pytest.raises(LspStartError) as caught:
            await client.start()
        assert "exited with code 3" in str(caught.value)
        assert "fatal: cannot initialize" in str(caught.value)

    asyncio.run(scenario())


def test_missing_binary_is_a_start_error(tmp_path: Path) -> None:
    async def scenario() -> None:
        client = LspClient("ghost", str(tmp_path), [str(tmp_path / "no-such-server")])
        with pytest.raises(LspStartError, match="cannot run"):
            await client.start()
        assert client.state == "failed"

    asyncio.run(scenario())


def test_server_requests_are_answered(tmp_path: Path) -> None:
    log = tmp_path / "log.jsonl"
    requests = [
        {"method": "workspace/configuration", "params": {"items": [{"section": "a.b"}, {}]}},
        {"method": "workspace/workspaceFolders"},
        {"method": "client/registerCapability", "params": {"registrations": []}},
        {"method": "x/unknown"},
    ]

    async def scenario() -> None:
        client = make(tmp_path, log=str(log), server_requests=requests)
        await client.start()
        await wait_until(lambda: len([e for e in log_of(log) if e.get("id", 0) >= 1000]) == 4)
        await client.shutdown()

    asyncio.run(scenario())
    answers = {e["id"]: e for e in log_of(log) if e.get("id", 0) >= 1000}
    assert answers[1000]["result"] == [7, {"a": {"b": 7}}]
    assert answers[1001]["result"][0]["uri"] == tmp_path.as_uri()
    assert answers[1002]["result"] is None
    assert answers[1003]["error"]["code"] == -32601


def test_sixty_fifth_document_closes_the_oldest(tmp_path: Path) -> None:
    log = tmp_path / "log.jsonl"

    async def scenario() -> None:
        client = make(tmp_path, log=str(log), push=False)
        await client.start()
        for n in range(MAX_OPEN_FILES + 1):
            await client.sync(str(tmp_path / f"f{n}.py"), f"x = {n}\n")
        assert len(client.open_paths()) == MAX_OPEN_FILES
        assert str(tmp_path / "f0.py") not in client.open_paths()
        await client.sync(str(tmp_path / "f1.py"), "touched\n")  # f1 becomes the newest
        await client.sync(str(tmp_path / "f65.py"), "x\n")
        assert str(tmp_path / "f2.py") not in client.open_paths()
        assert str(tmp_path / "f1.py") in client.open_paths()
        await client.shutdown()

    asyncio.run(scenario())
    closed = [
        e["params"]["textDocument"]["uri"]
        for e in log_of(log)
        if e.get("method") == "textDocument/didClose"
    ]
    assert closed == [(tmp_path / "f0.py").as_uri(), (tmp_path / "f2.py").as_uri()]


@pytest.mark.parametrize(
    ("script", "steps"),
    [
        ({}, ["shutdown", "exit"]),
        ({"ignore_exit": True}, ["shutdown", "exit", "term"]),
        ({"ignore_exit": True, "ignore_term": True}, ["shutdown", "exit", "term", "kill"]),
    ],
)
def test_shutdown_escalates_polite_then_term_then_kill(
    tmp_path: Path, script: dict[str, object], steps: list[str]
) -> None:
    clock = FakeClock(FAKE_CLOCK_START)

    async def scenario() -> None:
        client = make(tmp_path, clock=clock, **script)
        await drive(clock, client.start())
        pid = client.pid
        assert pid is not None
        await drive(clock, client.shutdown(), step=0.02)
        assert client.stop_steps == steps
        assert client.state == "stopped"
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        await client.shutdown()  # idempotent
        assert client.stop_steps == steps

    asyncio.run(scenario())


def test_dead_server_fails_pending_requests_at_once(tmp_path: Path) -> None:
    target = tmp_path / "a.py"

    async def scenario() -> None:
        client = make(tmp_path, pull=True, die_on="textDocument/diagnostic")
        await client.start()
        await client.sync(str(target), "x\n")
        params = {"textDocument": {"uri": target.as_uri()}}
        with pytest.raises(LspProtocolError):
            await client.request_retrying("textDocument/diagnostic", params, timeout=30)
        await wait_until(lambda: client.state == "failed")
        assert "exited with code 9" in client.failure_details() or not client.running
        with pytest.raises(LspProtocolError):
            await client.sync(str(target), "y\n")
        await client.shutdown()

    asyncio.run(scenario())
