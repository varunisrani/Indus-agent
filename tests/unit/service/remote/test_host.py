"""P17-17: the remote session host — idempotent create, the journal, outcome unknown, replay.

The far end is the daemon's real WebSocket endpoint over a ``DaemonSessionHost`` on the fake model,
reached in-loop through the ASGI double with a fault switch (``wire.Rig`` / ``AsgiTransport``).
``RemoteHost`` (the blocking facade) is driven over a real loopback listener from a thread.
"""

from __future__ import annotations

import asyncio
import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from indus.foundation.scope import Scope
from indus.protocol.errors import SESSION_BUSY
from indus.protocol.events import EventBase, ExitReason, MessageDelta, TurnDone
from indus.protocol.methods import ProtocolError, SessionCreateParams
from indus.protocol.server_requests import ApprovalRequest
from indus.service.admissions import LOST_TURN_NOTE
from indus.service.remote.channel import Channel, ChannelLimits, Dialed
from indus.service.remote.host import OutcomeUnknown, RemoteHost, RemoteSessionService
from indus.service.remote.journal import Journal, journal_dir, new_request_id
from indus.service.ws_auth import SUBPROTOCOL, TICKET_PROTOCOL_PREFIX, WS_INTERACTIVE
from indus.state import messages, registry, sessions
from indus.state import runtime_ledger as ledger
from indus.state.registry import StoreHandle
from tests.fakes.agent import eventually
from tests.fakes.model import text_turn, tool_batch
from tests.fakes.session_host import Holder, confirm
from tests.unit.backends._aio import aio
from tests.unit.service.remote.wire import Rig, rig

FAST = ChannelLimits(backoff_first=0.01, backoff_cap=0.05)


@pytest.fixture
def store(scope: Scope) -> Iterator[StoreHandle]:
    handle = registry.open_unshared(scope)
    yield handle
    handle.close()


async def remote(r: Rig, scope: Scope) -> RemoteSessionService:
    channel = Channel(r.dial, limits=FAST)
    return await RemoteSessionService(channel, Journal(scope.home)).start()


def admissions(store: StoreHandle, session_id: str) -> list[tuple[str, str, str]]:
    with store.read() as conn:
        rows = conn.execute(
            "SELECT request_id, state || ':' || coalesce(outcome, ''), principal_id"
            " FROM runtime_admissions WHERE session_id = ?",
            (session_id,),
        ).fetchall()
    return [(str(a), str(b), str(c)) for a, b, c in rows]


# -- create ---------------------------------------------------------------------------------------


