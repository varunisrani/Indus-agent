"""The task-board watchers inside a real in-process gateway daemon (P24-12 wiring, checked in wave
42): with ``kanban.dispatch_in_gateway`` (the default) the daemon watches each board under the
root, and a task outcome reaches the chat subscribed to it through the daemon's delivery router;
a multiplexing host runs the board part once (a root-level part), never again per served profile.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from indus.gateway.board_part import BoardPart
from indus.gateway.daemon.parts import PART_FACTORIES, ROOT_PART_FACTORIES
from indus.gateway.daemon.process import GatewayDaemon
from indus.gateway.delivery.parts import RouterPart
from indus.state.board import BoardStore
from indus.state.gateway_runtime import EXIT_OK
from tests.integration.gateway.daemon_kit import (
    ALLOWED,
    daemon_scope,
    fake_entry,
    fake_platform,
    fake_registry,
    fake_service,
    fly,
    model_server,
    quick,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX daemon")


def test_a_task_outcome_reaches_its_subscribed_chat_through_the_daemon(tmp_path: Path) -> None:
    scope = daemon_scope(tmp_path, config="kanban:\n  dispatch_interval_seconds: 3600\n")
    board = BoardStore(scope, "default")
    task = board.create("Write the release notes", created_by="test")
    board.subscribe(task.id, platform="telegram", chat_id=ALLOWED, profile=scope.profile_id)
    board.complete(task.id, summary="notes are in docs/release.md", force=True)
    board.close()
    fake = fake_platform()
    daemon = GatewayDaemon(
        scope,
        options=quick(),
        registry=fake_registry(fake_entry(fake)),
        service_factory=fake_service(model_server(), tmp_path / "svc-locks"),
        parts=[RouterPart(), BoardPart()],
    )

    async def main() -> None:
        flight = await fly(daemon)
        try:
            await flight.until(lambda: any("release notes" in t for t in fake.texts(ALLOWED)))
        finally:
            assert await flight.stop() == EXIT_OK

    asyncio.run(main())


def test_root_level_parts_run_once_per_host() -> None:
    assert "indus.gateway.board_part:create" in ROOT_PART_FACTORIES
    assert "indus.gateway.rooms.loop:create" in ROOT_PART_FACTORIES
    assert set(PART_FACTORIES) >= ROOT_PART_FACTORIES
