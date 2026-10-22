"""A session started in a folder runs its tools there, not in the host's work folder (W47-b).

The desktop creates each chat with the chat's folder (a project's folder or the profile's default
directory); the daemon serving it runs in its own work folder. A relative path a file tool gets
must land in the chat's folder, both for the session just created and for the same session
attached again by another service (a daemon restart, a window reload).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from indus.core.persist import MemoryStore
from indus.foundation.scope import Scope
from indus.protocol.methods import SessionCreateParams
from indus.service.session_service import SessionService
from indus.tools.builtin import write_file
from indus.tools.registry import ToolRegistry
from tests.fakes.agent import fake_options
from tests.fakes.model import FakeModelServer, fake_route, script_of, text_turn, tool_batch


def _service(
    scope: Scope, model: FakeModelServer, store: MemoryStore, locks: Path
) -> SessionService:
    registry = ToolRegistry(clock=scope.clock)
    write_file.register_tools(registry)
    # The host's own default folder, as the daemon's factory sets it (its work folder).
    options = fake_options(model, registry=registry, session_id=None, cwd=Path.cwd())
    return SessionService(
        scope,
        fake_route(model),
        toolsets=["file"],
        options=options,
        store=store,
        lock_dir=locks,
    )


def _folders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    daemon = tmp_path / "daemon-work"
    chat = tmp_path / "chat-folder"
    daemon.mkdir()
    chat.mkdir()
    monkeypatch.chdir(daemon)  # the host process runs in its own work folder
    return daemon, chat


def test_a_relative_write_lands_in_the_chat_folder(
    scope: Scope, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon, chat = _folders(tmp_path, monkeypatch)
    model = FakeModelServer(
        script_of(
            tool_batch(("write_file", {"path": "notes/plan.md", "content": "in the chat folder"})),
            text_turn("Written."),
        )
    )

    async def scenario() -> None:
        svc = _service(scope, model, MemoryStore(), tmp_path / "locks")
        try:
            created = await svc.create(SessionCreateParams(cwd=str(chat)))
            ticket = await svc.submit(created.session_id, "Write the plan.")
            await asyncio.wait_for(svc.wait(created.session_id, ticket.turn_id), 30)
        finally:
            await svc.aclose()

    asyncio.run(scenario())

    assert (chat / "notes" / "plan.md").read_text(encoding="utf-8") == "in the chat folder"
    assert not (daemon / "notes").exists()


def test_a_session_attached_again_keeps_its_folder(
    scope: Scope, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon, chat = _folders(tmp_path, monkeypatch)
    model = FakeModelServer(
        script_of(
            text_turn("Hello."),
            tool_batch(("write_file", {"path": "after-restart.txt", "content": "still here"})),
            text_turn("Written."),
        )
    )
    store = MemoryStore()

    async def scenario() -> None:
        first = _service(scope, model, store, tmp_path / "locks")
        try:
            created = await first.create(SessionCreateParams(cwd=str(chat)))
            sid = created.session_id
            ticket = await first.submit(sid, "Hi.")
            await asyncio.wait_for(first.wait(sid, ticket.turn_id), 30)
        finally:
            await first.aclose()
        # A fresh service over the same store: the restarted daemon resuming the chat.
        second = _service(scope, model, store, tmp_path / "locks")
        try:
            await second.resume(sid)
            ticket = await second.submit(sid, "Write a file.")
            await asyncio.wait_for(second.wait(sid, ticket.turn_id), 30)
        finally:
            await second.aclose()

    asyncio.run(scenario())

    assert (chat / "after-restart.txt").read_text(encoding="utf-8") == "still here"
    assert not (daemon / "after-restart.txt").exists()


def test_a_session_without_a_folder_keeps_the_profile_terminal_cwd(
    scope: Scope, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon, chat = _folders(tmp_path, monkeypatch)
    configured = tmp_path / "configured"
    configured.mkdir()
    scope = scope.with_config(_Config({"terminal.cwd": str(configured)}))
    model = FakeModelServer(
        script_of(
            tool_batch(("write_file", {"path": "plain.txt", "content": "configured"})),
            text_turn("Written."),
        )
    )

    async def scenario() -> None:
        svc = _service(scope, model, MemoryStore(), tmp_path / "locks")
        try:
            created = await svc.create()
            ticket = await svc.submit(created.session_id, "Write it.")
            await asyncio.wait_for(svc.wait(created.session_id, ticket.turn_id), 30)
        finally:
            await svc.aclose()

    asyncio.run(scenario())

    assert (configured / "plain.txt").read_text(encoding="utf-8") == "configured"
    assert not (daemon / "plain.txt").exists()
    assert not (chat / "plain.txt").exists()


class _Config:
    """A config source answering a few dotted keys."""

    def __init__(self, values: dict[str, object]) -> None:
        self.values = values

    def get(self, path: str, /) -> object:
        return self.values.get(path)
