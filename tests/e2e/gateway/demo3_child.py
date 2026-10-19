"""Demo 3's crash step in a process of its own (``test_demo3.py`` runs it twice).

``python -m tests.e2e.gateway.demo3_child ROOT crash``: the three-platform daemon on a fake clock
answers a Telegram DM, and the process SIGKILLs itself inside the Telegram send of that answer —
after the ledger row went ``attempting``, before Telegram acknowledged it.

``python -m tests.e2e.gateway.demo3_child ROOT recover``: a fresh daemon on the same profile home
(new wire fakes: the chat starts empty, the model has nothing scripted) boots, recovers, then
stops; it prints one JSON line: the Telegram chat's texts, the model calls made and the ledger
states.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

from tests.fakes.clock import FakeClock
from tests.fakes.model import text_turn
from tests.fakes.platform.telegram_wire import FakeBot
from tests.integration.gateway.three_platforms import (
    START,
    USERS,
    Brain,
    Wires,
    drive,
    start_daemon,
    stop_daemon,
    three_daemon,
    three_scope,
)

ANSWER = "The nightly build is green."
QUESTION = "how is the nightly build?"


def ledger_states(root: Path) -> list[str]:
    with sqlite3.connect(root / "home" / "state.db") as conn:
        return [row[0] for row in conn.execute("SELECT state FROM delivery_ledger")]


async def crash(root: Path) -> None:
    clock = FakeClock(START)
    wires = Wires.on(clock)
    original = FakeBot.send_message

    async def dying_send(self: FakeBot, chat_id: Any, text: str, **kw: Any) -> Any:
        if "nightly build is green" in text.replace("\\", ""):
            sys.stdout.flush()
            os.kill(os.getpid(), signal.SIGKILL)  # the reply is on its way: no ack, no cleanup
        return await original(self, chat_id, text, **kw)

    FakeBot.send_message = dying_send  # type: ignore[method-assign]
    daemon = three_daemon(three_scope(root, clock), wires, Brain((text_turn(ANSWER),)), root)
    task = await start_daemon(daemon, clock, wires)
    wires.telegram.platform.dm(USERS["telegram"], QUESTION)
    await drive(clock, wires, task.done, what="the kill", limit=60.0)
    print("finished without dying")


async def recover(root: Path) -> None:
    clock = FakeClock(START + timedelta(minutes=2))
    wires = Wires.on(clock)
    brain = Brain(())
    daemon = three_daemon(three_scope(root, clock), wires, brain, root)
    chat = wires.telegram.platform
    user = USERS["telegram"]
    task = await start_daemon(daemon, clock, wires)
    await drive(clock, wires, lambda: any("nightly" in t for t in chat.texts(user)), what="reply")
    for _ in range(20):  # a while longer: a second copy would show up here
        await drive(clock, wires, lambda: True, what="settle")
        clock.advance(1.0)
    code = await stop_daemon(daemon, task, clock, wires)
    print(
        json.dumps(
            {
                "texts": chat.texts(user),
                "model_calls": brain.calls,
                "states": ledger_states(root),
                "exit": code,
            }
        )
    )


def main() -> None:
    root, mode = Path(sys.argv[1]), sys.argv[2]
    asyncio.run(crash(root) if mode == "crash" else recover(root))


if __name__ == "__main__":
    main()