@aio
async def test_a_lost_create_reply_then_retry_makes_one_session(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    r = await rig(scope, store, tmp_path)
    svc = await remote(r, scope)
    r.last.cut_reply_to("session.create")
    created = await svc.create(SessionCreateParams(title=None))
    assert r.last is not r.transports[0] and r.transports[0].cut_fired.is_set()
    assert created.created is False  # the retry found the session the first try made
    assert r.host.service.attached() == [created.session_id]
    keys = {
        f["params"]["idempotency_key"]
        for t in r.transports
        for f in t.sent
        if f.get("method") == "session.create"
    }
    assert len(keys) == 1
    await svc.aclose()
    await r.close()


# -- submit and the journal -----------------------------------------------------------------------


@aio
async def test_a_socket_lost_after_admission_is_outcome_unknown(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    r = await rig(scope, store, tmp_path, text_turn("done"))
    svc = await remote(r, scope)
    sid = (await svc.create()).session_id
    r.last.cut_reply_to("prompt.submit")
    with pytest.raises(OutcomeUnknown, match="(?i)do not resend") as lost:
        await svc.submit(sid, "hello")
    request_id = lost.value.request_id
    assert request_id is not None
    kept = svc.journal.get(request_id)
    assert kept is not None and kept.state == "unknown" and kept.text == "hello"
    await eventually(lambda: svc.channel.connected)
    await eventually(lambda: [row[1] for row in admissions(store, sid)] == ["terminal:completed"])
    sent = [f for t in r.transports for f in t.sent if f.get("method") == "prompt.submit"]
    assert len(sent) == 1  # nothing was resent on its own
    # The person retries: the same request id finds the first admission; nothing runs twice.
    again = await svc.retry(request_id)
    assert again.turn_id
    rows = admissions(store, sid)
    assert rows == [(request_id, "terminal:completed", "attach:cli")]
    assert svc.journal.get(request_id) is None
    await svc.aclose()
    await r.close()


@aio
async def test_an_accepted_submit_retires_its_entry(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    holder = Holder()
    r = await rig(
        scope, store, tmp_path, tool_batch(("hold", {})), text_turn("ok"), tools={"hold": holder}
    )
    svc = await remote(r, scope)
    sid = (await svc.create()).session_id
    first = await svc.submit(sid, "work")
    assert first.status.value == "streaming" and svc.journal.pending() == []
    await eventually(lambda: holder.entered)
    holder.gate.set()
    turn = await svc.wait(sid, first.turn_id)
    assert turn.reason is ExitReason.COMPLETED and turn.generation is not None
    assert svc.journal.pending() == []
    await svc.aclose()
    await r.close()


@aio
async def test_a_refusal_from_the_daemon_is_a_protocol_error_and_retires_the_entry(
    scope: Scope, store: StoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = await rig(scope, store, tmp_path)
    svc = await remote(r, scope)
    sid = (await svc.create()).session_id
    original = svc.channel.request

    async def busy_submit(method: str, params: object = None, **kw: object) -> dict[str, object]:
        if method == "prompt.submit":
            from indus.service.remote.channel import RpcError

            raise RpcError(SESSION_BUSY.code, "busy", {"path": "session_id"})
        return await original(method, params, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(svc.channel, "request", busy_submit)
    with pytest.raises(ProtocolError) as refused:
        await svc.submit(sid, "hi")
    assert refused.value.error is SESSION_BUSY and refused.value.path == "session_id"
    assert svc.journal.pending() == []
    monkeypatch.undo()
    await svc.aclose()
    await r.close()


# -- a running turn and the reconnect -------------------------------------------------------------


@aio
async def test_a_reconnect_replays_missed_deltas_exactly_once(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    holder = Holder()
    r = await rig(
        scope,
        store,
        tmp_path,
        tool_batch(("hold", {})),
        text_turn("after", " the", " hold"),
        tools={"hold": holder},
    )
    svc = await remote(r, scope)
    sid = (await svc.create()).session_id
    seen: list[EventBase] = []
    svc.subscribe(sid, seen.append)
    submitted = await svc.submit(sid, "go")
    await eventually(lambda: holder.entered)
    waiter = asyncio.ensure_future(svc.wait(sid, submitted.turn_id))
    r.dial_gate = asyncio.Event()  # hold the reconnect back while the turn finishes
    await r.last.kill()
    with pytest.raises(OutcomeUnknown):
        await waiter
    holder.gate.set()
    receipt = await r.host.wait(submitted.turn_id)
    assert receipt.state == "terminal"
    before = len(seen)
    r.dial_gate.set()
    await eventually(lambda: any(isinstance(e, TurnDone) for e in seen))
    seqs = [e.seq for e in seen]
    assert seqs == sorted(set(seqs))  # in order, nothing twice
    assert len(seen) > before
    text = "".join(e.text for e in seen if isinstance(e, MessageDelta))
    assert text == "after the hold"
    sent = [f for t in r.transports for f in t.sent if f.get("method") == "prompt.submit"]
    assert len(sent) == 1 and admissions(store, sid)[0][1] == "terminal:completed"
    subscribe = [f for f in r.last.sent if f.get("method") == "session.subscribe"]
    assert subscribe and subscribe[0]["params"]["cursor"] > 0
    assert (await svc.wait(sid, submitted.turn_id)).reason is ExitReason.COMPLETED
    await svc.aclose()
    await r.close()


@aio
async def test_an_approval_is_answered_with_its_offered_generation(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    r = await rig(
        scope,
        store,
        tmp_path,
        tool_batch(("confirm", {})),
        text_turn("cleaned"),
        tools={"confirm": confirm},
    )
    svc = await remote(r, scope)
    sid = (await svc.create()).session_id
    asked: list[object] = []
    svc.on_request(sid, asked.append)
    submitted = await svc.submit(sid, "clean")
    await eventually(lambda: asked)
    request = asked[0]
    assert isinstance(request, ApprovalRequest)
    answered = await svc.answer_server_request(
        sid, request.request_id, {"kind": "approval", "choice": "once"}
    )
    assert answered.accepted is True
    answer_frame = next(f for f in r.last.sent if f.get("method") == "server_request.answer")
    assert answer_frame["params"]["generation"] == svc.generation(sid)
    turn = await svc.wait(sid, submitted.turn_id)
    assert turn.reason is ExitReason.COMPLETED
    await svc.aclose()
    await r.close()


@aio
async def test_controls_name_the_running_generation(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    holder = Holder()
    r = await rig(
        scope, store, tmp_path, tool_batch(("hold", {})), text_turn("x"), tools={"hold": holder}
    )
    svc = await remote(r, scope)
    sid = (await svc.create()).session_id
    assert (await svc.interrupt(sid)).interrupted is False  # nothing runs: no call at all
    assert (await svc.steer(sid, "faster")).queued is False
    submitted = await svc.submit(sid, "go")
    await eventually(lambda: holder.entered)
    await eventually(lambda: svc.generation(sid) is not None)
    stale = await svc.interrupt(sid, generation=(svc.generation(sid) or 1) - 1)
    assert stale.interrupted is False and holder.entered == 1
    assert (await svc.interrupt(sid)).interrupted is True
    turn = await svc.wait(sid, submitted.turn_id)
    assert turn.reason in (ExitReason.INTERRUPTED_BY_USER, ExitReason.COMPLETED)
    with pytest.raises(ProtocolError):
        await svc.redirect(sid, "other")  # not served by the daemon yet
    await svc.close(sid)
    with pytest.raises(ProtocolError):
        svc.events(sid)
    await svc.aclose()
    await r.close()


# -- the journal ----------------------------------------------------------------------------------


def test_the_journal_is_private_and_retires_entries(scope: Scope) -> None:
    journal = Journal(scope.home, wall=iter([1.0, 2.0, 3.0]).__next__)
    first = journal.prepare("s1", "one")
    second = journal.prepare("s2", "two")
    folder = journal_dir(scope.home)
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    for entry in (first, second):
        assert stat.S_IMODE((folder / f"{entry.request_id}.json").stat().st_mode) == 0o600
    assert "one" not in repr(first)
    assert [e.request_id for e in journal.pending()] == [first.request_id, second.request_id]
    assert [e.text for e in journal.pending("s2")] == ["two"]
    marked = journal.mark_unknown(first.request_id)
    assert marked is not None and journal.get(first.request_id) == marked
    assert journal.retire(first.request_id) and not journal.retire(first.request_id)
    assert journal.get("../escape") is None and not journal.retire("../escape")
    (folder / f"{new_request_id()}.json").write_text("{not json", encoding="utf-8")
    loose = folder / f"{new_request_id()}.json"
    loose.write_text('{"request_id": "x"}', encoding="utf-8")
    os.chmod(loose, 0o644)
    third = journal.prepare("s3", "three")
    assert [e.request_id for e in journal.pending()] == [second.request_id, third.request_id]


# -- the blocking facade --------------------------------------------------------------------------


@aio
async def test_the_remote_host_facade_runs_a_turn_over_a_real_listener(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    from indus.service.remote.ws_client import connect

    r = await rig(scope, store, tmp_path, text_turn("Hel", "lo"))
    url = await r.endpoint.start()
    tickets = [r.book.mint(WS_INTERACTIVE).ticket for _ in range(2)]

    async def build() -> RemoteSessionService:
        async def dial() -> Dialed:
            transport = await connect(url, [SUBPROTOCOL, TICKET_PROTOCOL_PREFIX + tickets.pop()])
            return Dialed(transport, r.book.profile_id)

        return await RemoteSessionService(Channel(dial), Journal(scope.home)).start()

    def drive() -> tuple[str, str, list[object]]:
        with RemoteHost(build) as host:
            sid = host.create().session_id
            feed, detach = host.feed(sid)
            submitted = host.submit(sid, "hi")
            turn = host.wait(sid, submitted.turn_id, timeout=10.0)
            detach()
            items: list[object] = []
            while not feed.empty():
                items.append(feed.get())
            host.close_session(sid)
            return sid, turn.reason.value, items

    try:
        sid, reason, items = await asyncio.to_thread(drive)
    finally:
        await r.close()
    assert sid and reason == "completed"
    assert "".join(i.text for i in items if isinstance(i, MessageDelta)) == "Hello"
    with pytest.raises(RuntimeError):
        RemoteHost(build).create()


# -- lost turns -----------------------------------------------------------------------------------


def _lose_a_turn(store: StoreHandle, session_id: str, text: str) -> str:
    """A turn an earlier owner started and died with: ``unknown`` once the next host opens."""
    sessions.create(store, session_id, "cli")
    messages.append(store, session_id, {"role": "user", "content": text})
    epoch = ledger.begin_owner_epoch(store, "dead-owner").epoch
    admitted = ledger.admit(
        store,
        epoch=epoch,
        principal_id="attach:cli",
        session_id=session_id,
        request_id=f"lost-{session_id}",
        payload={"text": text},
    )
    assert ledger.claim_head(store, epoch=epoch, session_id=session_id).kind == "claimed"
    return admitted.row.admission_id


@aio
async def test_a_lost_turn_is_discarded_by_its_generation_and_closed_in_its_transcript(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    lost = _lose_a_turn(store, "s-lost", "deploy it")
    other = _lose_a_turn(store, "s-other", "and this")
    r = await rig(scope, store, tmp_path, text_turn("next"))
    row = ledger.get(store, lost)
    assert row is not None and row.state == "unknown" and row.generation == 1
    svc = await remote(r, scope)
    await svc.resume("s-lost")
    stale = await svc.discard_unknown("s-lost", lost, 2)
    assert (stale.discarded, stale.reason) == (False, "stale_generation")
    foreign = await svc.discard_unknown("s-lost", other, 1)
    assert (foreign.discarded, foreign.reason) == (False, "not_found")  # another session's
    done = await svc.discard_unknown("s-lost", lost, 1)
    assert done.discarded and done.reason is None
    after = ledger.get(store, lost)
    assert after is not None and after.outcome == "discarded"
    assert [(m["role"], m["content"]) for m in messages.get(store, "s-lost")] == [
        ("user", "deploy it"),
        ("assistant", LOST_TURN_NOTE),
    ]
    still = ledger.get(store, other)
    assert still is not None and still.state == "unknown"  # untouched
    # The queue moves on: a new input runs.
    submitted = await svc.submit("s-lost", "go on")
    ended = await svc.wait("s-lost", submitted.turn_id)
    assert ended.reason is ExitReason.COMPLETED
    assert svc.turn_of("s-lost", submitted.turn_id) == (ended.turn_id, 2)
    await svc.aclose()
    await r.close()
