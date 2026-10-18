"""End-to-end smoke (M1 shape): create → submit → tool call → events → resume elsewhere.

A synchronous caller drives the real stack through ``InProcessHost``: session service, agent,
turn loop, P03 caller against the fake model, and the real P02 store. The model asks for a test
tool that writes a file; the events stream to the caller; then a second host with its own store
handle (a new "process") resumes the session and finds the history intact, sends it back to the
model byte for byte (same system prompt) and continues the conversation.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from indus.core.exit_reasons import ExitReason
from indus.core.persist import StateStore
from indus.foundation.scope import Scope
from indus.protocol.events import EventBase, MessageDelta, ToolCompleted, ToolStarted, TurnDone
from indus.protocol.methods import ProtocolError
from indus.service.host_inprocess import Feed, InProcessHost
from indus.service.session_service import SessionService
from indus.state import registry
from indus.state.session_lock import lock_dir_for
from indus.tools.registry import ToolRegistry, ToolTraits
from tests.fakes.agent import fake_options
from tests.fakes.clock import FakeClock
from tests.fakes.model import FakeModelServer, fake_route, script_of, text_turn, tool_batch
from tests.fakes.turn import check_alternation

WAIT = 10.0


def file_tools(folder: Path) -> ToolRegistry:
    tools = ToolRegistry()

    async def make_file(args: Mapping[str, Any]) -> str:
        target = folder / str(args["name"])
        target.write_text(str(args.get("text", "")), encoding="utf-8")
        return f"wrote {target.name}"

    tools.register(
        "make_file",
        "smoke_files",
        {
            "description": "write a small text file",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "text": {"type": "string"}},
                "required": ["name"],
            },
        },
        make_file,
        traits=ToolTraits(mutating=True),
    )
    return tools


def drain(feed: Feed) -> list[EventBase]:
    seen: list[EventBase] = []
    while True:
        item = feed.get(timeout=WAIT)
        if isinstance(item, EventBase):
            seen.append(item)
            if isinstance(item, TurnDone):
                return seen


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    folder = tmp_path / "work"
    folder.mkdir()
    return folder


@pytest.fixture
def handles(scope: Scope) -> Iterator[list[registry.StoreHandle]]:
    opened: list[registry.StoreHandle] = []
    yield opened
    for handle in opened:
        handle.close()


def host_on_store(
    scope: Scope,
    server: FakeModelServer,
    handle: registry.StoreHandle,
    tools: ToolRegistry,
    workdir: Path,
) -> InProcessHost:
    def make() -> SessionService:
        return SessionService(
            scope,
            fake_route(server),
            toolsets=["smoke_files"],
            options=fake_options(server, session_id=None, registry=tools, cwd=workdir),
            store=StateStore(handle),
        )

    return InProcessHost(make, scope=scope, call_timeout=WAIT)


def test_create_submit_tool_events_and_resume_in_a_new_service(
    scope: Scope,
    fake_clock: FakeClock,
    workdir: Path,
    handles: list[registry.StoreHandle],
) -> None:
    tools = file_tools(workdir)
    first_model = FakeModelServer(
        script_of(
            tool_batch(("make_file", {"name": "hello.txt", "text": "hello"})),
            text_turn("Created ", "hello.txt."),
        ),
        clock=fake_clock,
    )
    first_handle = registry.open_unshared(scope)
    handles.append(first_handle)
    with host_on_store(scope, first_model, first_handle, tools, workdir) as host:
        sid = host.create().session_id
        feed, _ = host.feed(sid)
        ticket = host.submit(sid, "create hello")
        events = drain(feed)
        result = host.wait(sid)
        assert lock_dir_for(first_handle.path).is_dir()

    assert result.exit_reason is ExitReason.COMPLETED
    assert result.final_text == "Created hello.txt."
    assert (workdir / "hello.txt").read_text(encoding="utf-8") == "hello"
    kinds = [type(e) for e in events]
    assert kinds.index(ToolStarted) < kinds.index(ToolCompleted) < kinds.index(MessageDelta)
    assert all(e.session_id == sid for e in events)
    assert [e.seq for e in events] == sorted({e.seq for e in events})
    completed = next(e for e in events if isinstance(e, ToolCompleted))
    assert completed.ok and completed.name == "make_file" and completed.result == "wrote hello.txt"
    assert ticket.generation == 1
    first_system = first_model.model_requests[0].body["messages"][0]

    # A new "process": a second store handle and a second host resume the session.
    second_model = FakeModelServer(script_of(text_turn("It says hello.")), clock=fake_clock)
    second_handle = registry.open_unshared(scope)
    handles.append(second_handle)
    with host_on_store(scope, second_model, second_handle, tools, workdir) as again:
        resumed = again.resume(sid)
        assert resumed.message_count == 4  # user, call, result, answer
        assert resumed.pending_requests == []
        again.submit(sid, "what is in it?")
        follow = again.wait(sid)
        with pytest.raises(ProtocolError):
            again.resume("20260101_000000_ffffff")

    assert follow.final_text == "It says hello."
    check_alternation(follow.rows)
    assert [r.role for r in follow.rows] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
        "assistant",
    ]
    sent = second_model.model_requests[0].body["messages"]
    assert sent[0] == first_system  # the stored prompt is reused byte for byte
    assert [m["role"] for m in sent[1:]] == ["user", "assistant", "tool", "assistant", "user"]
    stored = again_rows(second_handle, sid)
    assert len(stored) == 6
    check_alternation(stored)


def again_rows(handle: registry.StoreHandle, sid: str) -> list[Any]:
    import asyncio

    return asyncio.run(StateStore(handle).load(sid))
