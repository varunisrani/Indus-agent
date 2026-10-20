"""P19-04: the attached host as a viewer of the real daemon endpoint (``tests.fakes.daemon``).

Profile and capability checks, a transport killed mid-turn (the waiter's outcome is unknown, the
reconnect resubscribes from the last seq and the open approval card is offered once), and a turn
the daemon lost being announced once.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from acp import RequestError

from indus.acp.attached_host import AttachedHost
from indus.acp.backend import BackendError
from indus.foundation.scope import Scope
from indus.protocol.attach import ACP_EDITOR_POLICY
from indus.protocol.events import ExitReason, TurnDone
from indus.service.remote.channel import Channel, DialRefused
from indus.service.remote.host import OutcomeUnknown, RemoteSessionService
from indus.service.remote.journal import Journal
from indus.state import messages
from indus.state.registry import StoreHandle
from tests.contract.acp.harness import attached_backend, open_harness
from tests.fakes.agent import eventually
from tests.fakes.daemon import open_daemon
from tests.fakes.model import text_turn, tool_batch
from tests.fakes.session_host import confirm
from tests.unit.backends._aio import aio

NEW = {"mcpServers": []}


@aio
async def test_a_daemon_of_another_profile_is_refused(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    daemon = await open_daemon(scope, store, tmp_path)
    try:
        daemon.profile_id = "0123456789abcdef"
        host = attached_backend(scope, daemon)
        with pytest.raises(BackendError) as refused:
            await host.start()
        assert refused.value.code == "profile_mismatch"
        assert daemon.rig.dials == 0  # nothing dialled for another profile
        await host.aclose()
    finally:
        await daemon.close()


@aio
async def test_a_daemon_without_the_editor_policy_cannot_host_editor_sessions(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    async with open_harness("attached", scope, store, tmp_path) as h:
        assert h.daemon is not None
        h.daemon.capabilities.remove(ACP_EDITOR_POLICY)
        with pytest.raises(RequestError) as refused:
            await h.call("session/new", {"cwd": str(tmp_path), **NEW})
        assert (refused.value.data or {}).get("reason") == "acp_policy_unavailable"
        assert h.daemon.host.hosted() == []


@aio
async def test_no_daemon_is_daemon_unavailable(scope: Scope, tmp_path: Path) -> None:
    async def nobody() -> Any:
        raise DialRefused("no_daemon", "no gateway answers for this profile")

    async def connect() -> RemoteSessionService:
        channel = Channel(nobody, surface="acp")
        return RemoteSessionService(channel, Journal(scope.home), surface="acp")

    host = AttachedHost(scope, connect=connect)
    with pytest.raises(BackendError) as missing:
        await host.start()
    assert missing.value.code == "daemon_unavailable"


@aio
async def test_the_descriptor_comes_from_runtime_describe_on_every_connection(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    async with open_harness("attached", scope, store, tmp_path) as h:
        daemon = h.daemon
        assert daemon is not None
        await h.call("session/new", {"cwd": str(tmp_path), **NEW})
        host = await h.backend()
        assert isinstance(host, AttachedHost)
        assert ACP_EDITOR_POLICY in host.described["capabilities"]
        service = await host.start()
        daemon.capabilities.remove(ACP_EDITOR_POLICY)
        await daemon.rig.last.kill()
        await eventually(lambda: len(daemon.rig.transports) == 2, limit=10)
        await eventually(lambda: service.channel.state == "ready", limit=10)
        asked = [f for f in daemon.rig.last.sent if f.get("method") == "runtime.describe"]
        assert len(asked) == 1
        assert ACP_EDITOR_POLICY not in host.described["capabilities"]
        with pytest.raises(RequestError) as refused:
            await h.call("session/new", {"cwd": str(tmp_path), **NEW})
        assert (refused.value.data or {}).get("reason") == "acp_policy_unavailable"


@aio
async def test_usage_and_queued_cancel_are_served_by_the_daemon(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    turns = (tool_batch(("confirm", {})), text_turn("done"))
    async with open_harness(
        "attached", scope, store, tmp_path, *turns, tools={"confirm": confirm}
    ) as h:
        daemon = h.daemon
        assert daemon is not None
        host, sid = await _start_turn_waiting_on_approval(h, tmp_path)
        service = await host.start()
        # Another surface queues behind the running turn (an ACP submit interrupts it, R05.18).
        queued = await service.channel.request(
            "prompt.submit", {"session_id": sid, "text": "later", "when_busy": "queue"}
        )
        qid = queued["turn_id"]
        assert await host.cancel(sid, qid) == "cancelled"
        assert await host.cancel(sid, qid) == "settled"
        receipt = await host.receipt(sid, qid)
        assert (receipt.state, receipt.outcome) == ("terminal", "cancelled")
        usage = await host.usage(sid)
        numbers = daemon.host.service.agent(sid).binding.state.window.tokens
        assert usage is not None and usage.size == numbers
        sent = {f.get("method") for f in daemon.rig.last.sent}
        assert {"admission.cancel", "session.usage"} <= sent
        assert {m for m, _ in daemon.calls} <= {"session.mutate"}  # nothing else is shimmed


async def _start_turn_waiting_on_approval(h: Any, tmp_path: Path) -> tuple[AttachedHost, str]:
    result = await h.call("session/new", {"cwd": str(tmp_path), **NEW})
    sid = result["sessionId"]
    host = await h.backend()
    assert isinstance(host, AttachedHost)
    events = h.events
    h.agent.on_backend_event = lambda _key, event: events.append(event)
    await host.submit(sid, "clean up", input_id="b" * 32)
    held = h.agent.sessions.by_editor(sid)
    await eventually(lambda: len(held.offered) == 1, limit=10)
    return host, sid


@aio
async def test_a_killed_transport_fails_the_waiter_and_the_reconnect_resumes_from_the_last_seq(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    turns = (tool_batch(("confirm", {})), text_turn("cleaned"))
    async with open_harness(
        "attached", scope, store, tmp_path, *turns, tools={"confirm": confirm}
    ) as h:
        daemon = h.daemon
        assert daemon is not None
        host, sid = await _start_turn_waiting_on_approval(h, tmp_path)
        service = await host.start()
        waiter = asyncio.ensure_future(service.wait(sid))
        await h.pair.settle()
        assert not waiter.done()
        seen = max(e.seq for e in h.events)
        await daemon.rig.last.kill()
        with pytest.raises(OutcomeUnknown) as lost:
            await waiter
        assert "Do not resend" in str(lost.value)
        await eventually(lambda: len(daemon.rig.transports) == 2, limit=10)
        await eventually(lambda: service.channel.state == "ready", limit=10)
        resubscribe = [f for f in daemon.rig.last.sent if f.get("method") == "session.subscribe"]
        assert resubscribe and resubscribe[0]["params"]["cursor"] == seen
        held = h.agent.sessions.by_editor(sid)
        assert held is not None and len(held.offered) == 1  # the open card is not shown twice
        request, generation = held.offered[0]
        assert generation is not None
        accepted = await host.answer(
            sid, str(request.request_id), generation, {"kind": "approval", "choice": "once"}
        )
        assert accepted is True
        await eventually(lambda: any(isinstance(e, TurnDone) for e in h.events), limit=10)
        done = [e for e in h.events if isinstance(e, TurnDone)]
        assert len(done) == 1 and done[0].reason is ExitReason.COMPLETED
        seqs = [e.seq for e in h.events]
        assert seqs == sorted(set(seqs))  # no event applied twice across the reconnect
        assert any(
            m["role"] == "assistant" and m["content"] == "cleaned" for m in messages.get(store, sid)
        )


@aio
async def test_reconnect_reattaches_editor_mcp(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    spec = {"name": "docs", "command": "/usr/bin/true", "args": [], "env": []}
    async with open_harness("attached", scope, store, tmp_path) as h:
        daemon = h.daemon
        assert daemon is not None
        result = await h.call("session/new", {"cwd": str(tmp_path), "mcpServers": [spec]})
        sid = result["sessionId"]
        host = await h.backend()
        assert isinstance(host, AttachedHost)
        service = await host.start()
        before = list(daemon.calls)
        await daemon.rig.last.kill()
        await eventually(lambda: len(daemon.rig.transports) == 2, limit=10)
        await eventually(lambda: service.channel.state == "ready", limit=10)
        after = daemon.calls[len(before) :]
        assert ("session.mutate", "editor_mcp") in {
            (m, p.get("operation")) for m, p in after if m == "session.mutate"
        }
        assert all(p.get("session_id") == sid for m, p in after if m == "session.mutate")
        # Live reasoning needs no request: the daemon streams it to every viewer (D-16.603).
        assert not [f for f in daemon.rig.last.sent if f.get("method") == "config.set"]


@aio
async def test_a_lost_turn_is_announced_once(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    async with open_harness("attached", scope, store, tmp_path, text_turn("first")) as h:
        daemon = h.daemon
        assert daemon is not None
        result = await h.call("session/new", {"cwd": str(tmp_path), **NEW})
        sid = result["sessionId"]
        host = await h.backend()
        assert isinstance(host, AttachedHost)
        service = await host.start()
        admission = await host.submit(sid, "hi", input_id="c" * 32)
        await eventually(
            lambda: any(m["role"] == "assistant" for m in messages.get(store, sid)), limit=10
        )
        store.write(
            lambda conn: conn.execute(
                "UPDATE runtime_admissions SET state = 'unknown', outcome = NULL "
                "WHERE admission_id = ?",
                (admission.admission_id,),
            )
        )
        for round_ in (2, 3):
            await daemon.rig.last.kill()
            await eventually(lambda n=round_: len(daemon.rig.transports) == n, limit=10)
            await eventually(lambda: service.channel.state == "ready", limit=10)
            await h.pair.settle()
        notices = [
            u["content"]["text"]
            for u in h.pair.updates(sid)
            if u["sessionUpdate"] == "agent_message_chunk"
        ]
        lost = [t for t in notices if "outcome is unknown" in t]
        assert len(lost) == 1
        assert f"/discard {admission.admission_id}" in lost[0]
