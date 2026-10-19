"""P15-19 / P15-20 wiring in the intake: R06.5 marker order and mirror notes at the next turn."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from indus.foundation.scope import Scope
from indus.gateway.core.send import SendResult
from indus.gateway.delivery import mirror
from indus.gateway.delivery.ledger import DeliveryLedger
from indus.gateway.outbound.sender import OutboundSender
from indus.state import delivery_ledger as table
from indus.state import gateway_routes as routes
from tests.fakes.clock import FakeClock
from tests.fakes.model import text_turn
from tests.unit.gateway.core._kit import ALLOWED, build_kit, model_inputs


def test_active_turn_marker_clears_only_after_the_final_is_in_the_ledger(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_kit(scope, fake_clock, tmp_path, text_turn("Hello there"))
        gate = asyncio.Event()
        wire_send = kit.adapter._send  # pyright: ignore[reportPrivateUsage]

        async def held(chat_id: str, text: str, reply_to: str | None, meta: Any) -> SendResult:
            if "Hello" in text:
                await gate.wait()
            return await wire_send(chat_id, text, reply_to, meta)

        kit.adapter._send = held  # type: ignore[method-assign]
        book = DeliveryLedger(kit.store, clock=fake_clock)
        kit.sender.add("telegram", OutboundSender(kit.adapter, scope=scope, ledger=book))
        try:
            kit.fake.dm(ALLOWED, "hi bot")
            key = kit.key()

            def rows() -> list[table.LedgerRow]:
                return table.rows_for_session_key(kit.store, key)

            await kit.until(lambda: [r.state for r in rows()] == ["attempting"])
            entry = routes.get(kit.store, key)
            assert entry is not None and entry.active_turn_token is not None
            await kit.settle()
            entry = routes.get(kit.store, key)
            assert entry is not None and entry.active_turn_token is not None  # still sending
            gate.set()
            await kit.until(lambda: not kit.pipeline.busy(key))
            entry = routes.get(kit.store, key)
            assert entry is not None and entry.active_turn_token is None
            (row,) = rows()
            assert row.state == "delivered" and row.content == "Hello there"
            assert row.chat_id == ALLOWED and kit.wire() == ["Hello there"]
        finally:
            await book.aclose()
            await kit.close()

    asyncio.run(main())


def test_a_kept_mirror_note_rides_the_sessions_next_turn(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_kit(scope, fake_clock, tmp_path, text_turn("first"), text_turn("second"))
        try:
            kit.fake.dm(ALLOWED, "hello")
            key = kit.key()
            await kit.until(lambda: kit.wire() == ["first"] and not kit.pipeline.busy(key))
            entry = routes.get(kit.store, key)
            assert entry is not None
            note = mirror.note_text('the scheduled job "report"', "Disk usage is at 91%.")
            mirror.store_note(kit.store, entry.session_id, note)
            kit.fake.dm(ALLOWED, "what was that about?")
            await kit.until(lambda: kit.wire() == ["first", "second"])
            last = model_inputs(kit.server)[-1]
            assert last.startswith("what was that about?")
            assert "Delivery note:" in last and "Disk usage is at 91%." in last
            assert "Disk usage" not in model_inputs(kit.server)[0]
            assert mirror.take_notes(kit.store, entry.session_id) == []
        finally:
            await kit.close()

    asyncio.run(main())
