"""P17-22: chat turns run as admissions on the daemon's shared session host.

In-process daemons (``daemon_kit``) over the fake Telegram-shaped platform and the scripted fake
model, one after another on the same profile home:

- a DM becomes one admission row (chat principal, the crash marker's token as request id) whose
  turn answers in the chat, threaded to the message;
- a client attached over the daemon's WebSocket (ticket from the control socket) watches the
  messaging session and sees the next chat turn live, in order;
- a turn another surface admits into the chat's session is not posted to the chat unless its
  admission asks for it;
- a daemon that died mid-turn: the started admission is ``unknown`` at the next start, never run
  again; queued input behind it makes the chat hear once that the conversation is paused; further
  messages are dropped; ``/reset`` starts a fresh conversation that answers;
- a marker whose admission never started is not a lost turn: the input runs once after the restart;
- a turn the host started on its own that stored its reply before dying: owed once, not paused.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.gateway.daemon.process import GatewayDaemon
from indus.gateway.daemon.recovery import RECOVERED_LABEL, UNKNOWN_NOTICE, UNKNOWN_REASON
from indus.gateway.daemon.side_services import ControlPart
from indus.service import gateway_control
from indus.service.ws_auth import SUBPROTOCOL, TICKET_PROTOCOL_PREFIX, WS_INTERACTIVE
from indus.state import gateway_routes as routes
from indus.state import registry as store_registry
from indus.state import runtime_ledger as ledger
from indus.state.gateway_runtime import EXIT_OK
from tests.fakes.model import FakeModelServer, text_turn
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
from tests.integration.gateway.test_ws_listener import HAS_WS, RawClient
from tests.unit.gateway.core._kit import model_inputs

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX locks and sockets")


def daemon_for(
    scope: Scope,
    fake: FakeChatPlatform,
    server: FakeModelServer,
    tmp_path: Path,
    *,
    parts: list[Any] | None = None,
) -> GatewayDaemon:
    rebind(fake)
    return GatewayDaemon(
        scope,
        options=quick(),
        registry=fake_registry(fake_entry(fake)),
        service_factory=fake_service(server, tmp_path / "svc-locks"),
        parts=parts if parts is not None else [],
    )


def admissions(scope: Scope) -> list[ledger.AdmissionRow]:
    with store_registry.shared(scope) as store, store.read() as conn:
        ids = [
            r[0] for r in conn.execute("SELECT admission_id FROM runtime_admissions ORDER BY seq")
        ]
    with store_registry.shared(scope) as store:
        rows = [ledger.get(store, admission_id) for admission_id in ids]
    return [row for row in rows if row is not None]


def route(scope: Scope, key: str) -> routes.RouteEntry:
    with store_registry.shared(scope) as store:
        entry = routes.get(store, key)
    assert entry is not None
    return entry


def test_a_dm_is_an_admission_whose_turn_answers_in_the_chat(tmp_path: Path) -> None:
    scope = daemon_scope(tmp_path)
    key = dm_key(scope)
    fake = fake_platform()
    server = model_server(text_turn("Hello there."))
    daemon = daemon_for(scope, fake, server, tmp_path)
    asked: list[str] = []

    async def main() -> None:
        flight = await fly(daemon)
        assert daemon.host is not None and daemon.pipeline.shared is daemon.shared
        asked.append(fake.dm(ALLOWED, "hi").message_id)
        await flight.until(lambda: fake.texts(ALLOWED) == ["Hello there."])
        await flight.until(lambda: not daemon.pipeline.claims.keys())
        sid = daemon.pipeline.cached_session(key)
        assert sid is not None and daemon.host.state(sid).source == "messaging"
        assert await flight.stop() == EXIT_OK

    asyncio.run(main())
    (row,) = admissions(scope)
    assert row.principal_id == f"messaging:telegram:{ALLOWED}"
    assert row.request_id.startswith("turn-")
    assert (row.state, row.outcome, row.final_text) == ("terminal", "completed", "Hello there.")
    (reply,) = fake.messages(ALLOWED)
    assert asked and reply.reply_to == asked[0]
    assert model_inputs(server) == ["hi"]
    entry = route(scope, key)
    assert entry.active_turn_token is None and not entry.resume_pending


@pytest.mark.skipif(not HAS_WS, reason="no WebSocket implementation installed")
def test_an_attached_client_watches_the_chat_turn_live(tmp_path: Path) -> None:
    scope = daemon_scope(tmp_path)
    key = dm_key(scope)
    fake = fake_platform()
    server = model_server(text_turn("First."), text_turn("Second ", "answer."))
    daemon = daemon_for(scope, fake, server, tmp_path, parts=[ControlPart()])

    async def main() -> None:
        flight = await fly(daemon)
        try:
            fake.dm(ALLOWED, "one")
            await flight.until(lambda: fake.texts(ALLOWED) == ["First."])
            await flight.until(lambda: not daemon.pipeline.claims.keys())
            sid = daemon.pipeline.cached_session(key)
            assert sid is not None
            granted = await asyncio.to_thread(
                gateway_control.session_ticket, scope.home, WS_INTERACTIVE
            )
            assert granted.ok and granted.ws_url == daemon.ws_url()
            client, head = await RawClient.dial(
                granted.ws_url, [SUBPROTOCOL, TICKET_PROTOCOL_PREFIX + granted.ticket]
            )
            assert head.startswith("HTTP/1.1 101")
            ready = await client.recv()
            assert ready["method"] == "gateway.ready"
            assert ready["params"]["instance_id"] == daemon.instance_id
            await client.send(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "session.resume",
                    "params": {"session_id": sid},
                }
            )
            resumed = await client.recv()
            while resumed.get("id") != 1:  # notifications may come first
                resumed = await client.recv()
            assert resumed["id"] == 1 and resumed["result"]["session_id"] == sid
            fake.dm(ALLOWED, "two")
            seen: list[dict[str, Any]] = []
            while not seen or seen[-1].get("method") != "turn.done":
                frame = await asyncio.wait_for(client.recv(), 15)
                if "method" in frame:
                    seen.append(frame)
            deltas = "".join(
                f["params"].get("text", "") for f in seen if f["method"] == "message.delta"
            )
            assert deltas == "Second answer."
            seqs = [f["params"]["seq"] for f in seen]
            assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
            await flight.until(lambda: fake.texts(ALLOWED) == ["First.", "Second answer."])
            await client.close()
        finally:
            assert await flight.stop() == EXIT_OK
        assert daemon.ws_url() is None and daemon.status.snapshot().surfaces["ws"] == {
            "state": "stopped"
        }

    asyncio.run(main())


def test_another_surfaces_turn_reaches_the_chat_only_when_asked(tmp_path: Path) -> None:
    scope = daemon_scope(tmp_path)
    key = dm_key(scope)
    fake = fake_platform()
    server = model_server(text_turn("Hi."), text_turn("Kept quiet."), text_turn("Posted."))
    daemon = daemon_for(scope, fake, server, tmp_path)

    async def main() -> None:
        flight = await fly(daemon)
        try:
            fake.dm(ALLOWED, "hello")
            await flight.until(lambda: fake.texts(ALLOWED) == ["Hi."])
            await flight.until(lambda: not daemon.pipeline.claims.keys())
            sid = daemon.pipeline.cached_session(key)
            host = daemon.host
            assert sid is not None and host is not None
            quiet = await host.submit(
                sid, "from the terminal", principal="cli:me", request_id="r1", busy="queue"
            )
            assert (await host.wait(quiet.admission_id)).outcome == "completed"
            loud = await host.admissions.admit(
                "cli:me", host.key_of(sid) or sid, "r2", {"text": "post it", "deliver": True}
            )
            assert (await host.wait(loud.admission_id)).outcome == "completed"
            # The quiet turn ended first: its turn-end listener ran before this reply went out.
            await flight.until(lambda: fake.texts(ALLOWED)[-1:] == ["Posted."])
            assert fake.texts(ALLOWED) == ["Hi.", "Posted."]
        finally:
            assert await flight.stop() == EXIT_OK

    asyncio.run(main())
    assert model_inputs(server) == ["hello", "from the terminal", "post it"]


def _first_session(scope: Scope, fake: FakeChatPlatform, tmp_path: Path) -> str:
    """Boot, answer "hello", stop; the chat's session id."""
    server = model_server(text_turn("hello back"))
    daemon = daemon_for(scope, fake, server, tmp_path)

    async def main() -> str:
        flight = await fly(daemon)
        fake.dm(ALLOWED, "hello")
        await flight.until(lambda: fake.texts(ALLOWED)[-1:] == ["hello back"])
        await flight.until(lambda: not daemon.pipeline.claims.keys())
        assert await flight.stop() == EXIT_OK
        return route(scope, dm_key(scope)).session_id

    return asyncio.run(main())


