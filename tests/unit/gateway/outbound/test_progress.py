"""P15-16: tool progress, typing and the heartbeat (fake clock)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from indus.gateway.outbound.progress import (
    AuditLog,
    Heartbeat,
    ToolProgress,
    TypingLoop,
    progress_defaults,
    progress_line,
)
from indus.protocol.events import ToolStarted
from tests.fakes.clock import FakeClock
from tests.unit.gateway.outbound.recording import fail, recording_adapter

SECRET = "ghp_" + "Zx9" * 12


def _clock() -> FakeClock:
    return FakeClock(datetime(2026, 1, 1, tzinfo=UTC))


def _drive(
    progress: ToolProgress, clock: FakeClock, script: Callable[[], Awaitable[None]]
) -> list[str]:
    async def main() -> list[str]:
        task = asyncio.create_task(progress.run())
        await clock.run_until_idle()
        await script()
        progress.close()
        return await task

    return asyncio.run(main())


def test_platform_defaults() -> None:
    assert progress_defaults("telegram").mode == "off"
    assert progress_defaults("telegram").heartbeat
    assert progress_defaults("discord").mode == "all"


def test_lines_never_carry_raw_arguments() -> None:
    args = {"command": f"curl -H 'Authorization: token {SECRET}' https://api", "timeout": 5}
    assert progress_line("terminal", args) == "⚙️ Running a command"
    assert progress_line("my_plugin_tool", args) == "⚙️ my plugin tool"
    verbose = progress_line("terminal", args, verbose=True)
    assert verbose.startswith("⚙️ Running a command (terminal)\n```\n")
    assert SECRET not in verbose and "timeout" in verbose


def test_accumulate_edits_one_bubble_with_throttle_and_repeat_counter() -> None:
    clock = _clock()
    adapter = recording_adapter(clock=clock)
    progress = ToolProgress(adapter, "chat", clock=clock, mode="all")

    async def script() -> None:
        progress.tool_started("read_file", {"path": "/a"})
        await clock.run_until_idle()
        progress.tool_started("read_file", {"path": "/b"})
        progress.tool_started("web_search", {"query": "x"})
        await clock.run_until_idle()
        assert len(adapter.ops) == 1  # the edit waits for 1.5 s
        await clock.advance_and_settle(1.5)
        progress.feed(ToolStarted(session_id="s", seq=1, turn_id="t", call_id="c", name="clarify"))
        progress.tool_started("terminal", {"command": "ls"})

    ids = _drive(progress, clock, script)
    assert ids == ["m1"]
    send, edit, last = adapter.ops
    assert send.text == "⚙️ Reading a file"
    assert edit.at == 1.5
    assert edit.text == "⚙️ Reading a file (×2)\n⚙️ Searching the web"
    assert last.text.endswith("\n⚙️ Running a command")
    assert all(op.interim for op in adapter.ops)


def test_new_mode_reports_only_tool_changes() -> None:
    clock = _clock()
    adapter = recording_adapter(clock=clock)
    progress = ToolProgress(adapter, "chat", clock=clock, mode="new", grouping="separate")

    async def script() -> None:
        for name in ["read_file", "read_file", "patch", "patch", "read_file"]:
            progress.tool_started(name)
            await clock.run_until_idle()

    _drive(progress, clock, script)
    assert [op.text for op in adapter.ops] == [
        "⚙️ Reading a file",
        "⚙️ Editing a file",
        "⚙️ Reading a file",
    ]


def test_separate_and_no_edit_platforms_send_one_message_per_line() -> None:
    clock = _clock()
    adapter = recording_adapter(clock=clock, supports_edit=False)
    progress = ToolProgress(adapter, "chat", clock=clock, mode="all")

    async def script() -> None:
        progress.tool_started("read_file")
        progress.tool_started("web_search")
        await clock.run_until_idle()

    _drive(progress, clock, script)
    assert [op.op for op in adapter.ops] == ["send", "send"]


def test_off_mode_sends_nothing() -> None:
    clock = _clock()
    adapter = recording_adapter(clock=clock)
    progress = ToolProgress(adapter, "chat", clock=clock, mode="off")

    async def script() -> None:
        progress.tool_started("terminal")
        await clock.run_until_idle()

    _drive(progress, clock, script)
    assert adapter.ops == []


def test_full_bubble_continues_in_a_new_one() -> None:
    clock = _clock()
    adapter = recording_adapter(clock=clock, max_len=60)
    progress = ToolProgress(adapter, "chat", clock=clock, mode="all")
    names = ["read_file", "patch", "web_search", "terminal", "todo", "memory", "execute_code"]

    async def script() -> None:
        for name in names:
            progress.tool_started(name)
            await clock.advance_and_settle(2.0)

    ids = _drive(progress, clock, script)
    assert len(ids) > 1
    assert all(len(op.text) <= 60 for op in adapter.ops)
    shown = "\n".join(adapter.messages[i] for i in ids)
    assert shown.count("⚙️") == len(names)


def test_uneditable_bubble_falls_back_to_lines() -> None:
    clock = _clock()
    adapter = recording_adapter(clock=clock)
    adapter.edit_results.append(fail("not_found", "message to edit not found"))
    progress = ToolProgress(adapter, "chat", clock=clock, mode="all")

    async def script() -> None:
        progress.tool_started("read_file")
        await clock.run_until_idle()
        progress.tool_started("patch")
        await clock.advance_and_settle(2.0)
        progress.tool_started("terminal")
        await clock.run_until_idle()

    _drive(progress, clock, script)
    assert [(op.op, op.text) for op in adapter.ops][-2:] == [
        ("send", "⚙️ Editing a file"),
        ("send", "⚙️ Running a command"),
    ]


def test_log_mode_writes_a_redacted_rotating_audit_file(tmp_path: Path) -> None:
    clock = _clock()
    adapter = recording_adapter(clock=clock)
    path = tmp_path / "logs" / "tools.log"
    audit = AuditLog(path, max_bytes=400, backups=3)
    progress = ToolProgress(adapter, "chat", clock=clock, mode="log", audit=audit)

    async def script() -> None:
        for _ in range(40):
            progress.tool_started("terminal", {"command": f"echo {SECRET}"})
        await clock.run_until_idle()

    _drive(progress, clock, script)
    assert adapter.ops == []
    files = sorted(p.name for p in path.parent.iterdir())
    assert files == ["tools.log", "tools.log.1", "tools.log.2", "tools.log.3"]
    text = "".join(p.read_text() for p in path.parent.iterdir())
    assert "Running a command" in text and SECRET not in text
    assert all(p.stat().st_size <= 400 for p in path.parent.iterdir())
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_typing_pauses_while_approval_waits() -> None:
    clock = _clock()
    adapter = recording_adapter(clock=clock)
    typing = TypingLoop(adapter, "chat", clock=clock)

    async def main() -> None:
        task = asyncio.create_task(typing.run())
        await clock.run_until_idle()
        for _ in range(5):
            await clock.advance_and_settle(1.0)
        typing.pause()  # t = 5: an approval card is shown
        await clock.run_until_idle()
        for _ in range(4):
            await clock.advance_and_settle(1.0)
        typing.resume()  # t = 9
        await clock.run_until_idle()
        await clock.advance_and_settle(2.0)
        typing.stop()
        await task

    asyncio.run(main())
    starts = [op.at for op in adapter.ops if op.op == "typing"]
    assert starts == [0.0, 2.0, 4.0, 9.0, 11.0]
    stops = [op.at for op in adapter.ops if op.op == "typing_stop"]
    assert stops[0] == 5.0 and stops[-1] == 11.0


def test_heartbeat_is_edited_in_place() -> None:
    clock = _clock()
    adapter = recording_adapter(clock=clock)
    beat = Heartbeat(adapter, "chat", clock=clock)

    async def main() -> None:
        task = asyncio.create_task(beat.run())
        await clock.run_until_idle()
        await clock.advance_and_settle(179)
        assert adapter.ops == []
        await clock.advance_and_settle(1)
        await clock.advance_and_settle(180)
        beat.stop()
        await task

    asyncio.run(main())
    assert [(op.op, op.text) for op in adapter.ops] == [
        ("send", "Still working… 3 min"),
        ("edit", "Still working… 6 min"),
    ]
    assert all(op.interim for op in adapter.ops)


def test_repeat_counter_after_a_fence_gets_its_own_line() -> None:
    clock = _clock()
    adapter = recording_adapter(clock=clock)
    progress = ToolProgress(adapter, "chat", clock=clock, mode="verbose")

    async def script() -> None:
        progress.tool_started("terminal", {"command": "ls"})
        progress.tool_started("terminal", {"command": "ls"})
        await clock.run_until_idle()

    _drive(progress, clock, script)
    assert adapter.ops[-1].text.endswith("\n```\n(×2)")
