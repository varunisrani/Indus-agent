"""P15-26: boot recovery — what a previous process left is settled before new turns run.

Daemons run in-process one after another on the same profile home (each with its own scripted
fake model): a stop mid-turn followed by three boots resumes exactly once; a crash-left turn
pauses its conversation (one notice, no model call, ``/reset`` unblocks); a crash-left turn whose
reply was already written is delivered once with a label, never regenerated; the restart-loop
guard blocks auto-resume; restart notices reach home channels once per boot.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.gateway.daemon.lifecycle import INTERRUPTED_REASON, STOPPED_TURN_NOTICE
from indus.gateway.daemon.process import GatewayDaemon
from indus.gateway.daemon.recovery import (
    BACK_ONLINE,
    HEADS_UP,
    RECOVERED_LABEL,
    RESUME_NOTE,
    UNKNOWN_NOTICE,
    UNKNOWN_REASON,
    resolve_unknown,
)
from indus.service.admissions import LOST_TURN_NOTE
from indus.state import gateway_routes as routes
from indus.state import messages, sessions
from indus.state import registry as store_registry
from indus.state import runtime_ledger as ledger
from indus.state.gateway_runtime import EXIT_OK, boots_path
from tests.fakes.model import FakeModelServer, Finish, Stall, Text, Turn, text_turn
from tests.fakes.platform.core import FakeChatPlatform
from tests.integration.gateway.daemon_kit import (
    ALLOWED,
    daemon_scope,
    dm_key,
    fake_entry,
    fake_platform,
    fake_registry,
    fake_service,
    fly,
    model_server,
    quick,
    rebind,
)
from tests.unit.gateway.core._kit import model_inputs

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX locks")


def stalled(text: str) -> Turn:
    return Turn((Text((text,)), Stall(10_000.0), Finish("stop")))


def daemon_for(
    scope: Scope, fake: FakeChatPlatform, server: FakeModelServer, tmp_path: Path
) -> GatewayDaemon:
    rebind(fake)
    return GatewayDaemon(
        scope,
        options=quick(),
        registry=fake_registry(fake_entry(fake)),
        service_factory=fake_service(server, tmp_path / "svc-locks"),
        parts=[],
    )


def route(scope: Scope, key: str) -> routes.RouteEntry | None:
    with store_registry.shared(scope) as store:
        return routes.get(store, key)


def boot_and_stop(scope: Scope, fake: FakeChatPlatform, server: FakeModelServer, tmp: Path) -> int:
    async def main() -> int:
        flight = await fly(daemon_for(scope, fake, server, tmp))
        await flight.until(lambda: not flight.daemon.pipeline.claims.keys())
        return await flight.stop()

    return asyncio.run(main())


def test_a_turn_cut_by_a_stop_resumes_once_across_three_boots(tmp_path: Path) -> None:
    scope = daemon_scope(tmp_path)
    key = dm_key(scope)
    fake = fake_platform()
    first = model_server(stalled("working on it"))

    async def boot_one() -> int:
        flight = await fly(daemon_for(scope, fake, first, tmp_path))
        fake.dm(ALLOWED, "do the long thing")
        await flight.until(lambda: first.requests and flight.daemon.pipeline.claims.keys())
        return await flight.stop()

    assert asyncio.run(boot_one()) == EXIT_OK
    assert fake.texts(ALLOWED) == [STOPPED_TURN_NOTICE]
    entry = route(scope, key)
    assert entry is not None and entry.resume_pending
    assert entry.resume_reason == INTERRUPTED_REASON and entry.active_turn_token is None

    second = model_server(text_turn("Picked it up: done."))
    assert boot_and_stop(scope, fake, second, tmp_path) == EXIT_OK
    assert fake.texts(ALLOWED)[1:] == [HEADS_UP, "Picked it up: done."]
    assert model_inputs(second)[-1] == RESUME_NOTE
    entry = route(scope, key)
    assert entry is not None and not entry.resume_pending

    third = model_server()  # any model call here would raise: nothing may run again
    assert boot_and_stop(scope, fake, third, tmp_path) == EXIT_OK
    assert len(fake.texts(ALLOWED)) == 3
    assert third.requests == []


def test_a_stop_after_the_reply_went_out_neither_interrupts_nor_resumes_the_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reply is delivered but the turn still holds its claim (marker bookkeeping): a stop's
    drain must not send the "stopped" notice nor flag the conversation for a resume (which would
    run the finished turn again at the next boot). Found by Demo 3 (P15-36)."""
    scope = daemon_scope(tmp_path)
    key = dm_key(scope)
    fake = fake_platform()
    server = model_server(text_turn("All done."))
    entered, gate = threading.Event(), threading.Event()
    real_end_turn = routes.end_turn

    def slow_end_turn(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        gate.wait(10.0)
        return real_end_turn(*args, **kwargs)

    monkeypatch.setattr(routes, "end_turn", slow_end_turn)

    async def main() -> int:
        flight = await fly(daemon_for(scope, fake, server, tmp_path))
        fake.dm(ALLOWED, "hello")
        await flight.until(lambda: fake.texts(ALLOWED) == ["All done."] and entered.is_set())
        assert flight.daemon.pipeline.claims.keys()  # only the bookkeeping is left
        threading.Timer(0.3, gate.set).start()
        return await flight.stop()

    assert asyncio.run(main()) == EXIT_OK
    assert fake.texts(ALLOWED) == ["All done."]
    entry = route(scope, key)
    assert entry is not None and not entry.resume_pending and entry.active_turn_token is None


def test_a_crash_left_turn_pauses_the_conversation_until_reset(tmp_path: Path) -> None:
    scope = daemon_scope(tmp_path)
    key = dm_key(scope)
    fake = fake_platform()
    # A finished conversation, then a crash in the middle of a later turn: the marker stays.
    fake.dm(ALLOWED, "hello")
    _run_one_turn(scope, fake, tmp_path)
    with store_registry.shared(scope) as store:
        assert routes.begin_turn(store, key, "turn-77")
    sent_before = len(fake.texts(ALLOWED))

    server = model_server(text_turn("fresh start"))
    daemon = daemon_for(scope, fake, server, tmp_path)

    async def main() -> None:
        flight = await fly(daemon)
        assert daemon.boot.recovery is not None and daemon.boot.recovery.paused == [key]
        fake.dm(ALLOWED, "are you there?")
        await flight.until(lambda: len(fake.texts(ALLOWED)) == sent_before + 1)
        assert fake.texts(ALLOWED)[-1] == UNKNOWN_NOTICE
        fake.dm(ALLOWED, "hello??")  # told once already: dropped quietly, never run
        fake.dm(ALLOWED, "/reset")
        await flight.until(lambda: len(fake.texts(ALLOWED)) == sent_before + 2)
        entry = route(scope, key)
        assert entry is not None and not entry.resume_pending
        assert entry.active_turn_token is None
        assert server.requests == []
        fake.dm(ALLOWED, "let's go again")
        await flight.until(lambda: fake.texts(ALLOWED)[-1] == "fresh start")
        assert await flight.stop() == EXIT_OK

    asyncio.run(main())
    assert model_inputs(server) == ["let's go again"]


def _run_one_turn(scope: Scope, fake: FakeChatPlatform, tmp_path: Path) -> None:
    """Boot, answer the one queued DM ("hello") with "second answer", stop."""
    server = model_server(text_turn("second answer"))

    async def main() -> None:
        flight = await fly(daemon_for(scope, fake, server, tmp_path))
        await flight.until(lambda: fake.texts(ALLOWED)[-1:] == ["second answer"])
        await flight.until(lambda: not flight.daemon.pipeline.claims.keys())
        assert await flight.stop() == EXIT_OK

    asyncio.run(main())


def test_a_reply_written_before_a_crash_is_delivered_once_with_a_label(tmp_path: Path) -> None:
    scope = daemon_scope(tmp_path)
    key = dm_key(scope)
    fake = fake_platform()
    fake.dm(ALLOWED, "hello")
    _run_one_turn(scope, fake, tmp_path)
    # The crash came after the reply was stored and before it was sent: the marker started
    # before the stored reply.
    with store_registry.shared(scope) as store:
        assert routes.begin_turn(store, key, "turn-5")

        def backdate(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE gateway_routes SET active_turn_started_at = active_turn_started_at - 60 "
                "WHERE session_key = ?",
                (key,),
            )

        store.write(backdate)
    before = len(fake.texts(ALLOWED))
    server = model_server()
    assert boot_and_stop(scope, fake, server, tmp_path) == EXIT_OK
    assert fake.texts(ALLOWED)[before:] == [f"{RECOVERED_LABEL}\n\nsecond answer"]
    assert server.requests == []
    entry = route(scope, key)
    assert entry is not None and entry.active_turn_token is None and not entry.resume_pending
    assert boot_and_stop(scope, fake, model_server(), tmp_path) == EXIT_OK
    assert len(fake.texts(ALLOWED)) == before + 1  # delivered once, never again


def test_a_tripped_restart_loop_guard_skips_the_resume_and_keeps_the_flag(
    tmp_path: Path,
) -> None:
    scope = daemon_scope(tmp_path)
    key = dm_key(scope)
    fake = fake_platform()
    fake.dm(ALLOWED, "hello")
    _run_one_turn(scope, fake, tmp_path)
    with store_registry.shared(scope) as store:
        assert routes.mark_resume_pending(store, key, INTERRUPTED_REASON)
    now = scope.clock.now().timestamp()
    boots_path(scope.home).write_text(json.dumps({"boots": [now - 40, now - 20]}))
    before = len(fake.texts(ALLOWED))
    server = model_server()
    daemon = daemon_for(scope, fake, server, tmp_path)

    async def main() -> None:
        flight = await fly(daemon)
        assert daemon.boot.restart_loop_tripped
        assert await flight.stop() == EXIT_OK

    asyncio.run(main())
    assert len(fake.texts(ALLOWED)) == before and server.requests == []
    entry = route(scope, key)
    assert entry is not None and entry.resume_pending and entry.resume_reason == INTERRUPTED_REASON


def test_a_stale_interrupted_flag_is_not_resumed(tmp_path: Path) -> None:
    scope = daemon_scope(tmp_path)
    key = dm_key(scope)
    fake = fake_platform()
    fake.dm(ALLOWED, "hello")
    _run_one_turn(scope, fake, tmp_path)
    with store_registry.shared(scope) as store:
        assert routes.mark_resume_pending(store, key, INTERRUPTED_REASON)

        def age(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE gateway_routes SET updated_at = updated_at - 7200 WHERE session_key = ?",
                (key,),
            )

        store.write(age)
    daemon = daemon_for(scope, fake, model_server(), tmp_path)

    async def main() -> None:
        flight = await fly(daemon)
        assert daemon.boot.recovery is not None
        assert daemon.boot.recovery.skipped == {key: "stale"}
        assert await flight.stop() == EXIT_OK

    asyncio.run(main())


def test_resolve_unknown_checks_the_generation(tmp_path: Path) -> None:
    scope = daemon_scope(tmp_path)
    with store_registry.shared(scope) as store:
        routes.get_or_create(
            store,
            "k",
            new_session_id=lambda: "s1",
            platform="telegram",
            chat_type="dm",
            origin={"platform": "telegram", "chat_id": "1"},
        )
        routes.begin_turn(store, "k", "turn-9")
        routes.mark_resume_pending(store, "k", UNKNOWN_REASON)
        assert not resolve_unknown(store, "k", "turn-8")
        assert routes.get(store, "k").resume_pending  # type: ignore[union-attr]
        assert resolve_unknown(store, "k", "turn-9")
        entry = routes.get(store, "k")
        assert entry is not None and not entry.resume_pending and entry.active_turn_token is None
        assert not resolve_unknown(store, "k", "turn-9")


def test_resolve_unknown_discards_the_lost_admission_and_closes_its_transcript(
    tmp_path: Path,
) -> None:
    scope = daemon_scope(tmp_path)
    with store_registry.shared(scope) as store:
        routes.get_or_create(
            store,
            "k",
            new_session_id=lambda: "s1",
            platform="telegram",
            chat_type="dm",
            origin={"platform": "telegram", "chat_id": "1"},
        )
        sessions.create(store, "s1", "telegram")
        messages.append(store, "s1", {"role": "user", "content": "deploy it"})
        epoch = ledger.begin_owner_epoch(store, "dead-owner").epoch
        ledger.admit(
            store,
            epoch=epoch,
            principal_id="messaging:telegram:1",
            session_id="s1",
            request_id="turn-9",
            payload={"text": "deploy it"},
        )
        ledger.claim_head(store, epoch=epoch, session_id="s1")
        ledger.begin_owner_epoch(store, "next-owner")  # the owner died: started → unknown
        routes.begin_turn(store, "k", "turn-9")
        routes.mark_resume_pending(store, "k", UNKNOWN_REASON)
        assert resolve_unknown(store, "k", "turn-9")
        lost = ledger.find(store, "messaging:telegram:1", "s1", "turn-9")
        assert lost is not None and lost.outcome == "discarded"
        assert [(m["role"], m["content"]) for m in messages.get(store, "s1")] == [
            ("user", "deploy it"),
            ("assistant", LOST_TURN_NOTE),
        ]


@pytest.mark.parametrize("notice", [True, False])
def test_restart_notices_reach_home_channels_once_per_boot(tmp_path: Path, notice: bool) -> None:
    config = (
        "platforms:\n"
        "  telegram:\n"
        f"    restart_notice: {'true' if notice else 'false'}\n"
        "    home_channel: {platform: telegram, chat_id: '555'}\n"
    )
    scope = daemon_scope(tmp_path, config=config)
    fake = fake_platform()
    assert boot_and_stop(scope, fake, model_server(), tmp_path) == EXIT_OK
    assert fake.texts("555") == []  # a first boot is not a restart
    assert boot_and_stop(scope, fake, model_server(), tmp_path) == EXIT_OK
    assert fake.texts("555") == ([BACK_ONLINE] if notice else [])