def _left_by_a_dead_owner(
    scope: Scope, session_id: str, token: str, *, started: bool, queued_after: bool = False
) -> None:
    """What a daemon killed mid-turn leaves: its owner epoch, the chat's admission (started or
    still queued) under the marker's token, the marker on the route, maybe more input queued."""
    key = dm_key(scope)
    with store_registry.shared(scope) as store:
        epoch = ledger.begin_owner_epoch(store, "dead-owner").epoch
        ledger.admit(
            store,
            epoch=epoch,
            principal_id=f"messaging:telegram:{ALLOWED}",
            session_id=session_id,
            request_id=token,
            payload={"text": "do the risky thing"},
        )
        if started:
            claimed = ledger.claim_head(store, epoch=epoch, session_id=session_id)
            assert claimed.kind == "claimed"
        if queued_after:
            ledger.admit(
                store,
                epoch=epoch,
                principal_id=f"messaging:telegram:{ALLOWED}",
                session_id=session_id,
                request_id=token + "-next",
                payload={"text": "and then this"},
            )
        assert routes.begin_turn(store, key, token)


def test_a_turn_lost_with_its_daemon_pauses_the_chat_once_and_never_runs_again(
    tmp_path: Path,
) -> None:
    scope = daemon_scope(tmp_path)
    key = dm_key(scope)
    fake = fake_platform()
    session_id = _first_session(scope, fake, tmp_path)
    _left_by_a_dead_owner(scope, session_id, "turn-1-dead", started=True, queued_after=True)
    before = len(fake.texts(ALLOWED))
    server = model_server(text_turn("fresh start"))
    daemon = daemon_for(scope, fake, server, tmp_path)

    async def main() -> None:
        flight = await fly(daemon)
        report = daemon.boot.recovery
        assert report is not None and report.paused == [key]
        # The queued input behind the lost turn: the chat hears once that it is paused.
        await flight.until(lambda: fake.texts(ALLOWED)[before:] == [UNKNOWN_NOTICE])
        fake.dm(ALLOWED, "are you there?")  # told already: dropped quietly, never run
        fake.dm(ALLOWED, "/reset")
        await flight.until(lambda: len(fake.texts(ALLOWED)) == before + 2)
        entry = route(scope, key)
        assert not entry.resume_pending and entry.active_turn_token is None
        assert entry.session_id != session_id
        assert server.requests == []
        fake.dm(ALLOWED, "let's go again")
        await flight.until(lambda: fake.texts(ALLOWED)[-1] == "fresh start")
        await flight.until(lambda: not daemon.pipeline.claims.keys())
        assert await flight.stop() == EXIT_OK

    asyncio.run(main())
    assert model_inputs(server) == ["let's go again"]
    lost = [row for row in admissions(scope) if row.request_id == "turn-1-dead"]
    assert [row.state for row in lost] == ["unknown"]  # never resubmitted


