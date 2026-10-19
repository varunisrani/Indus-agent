"""P15-19: kill a sending process at each ledger point, then boot: the right redeliveries only.

Kill points (SIGKILL, so nothing is cleaned up) and what the next boot must send:

- before the pending row → nothing (the reply was never owed in the ledger);
- between pending and attempting → the reply once, plainly (it was never sent);
- during the platform call → the reply once, labelled (it may already be on screen);
- after delivered → nothing.

Three boots in a row never send more than once.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from indus.foundation.clock import SystemClock
from indus.foundation.scope import make_scope
from indus.gateway.delivery.ledger import DUPLICATE_PREFIX, DeliveryLedger
from indus.gateway.outbound.sender import OutboundSender
from indus.state import delivery_ledger as table
from indus.state import registry
from tests.integration.gateway.ledger_child import KEY, REPLY
from tests.unit.gateway.outbound.recording import recording_adapter

ROOT = Path(__file__).resolve().parents[3]


def _kill_at(home: Path, point: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("INDUS_")}
    user_home = home.parent / "user-home"
    user_home.mkdir(exist_ok=True)
    env.update({"INDUS_HOME": str(home), "HOME": str(user_home), "PYTHONPATH": str(ROOT)})
    return subprocess.run(
        [sys.executable, "-m", "tests.integration.gateway.ledger_child", str(home), point],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
        timeout=120,
    )


async def _boot(home: Path) -> list[str]:
    scope = make_scope(home, env={"INDUS_HOME": str(home)}, clock=SystemClock())
    store = registry.open_unshared(scope)
    try:
        adapter = recording_adapter(name="telegram")
        book = DeliveryLedger(store, clock=scope.clock)
        sender = OutboundSender(adapter, scope=scope, platform="telegram", ledger=book)
        book.bind(lambda platform: sender if platform == "telegram" else None)
        await book.recover()
        await book.aclose()
        return [op.text for op in adapter.ops]
    finally:
        store.close()


def _state(home: Path) -> list[str]:
    scope = make_scope(home, env={"INDUS_HOME": str(home)}, clock=SystemClock())
    store = registry.open_unshared(scope)
    try:
        return [row.state for row in table.rows_for_session_key(store, KEY)]
    finally:
        store.close()


@pytest.mark.parametrize(
    ("point", "expected", "rows"),
    [
        ("before_pending", [], []),
        ("pending", [REPLY], ["delivered"]),
        ("await", [f"{DUPLICATE_PREFIX}\n\n{REPLY}"], ["delivered"]),
        ("delivered", [], ["delivered"]),
    ],
)
def test_kill_point_then_boot(
    tmp_path: Path, point: str, expected: list[str], rows: list[str]
) -> None:
    home = tmp_path / "gateway-home"
    home.mkdir()
    killed = _kill_at(home, point)
    assert killed.returncode == -signal.SIGKILL, killed.stderr
    assert "finished" not in killed.stdout
    sent = [asyncio.run(_boot(home)) for _ in range(3)]
    assert sent[0] == expected
    assert sent[1] == [] and sent[2] == []  # never twice across boots
    assert _state(home) == rows
