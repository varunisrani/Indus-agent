"""P17-18 over P17-15..17: the CLI's real attach backend (``attach_remote``) against the daemon's
real WebSocket endpoint.

The far end is ``WsEndpoint`` over a ``DaemonSessionHost`` on the fake model (``wire.rig``),
listening on loopback; the CLI runs in a worker thread exactly as a person's command would
(``AttachedHost`` on its own loop, ``Channel`` + ``RemoteSessionService`` over the ``wsproto``
client). Only ``ensure`` is replaced (it would start a daemon process; that is covered end to
end in ``tests/integration/cli/test_attach_daemon.py``), and the dial mints tickets from the
endpoint's book instead of asking a control socket.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from click.testing import CliRunner

from indus.cli import app as app_mod
from indus.cli import attach, first_run
from indus.cli.args import CliState
from indus.cli.attach_remote import DaemonBackend, RemoteAttached, RemoteRefusal
from indus.foundation.scope import Scope
from indus.protocol.events import ExitReason
from indus.protocol.methods import SessionCreateParams
from indus.service.admissions import LOST_TURN_NOTE
from indus.service.gateway_ensure import Endpoint, Ensured
from indus.service.remote.channel import Channel, Dialed
from indus.service.remote.host import OutcomeUnknown, RemoteSessionService
from indus.service.remote.journal import Journal
from indus.service.ws_auth import SUBPROTOCOL, TICKET_PROTOCOL_PREFIX, WS_INTERACTIVE
from indus.state import messages, registry, sessions
from indus.state import runtime_ledger as ledger
from indus.state.registry import StoreHandle
from tests.fakes.model import text_turn
from tests.unit.backends._aio import aio
from tests.unit.service.remote.wire import Rig, rig


@pytest.fixture
def store(scope: Scope) -> Iterator[StoreHandle]:
    handle = registry.open_unshared(scope)
    yield handle
    handle.close()


class Ran:
    def __init__(self, code: int, out: str, err: str) -> None:
        self.code, self.out, self.err = code, out, err


def invoke(scope: Scope, *argv: str) -> Ran:
    state = CliState(scope=scope, stdin_isatty=lambda: False, stdout_isatty=lambda: False)
    with CliRunner().isolation() as (out, err, _mixed):
        code = app_mod.dispatch(list(argv), state)
    return Ran(code, out.getvalue().decode(), err.getvalue().decode())


def ready(url: str, r: Rig) -> Callable[..., Ensured]:
    def fake(home: Path, env: Any, **kw: Any) -> Ensured:
        assert kw["mode"] == "start"
        endpoint = Endpoint(url, r.book.profile_id, r.book.instance_id, "test")
        return Ensured("ready", endpoint=endpoint, pid=1)

    return fake


async def listening(r: Rig, monkeypatch: pytest.MonkeyPatch, *, dials: int = 4) -> DaemonBackend:
    """The endpoint on loopback and the CLI's backend pointed at it."""
    from indus.service.remote.ws_client import connect

    url = await r.endpoint.start()
    tickets = [r.book.mint(WS_INTERACTIVE).ticket for _ in range(dials)]

    async def dial() -> Dialed:
        transport = await connect(url, [SUBPROTOCOL, TICKET_PROTOCOL_PREFIX + tickets.pop()])
        return Dialed(transport, r.book.profile_id)

    backend = DaemonBackend(ensure_fn=ready(url, r), dial=dial)
    monkeypatch.setattr(attach, "backend_factory", lambda: backend)
    monkeypatch.setattr(first_run, "usable_provider", lambda _scope: "a test provider")
    return backend


def rows(store: StoreHandle) -> list[tuple[str, str, str]]:
    with store.read() as conn:
        found = conn.execute(
            "SELECT principal_id, state, coalesce(outcome, '') FROM runtime_admissions ORDER BY seq"
        ).fetchall()
    return [(str(a), str(b), str(c)) for a, b, c in found]


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


# -- finite runs ----------------------------------------------------------------------------------