def test_a_marker_whose_admission_never_started_runs_it_once(tmp_path: Path) -> None:
    scope = daemon_scope(tmp_path)
    key = dm_key(scope)
    fake = fake_platform()
    session_id = _first_session(scope, fake, tmp_path)
    _left_by_a_dead_owner(scope, session_id, "turn-1-queued", started=False)
    server = model_server(text_turn("done now"))
    daemon = daemon_for(scope, fake, server, tmp_path)

    async def main() -> None:
        flight = await fly(daemon)
        report = daemon.boot.recovery
        assert report is not None and report.paused == [] and report.cleared == [key]
        await flight.until(lambda: fake.texts(ALLOWED)[-1] == "done now")
        assert await flight.stop() == EXIT_OK

    asyncio.run(main())
    assert model_inputs(server) == ["do the risky thing"]
    entry = route(scope, key)
    assert entry.active_turn_token is None and entry.resume_reason != UNKNOWN_REASON
    (row,) = [row for row in admissions(scope) if row.request_id == "turn-1-queued"]
    assert (row.state, row.outcome) == ("terminal", "completed")


def test_a_lost_host_turn_whose_reply_was_stored_is_owed_once_not_paused(tmp_path: Path) -> None:
    """A turn the host started on its own (no crash marker) died after storing its reply: the
    reply goes out once with the recovered label, its admission is discarded, nothing pauses."""
    scope = daemon_scope(tmp_path)
    key = dm_key(scope)
    fake = fake_platform()
    session_id = _first_session(scope, fake, tmp_path)
    with store_registry.shared(scope) as store:
        epoch = ledger.begin_owner_epoch(store, "dead-owner").epoch
        ledger.admit(
            store,
            epoch=epoch,
            principal_id="internal:goal-continuation",
            session_id=session_id,
            request_id="goal-1",
            payload={"text": "keep going", "origin": "goal continuation"},
        )
        assert ledger.claim_head(store, epoch=epoch, session_id=session_id).kind == "claimed"

        def earlier(conn: sqlite3.Connection) -> None:  # the stored reply came after it
            conn.execute("UPDATE runtime_admissions SET created_at = created_at - 60")

        store.write(earlier)
    before = len(fake.texts(ALLOWED))

    def boot(server: FakeModelServer) -> None:
        daemon = daemon_for(scope, fake, server, tmp_path)

        async def main() -> None:
            flight = await fly(daemon)
            assert await flight.stop() == EXIT_OK

        asyncio.run(main())

    boot(model_server())
    assert fake.texts(ALLOWED)[before:] == [f"{RECOVERED_LABEL}\n\nhello back"]
    entry = route(scope, key)
    assert not entry.resume_pending
    (row,) = [row for row in admissions(scope) if row.request_id == "goal-1"]
    assert (row.state, row.outcome) == ("terminal", "discarded")
    server = model_server()
    boot(server)
    assert len(fake.texts(ALLOWED)) == before + 1 and server.requests == []


def test_a_background_task_runs_as_an_admission_in_its_own_session(tmp_path: Path) -> None:
    scope = daemon_scope(tmp_path)
    fake = fake_platform()
    server = model_server(text_turn("Background result."))
    daemon = daemon_for(scope, fake, server, tmp_path)

    async def main() -> None:
        flight = await fly(daemon)
        try:
            fake.dm(ALLOWED, "/bg summarise the logs")
            await flight.until(lambda: any("Done:" in t for t in fake.texts(ALLOWED)))
            await flight.until(lambda: not daemon.pipeline._tasks)  # pyright: ignore[reportPrivateUsage]
            assert daemon.host is not None and daemon.host.hosted() == []
        finally:
            assert await flight.stop() == EXIT_OK

    asyncio.run(main())
    done = [t for t in fake.texts(ALLOWED) if "Done:" in t]
    assert len(done) == 1 and done[0].endswith("\nBackground result.")
    (row,) = admissions(scope)
    assert row.principal_id == f"messaging:telegram:{ALLOWED}" and row.request_id.startswith(
        "side-"
    )
    assert (row.state, row.outcome) == ("terminal", "completed")
