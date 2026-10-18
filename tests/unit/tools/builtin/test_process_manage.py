"""``process_manage`` actions over the process registry with fake job handles (P05-14)."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest

from indus.backends.processes import ProcessRecord
from indus.tools.builtin.process_manage import ACTIONS
from indus.tools.registry import ToolRegistry
from tests.fakes.terminal import FakeJob, forge_token
from tests.unit.tools.builtin._terminal_rig import Rig


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def start(rig: Rig, command: str = "server", session_id: str = "s1", **args: Any) -> ProcessRecord:
    body = rig.run(rig.call(session_id, command=command, background=True, **args))
    record = rig.shared.processes.get(body["session_id"], str(rig.scope.home))
    assert record is not None
    return record


def job(record: ProcessRecord) -> FakeJob:
    assert isinstance(record.handle, FakeJob)
    return record.handle


def end(rig: Rig, record: ProcessRecord, code: int = 0) -> None:
    job(record).code = code
    rig.shared.processes.notice_exit(record)


def manage(rig: Rig, conversation: str = "s1", **args: Any) -> dict[str, Any]:
    return rig.run(rig.manage_call(conversation, **args))


# -- lookups and errors -------------------------------------------------------------------------


def test_list_shows_this_conversations_processes(rig: Rig) -> None:
    first = start(rig, "make")
    second = start(rig, "server")
    start(rig, "elsewhere", session_id="s2")
    rig.shared.processes.ingest(first, "built\n")
    end(rig, first, 2)
    listed = manage(rig, action="list")["processes"]
    assert [p["session_id"] for p in listed] == [first.id, second.id]
    assert listed[0]["status"] == "exited" and listed[0]["exit_code"] == 2
    assert listed[0]["output_preview"] == "built\n"
    assert listed[1]["status"] == "running" and "exit_code" not in listed[1]


def test_unknown_and_malformed_calls(rig: Rig) -> None:
    assert manage(rig, action="poll", session_id="proc_ffffffffffff")["status"] == "not_found"
    assert "required for poll" in manage(rig, action="poll")["error"]
    assert "Unknown action" in manage(rig, action="explode", session_id="x")["error"]
    record = start(rig)
    assert "not available" in manage(rig, action="handoff", session_id=record.id)["error"]
    assert set(ACTIONS) >= {"list", "poll", "log", "wait", "kill", "write", "submit", "close"}


def test_prefix_and_numeric_ids(rig: Rig) -> None:
    record = start(rig)
    short = record.id.removeprefix("proc_")[:6]
    assert manage(rig, action="poll", session_id=short)["session_id"] == record.id
    assert manage(rig, action="poll", session_id=1234)["status"] == "not_found"


def test_other_profile_cannot_see_the_job(rig: Rig, tmp_path: Path) -> None:
    record = start(rig)
    other = Rig(tmp_path / "second")
    other.shared = rig.shared
    other.manage._shared = rig.shared  # pyright: ignore[reportPrivateUsage]
    assert manage(other, action="poll", session_id=record.id)["status"] == "not_found"


# -- poll and log -------------------------------------------------------------------------------


def test_poll_running_then_exited_and_never_consumes(rig: Rig) -> None:
    record = start(rig, "build")
    rig.shared.processes.ingest(record, "x" * 1500)
    running = manage(rig, action="poll", session_id=record.id)
    assert running["status"] == "running" and running["pid"] == record.pid
    assert len(running["output_preview"]) == 1000 and "exit_code" not in running
    end(rig, record, 0)
    done = manage(rig, action="poll", session_id=record.id)
    assert done["status"] == "exited" and done["exit_code"] == 0
    assert done["completion_reason"] == "exited"
    assert not record.consumed


def test_log_offset_zero_is_head(rig: Rig) -> None:
    record = start(rig)
    rig.shared.processes.ingest(record, "".join(f"\x1b[32mline {i}\x1b[0m\n" for i in range(300)))
    tail = manage(rig, action="log", session_id=record.id)
    assert tail["total_lines"] == 300 and tail["showing"] == "200 lines"
    assert tail["output"].splitlines()[0] == "line 100" and "\x1b" not in tail["output"]
    head = manage(rig, action="log", session_id=record.id, offset=0, limit=3)
    assert head["output"] == "line 0\nline 1\nline 2"
    past = manage(rig, action="log", session_id=record.id, offset=500)
    assert past["output"] == "" and past["total_lines"] == 300
    assert "at least 1" in manage(rig, action="log", session_id=record.id, limit=0)["error"]


def test_log_through_end_consumes(rig: Rig) -> None:
    record = start(rig)
    rig.shared.processes.ingest(record, "a\nb\nc\n")
    end(rig, record)
    manage(rig, action="log", session_id=record.id, offset=0, limit=2)
    assert not record.consumed  # the end was not read yet
    manage(rig, action="log", session_id=record.id, offset=2, limit=5)
    assert record.consumed


# -- wait ---------------------------------------------------------------------------------------


def test_wait_returns_when_the_job_ends(rig: Rig) -> None:
    record = start(rig, "make")

    async def go() -> dict[str, Any]:
        waiting = asyncio.ensure_future(
            rig.manage_call(action="wait", session_id=record.id, timeout=60)
        )
        await rig.clock.sleep(3)
        rig.shared.processes.ingest(record, "ok\n")
        end(rig, record, 0)
        return await waiting

    body = rig.run(go())
    assert body["status"] == "exited" and body["exit_code"] == 0 and body["output"] == "ok\n"
    assert record.consumed
    assert rig.clock.monotonic() < 10


def test_wait_timeout_is_not_an_error(rig: Rig) -> None:
    record = start(rig, "server")
    body = manage(rig, action="wait", session_id=record.id, timeout=5)
    assert body["status"] == "timeout" and body["process_running"] is True
    assert "This is not an error" in body["timeout_note"]
    assert "error" not in body


def test_wait_is_capped_and_must_be_positive(rig: Rig) -> None:
    record = start(rig, "server")
    capped = manage(rig, action="wait", session_id=record.id, timeout=1000)
    assert "shortened to the 180s limit" in capped["timeout_note"]
    assert rig.clock.monotonic() < 200
    for bad in (0, -3):
        refused = manage(rig, action="wait", session_id=record.id, timeout=bad)
        assert refused["status"] == "error" and "positive" in refused["error"]


def test_wait_interrupted(rig: Rig) -> None:
    record = start(rig, "server")
    stop = threading.Event()
    stop.set()

    async def go() -> dict[str, Any]:
        import json

        text = await rig.manage(
            {"action": "wait", "session_id": record.id},
            scope=rig.scope,
            session_id="s1",
            cancel=stop,
        )
        return json.loads(text)

    body = rig.run(go())
    assert body["status"] == "interrupted" and body["process_running"] is True


# -- kill and input ------------------------------------------------------------------------------


def test_kill_and_already_exited(rig: Rig) -> None:
    record = start(rig, "server")
    rig.shared.processes.ingest(record, "serving\n")
    killed = manage(rig, action="kill", session_id=record.id)
    assert killed["status"] == "killed" and killed["completion_reason"] == "killed"
    assert killed["output"] == "serving\n" and job(record).stopped == 1
    again = manage(rig, action="kill", session_id=record.id)
    assert again["status"] == "already_exited"


def test_stdin_actions(rig: Rig) -> None:
    record = start(rig, "python3 -i")
    assert manage(rig, action="write", session_id=record.id, data="1+1") == {
        "status": "ok",
        "bytes_written": 3,
    }
    assert manage(rig, action="submit", session_id=record.id, data="print(2)")["bytes_written"] == 9
    assert manage(rig, action="close", session_id=record.id) == {
        "status": "ok",
        "message": "input closed",
    }
    assert job(record).written == ["1+1", "print(2)\n"] and job(record).input_closed
    end(rig, record)
    assert manage(rig, action="write", session_id=record.id, data="x")["status"] == "already_exited"


# -- the stop-polling policy --------------------------------------------------------------------


def test_auto_notify_after_two_empty_polls(rig: Rig) -> None:
    rig.deliverable()
    record = start(rig, "long build")
    first = manage(rig, action="poll", session_id=record.id)
    assert "notify_on_complete" not in first
    second = manage(rig, action="poll", session_id=record.id)
    assert second["notify_on_complete"] is True and "Do not wait on or poll" in second["note"]
    assert record.notify_on_exit


def test_auto_notify_on_waits_and_reset_by_output(rig: Rig) -> None:
    rig.deliverable()
    record = start(rig, "long build")
    manage(rig, action="wait", session_id=record.id, timeout=2)
    rig.shared.processes.ingest(record, "progress\n")
    fresh = manage(rig, action="wait", session_id=record.id, timeout=2)
    assert "notify_on_complete" not in fresh  # new output: the count starts over
    manage(rig, action="wait", session_id=record.id, timeout=2)
    told = manage(rig, action="wait", session_id=record.id, timeout=2)
    assert told["notify_on_complete"] is True and "Notify is now on" in told["timeout_note"]


def test_auto_notify_not_where_undeliverable(rig: Rig) -> None:
    record = start(rig, "long build")
    for _ in range(3):
        body = manage(rig, action="poll", session_id=record.id)
    assert "notify_on_complete" not in body and not record.notify_on_exit


def test_auto_notify_switched_off_by_config(tmp_path: Path) -> None:
    rig = Rig(tmp_path, config={"terminal": {"process_wait_auto_notify": False}})
    rig.deliverable()
    record = start(rig, "long build")
    for _ in range(3):
        body = manage(rig, action="poll", session_id=record.id)
    assert "notify_on_complete" not in body


# -- redaction ----------------------------------------------------------------------------------


def test_output_and_command_redacted(rig: Rig) -> None:
    token = forge_token()
    record = start(rig, f"deploy --token {token}")
    rig.shared.processes.ingest(record, f"using {token}\n")
    for action in ("poll", "log"):
        body = manage(rig, action=action, session_id=record.id)
        assert token not in str(body), action
    listed = manage(rig, action="list")
    assert token not in str(listed)
    killed = manage(rig, action="kill", session_id=record.id)
    assert token not in str(killed)


def test_registered_in_the_terminal_toolset(rig: Rig) -> None:
    registry = ToolRegistry()
    rig.manage.register(registry)
    entry = registry.get("process_manage")
    assert entry is not None and entry.toolset == "terminal" and entry.is_async