@aio
async def test_chat_q_runs_its_turn_in_the_daemon_and_prints_the_reply(
    scope: Scope, store: StoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = await rig(scope, store, tmp_path, text_turn("Hel", "lo"), text_turn("again"))
    await listening(r, monkeypatch)
    try:
        ran = await asyncio.to_thread(invoke, scope, "chat", "-q", "say hello")
        oneshot = await asyncio.to_thread(invoke, scope, "-z", "once more")
    finally:
        await r.close()
    assert ran.code == 0, ran.err
    assert ran.out == "Hello\n"
    assert "session_id: " in ran.err
    assert oneshot.code == 0, oneshot.err
    assert (oneshot.out, oneshot.err) == ("again\n", "")
    # Both turns ran in the daemon, admitted under the CLI's principal; nothing ran here.
    assert rows(store) == [("attach:cli", "terminal", "completed")] * 2


def _risky_tool(scope: Scope, decisions: list[Any]) -> Callable[..., Any]:
    """An inline tool that asks the real approval pipeline about a risky command, the way the
    terminal tool does (the turn's run context and broker), and records the decision."""
    from indus.core.turn_state import TurnState
    from indus.foundation.config import loader
    from indus.safety.approvals.decision import ApprovalContext
    from indus.safety.approvals.policy import decide
    from indus.tools.calls import ToolCall, ToolResult

    async def risky(call: ToolCall, state: TurnState) -> ToolResult:
        read = scope.with_config(loader.load(scope))
        ctx = ApprovalContext(
            scope=read,
            session_id="s-risky",
            turn_id=state.turn_id,
            run_context=cast(Any, state.runtime.run_context or "interactive"),
            broker=state.control.broker,
        )
        decision = await decide("git reset --hard", ctx)
        decisions.append(decision)
        return ToolResult(decision.message or "ran")

    return risky


@aio
@pytest.mark.parametrize("mode", ["deny", "approve"])
async def test_an_attached_chat_q_decides_approvals_as_local_does(
    scope: Scope,
    store: StoreHandle,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """W47 g (D-16.487; was W46 F-5's client-side answer): an attached finite ``chat -q`` runs its
    turn ``single_query`` in the daemon, so a risky command is refused with ``--local``'s wording
    (``approvals.single_query_mode: deny``, the default) or allowed when set to approve; nothing
    waits for an answer, the turn ends normally and nobody is asked."""
    from tests.fakes.model import tool_batch

    if mode == "approve":
        (scope.home / "config.yaml").write_text(
            "approvals:\n  single_query_mode: approve\n", encoding="utf-8"
        )
    decisions: list[Any] = []
    r = await rig(
        scope,
        store,
        tmp_path,
        tool_batch(("risky", {})),
        text_turn("I left the build folder alone."),
        tools={"risky": _risky_tool(scope, decisions)},
    )
    await listening(r, monkeypatch)
    try:
        ran = await asyncio.to_thread(invoke, scope, "chat", "-q", "clean up")
    finally:
        await r.close()
    assert ran.code == 0, ran.err
    assert "I left the build folder alone." in ran.out
    assert "waiting for an answer" not in ran.err
    (decision,) = decisions
    if mode == "deny":
        assert not decision.approved and decision.step == "unattended"
        assert "a one-shot run has nobody present to approve it" in (decision.message or "")
        assert "approvals.single_query_mode: approve" in (decision.message or "")
    else:
        assert decision.approved and decision.step == "unattended"
    assert rows(store) == [("attach:cli", "terminal", "completed")]


@aio
async def test_a_session_behind_a_lost_turn_exits_3_and_discard_lets_it_go_on(
    scope: Scope, store: StoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lost = _lose_a_turn(store, "s-lost", "deploy it")
    r = await rig(scope, store, tmp_path, text_turn("next answer"))
    backend = await listening(r, monkeypatch, dials=6)

    def discard() -> tuple[list[attach.PendingRow], list[str], list[str]]:
        host = attach.AttachedHost(backend, scope, object()).start()
        try:
            sid = host.invoke("resume", "s-lost", policy=None).session_id
            listed = host.pending(sid)
            repl = _Repl(sid)
            hook = attach.attached_commands(host)
            hook(cast(Any, repl), f"/discard {lost}")
            return listed, repl.notes, repl.errors
        finally:
            host.stop()

    try:
        blocked = await asyncio.to_thread(invoke, scope, "-r", "s-lost", "chat", "-q", "hi")
        listed, notes, errors = await asyncio.to_thread(discard)
        after = await asyncio.to_thread(invoke, scope, "-r", "s-lost", "chat", "-q", "hi")
    finally:
        await r.close()
    assert blocked.code == 3, blocked.err
    assert "/discard" in blocked.err and "nothing was sent" in blocked.err
    assert [(row.admission_id, row.state, row.generation) for row in listed] == [
        (lost, "unknown", 1)
    ]
    assert listed[0].text == "deploy it"
    assert errors == [] and notes[-1].startswith(f"discarded {lost}")
    assert after.code == 0, after.err
    assert after.out == "next answer\n"
    contents = [(m["role"], m["content"]) for m in messages.get(store, "s-lost")]
    assert contents[:2] == [("user", "deploy it"), ("assistant", LOST_TURN_NOTE)]
    assert rows(store) == [
        ("attach:cli", "terminal", "discarded"),
        ("attach:cli", "terminal", "completed"),
    ]


class _Repl:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.notes: list[str] = []
        self.errors: list[str] = []

    def note(self, text: str) -> None:
        self.notes.append(text)

    def error(self, text: str) -> None:
        self.errors.append(text)


# -- the adapter ----------------------------------------------------------------------------------


@aio
async def test_create_sends_only_what_the_wire_carries(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    r = await rig(scope, store, tmp_path)
    service = await RemoteSessionService(Channel(r.dial), Journal(scope.home)).start()
    attached = RemoteAttached(service, scope)
    policy = {"cwd": str(tmp_path), "model": "m1", "provider": "p1", "yolo": True}
    created = await attached.create(SessionCreateParams(), source="cli", policy=policy)
    assert created.created
    (frame,) = [f for f in r.last.sent if f.get("method") == "session.create"]
    params = frame["params"]
    assert params["cwd"] == str(tmp_path) and params["model"] == "m1"
    assert "provider" not in params and "yolo" not in params
    await attached.aclose()
    await r.close()


class _LostService:
    """A remote service whose wait loses its transport (the daemon went away)."""

    def __init__(self) -> None:
        self.listeners: list[Any] = []

    async def wait(self, session_id: str, turn_id: str | None = None) -> Any:
        raise OutcomeUnknown(session_id, turn_id=turn_id)

    def key_of(self, session_id: str) -> str:
        return session_id

    def current_id(self, session_id: str) -> str:
        return session_id


@aio
async def test_a_lost_wait_is_outcome_unknown_or_blocked_when_ours_still_waits(
    scope: Scope, store: StoreHandle
) -> None:
    lost = _lose_a_turn(store, "s1", "first")
    attached = RemoteAttached(cast(Any, _LostService()), scope)
    with pytest.raises(RemoteRefusal) as unknown:
        await attached.wait("s1", lost)
    assert unknown.value.reason == "outcome_unknown"  # ours is the lost one
    epoch = ledger.begin_owner_epoch(store, "next-owner").epoch
    queued = ledger.admit(
        store,
        epoch=epoch,
        principal_id="attach:cli",
        session_id="s1",
        request_id="r-mine",
        payload={"text": "second"},
    ).row
    with pytest.raises(RemoteRefusal) as blocked:
        await attached.wait("s1", queued.admission_id)
    assert blocked.value.reason == "blocked_by_unknown"  # ours waits behind the lost one
    assert attach.failure_reason(blocked.value) == "blocked_by_unknown"


def test_pending_in_an_empty_home_creates_nothing(scope: Scope) -> None:
    from indus.cli.attach_remote import _pending_rows  # pyright: ignore[reportPrivateUsage]

    assert _pending_rows(scope, {"s1"}) == []
    assert not any(scope.home.rglob("*.db"))


@pytest.mark.parametrize(
    ("verdict", "words"),
    [
        (Ensured("stopped", "planned_stop", "the gateway was stopped (stop)"), "planned_stop"),
        (Ensured("refused", "recover_budget", "restarted 3 times in 60 s"), "recover_budget"),
        (Ensured("exited", "exited", "exited before it answered\n  tail"), "exited before"),
        (
            Ensured("ready", endpoint=Endpoint(None, "p", "i", "v")),
            "no attach endpoint",
        ),
    ],
)
def test_an_unusable_daemon_is_attach_unavailable(
    scope: Scope, verdict: Ensured, words: str
) -> None:
    backend = DaemonBackend(ensure_fn=lambda *_a, **_k: verdict)
    with pytest.raises(attach.AttachUnavailable, match=words) as caught:
        backend.ensure(scope, timeout=1.0)
    assert "\n" not in str(caught.value)


def test_the_finite_reply_is_the_text_after_the_last_tool() -> None:
    from indus.cli.attach_remote import _Replies  # pyright: ignore[reportPrivateUsage]
    from indus.protocol.events import MessageDelta, ToolStarted, TurnStarted

    replies = _Replies()
    replies(TurnStarted(session_id="s", seq=1, turn_id="t1", generation=1))
    replies(MessageDelta(session_id="s", seq=2, turn_id="t1", text="Let me look. "))
    replies(ToolStarted(session_id="s", seq=3, turn_id="t1", call_id="c1", name="read"))
    replies(MessageDelta(session_id="s", seq=4, turn_id="t1", text="It is "))
    replies(MessageDelta(session_id="s", seq=5, turn_id="t1", text="fine."))
    assert replies.of("t1") == "It is fine."
    assert replies.of("t2") is None
    assert ExitReason.COMPLETED  # the turn's end comes from turn.done (RemoteTurn)
