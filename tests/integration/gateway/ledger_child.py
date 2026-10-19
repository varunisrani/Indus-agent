"""A gateway-like process that sends one ledgered final and is SIGKILLed at a named point.

``python -m tests.integration.gateway.ledger_child HOME POINT`` records a final for chat ``100``
through the real outbound sender and delivery ledger, and kills itself (SIGKILL, no cleanup) at
``POINT``: ``before_pending``, ``pending`` (between pending and attempting), ``await`` (inside the
platform call) or ``delivered``. Used by ``test_ledger_recovery.py``.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path
from typing import Any

from indus.foundation.clock import SystemClock
from indus.foundation.scope import make_scope
from indus.gateway.core.send import SendResult
from indus.gateway.delivery import ledger as ledger_mod
from indus.gateway.delivery.ledger import DeliveryLedger
from indus.gateway.outbound.sender import FinalReply, OutboundSender
from indus.state import registry
from tests.unit.gateway.outbound.recording import RecordingAdapter, recording_adapter

KEY = "@default:telegram:dm:100"
REPLY = "The build is green."


def _die() -> None:
    sys.stdout.flush()
    os.kill(os.getpid(), signal.SIGKILL)


class DyingAdapter(RecordingAdapter):
    platform = "telegram"

    async def _send(
        self, chat_id: str, text: str, reply_to: str | None, meta: dict[str, Any]
    ) -> SendResult:
        _die()  # the platform call is in flight: it may or may not have landed
        raise AssertionError("unreachable")


async def main(home: Path, point: str) -> None:
    scope = make_scope(home, env={"INDUS_HOME": str(home)}, clock=SystemClock())
    store = registry.open_unshared(scope)
    adapter = DyingAdapter() if point == "await" else recording_adapter(name="telegram")
    book = DeliveryLedger(store, clock=scope.clock)
    sender = OutboundSender(adapter, scope=scope, platform="telegram", ledger=book)
    ledger_mod.FAULT_HOOK = lambda at: _die() if at == point else None
    await sender.deliver("100", FinalReply(REPLY, session_key=KEY, message_ref="s1:t1"))
    print("finished")


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]), sys.argv[2]))
