"""P24-12 wiring: the task-board part of the gateway daemon (``gateway.board_part``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from indus.foundation.config import writer
from indus.foundation.scope import Scope
from indus.gateway.board_part import BoardPart, create, dispatch_enabled
from indus.gateway.daemon.parts import PART_FACTORIES, Stage
from indus.state.board import BoardStore, board_path
from tests.unit.backends._aio import aio


@dataclass
class _Daemon:
    scope: Scope
    parts: list[Any] = field(default_factory=lambda: [])


def test_the_part_is_a_background_part_every_gateway_loads() -> None:
    assert "indus.gateway.board_part:create" in PART_FACTORIES
    part = create(None)  # type: ignore[arg-type]
    assert isinstance(part, BoardPart) and part.stage is Stage.BACKGROUND


def test_the_switch_defaults_on() -> None:
    assert dispatch_enabled({}) is True
    assert dispatch_enabled({"kanban.dispatch_in_gateway": False}) is False


@aio
async def test_a_profile_without_a_board_gets_no_loop_and_no_file(scope: Scope) -> None:
    part = BoardPart()
    await part.start(_Daemon(scope))  # type: ignore[arg-type]
    assert part._tasks == []  # pyright: ignore[reportPrivateUsage]
    assert not board_path(scope.root, "default").exists()
    await part.stop(_Daemon(scope))  # type: ignore[arg-type]


@aio
async def test_an_existing_board_is_watched_until_stop(scope: Scope) -> None:
    BoardStore(scope, "default").close()
    part = BoardPart()
    daemon = _Daemon(scope)
    await part.start(daemon)  # type: ignore[arg-type]
    tasks = list(part._tasks)  # pyright: ignore[reportPrivateUsage]
    assert len(tasks) == 1 and not tasks[0].done()
    # no router part: a notice reports "not sent" (the cursor stays for the next pass)
    assert await part._deliver({"platform": "telegram", "chat_id": "1"}, "hi") is False  # pyright: ignore[reportPrivateUsage]
    await part.stop(daemon)  # type: ignore[arg-type]
    assert all(task.done() for task in tasks)


@aio
async def test_dispatch_off_leaves_the_boards_to_another_process(scope: Scope) -> None:
    BoardStore(scope, "default").close()
    writer.save(scope, {"kanban": {"dispatch_in_gateway": False}})
    part = BoardPart()
    await part.start(_Daemon(scope))  # type: ignore[arg-type]
    assert part._tasks == []  # pyright: ignore[reportPrivateUsage]
    await part.stop(_Daemon(scope))  # type: ignore[arg-type]
