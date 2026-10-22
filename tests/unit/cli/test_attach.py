"""P17-18: `indus`, `chat`, `-q` and `-z` attach to the profile's daemon by default.

The daemon side is the in-memory double of the CLI's attach seams (``attach_double``:
``StubBackend`` / ``StubRemote``), installed as ``attach.backend_factory``. Commands run through
the app's dispatch like a person types them. The real backend (``attach_remote.DaemonBackend``)
is tested in ``test_attach_remote.py`` and against a real daemon in
``tests/integration/cli/test_attach_daemon.py``.
"""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from click.testing import CliRunner

from indus.cli import app as app_mod
from indus.cli import attach, first_run
from indus.cli.args import ChatArgs, CliState, GlobalArgs, HostOverrides
from indus.cli.session_select import LaunchPolicy
from indus.foundation.scope import Scope
from indus.protocol.events import ExitReason, MessageDelta, TurnDone
from indus.protocol.server_requests import ApprovalAnswer, ApprovalChoice, ApprovalRequest
from indus.state import registry
from tests.fakes.agent import any_window
from tests.fakes.model import FakeModelServer, fake_route, script_of, text_turn
from tests.unit.cli.attach_double import RemoteFailure, StubBackend, StubRemote, turn_result
from tests.unit.state._helpers_d import add_session


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> StubBackend:
    stub = StubBackend()
    monkeypatch.setattr(attach, "backend_factory", lambda: stub)
    monkeypatch.setattr(first_run, "usable_provider", lambda _scope: "a test provider")
    return stub


class Ran:
    def __init__(self, code: int, out: str, err: str) -> None:
        self.code, self.out, self.err = code, out, err


def invoke(
    scope: Scope, *argv: str, tty: bool = False, overrides: HostOverrides | None = None
) -> Ran:
    state = CliState(
        scope=scope, overrides=overrides, stdin_isatty=lambda: tty, stdout_isatty=lambda: tty
    )
    with CliRunner().isolation() as (out, err, _mixed):
        code = app_mod.dispatch(list(argv), state)
    return Ran(code, out.getvalue().decode(), err.getvalue().decode())


def calls(remote: StubRemote, name: str) -> list[Any]:
    return [arg for kind, arg in remote.calls if kind == name]


# -- the route ------------------------------------------------------------------------------------


def test_local_and_driver_overrides_stay_in_process(scope: Scope) -> None:
    state = CliState(scope=scope)
    assert not attach.wants_local(state, GlobalArgs())
    assert attach.wants_local(state, GlobalArgs(local=True))
    assert attach.wants_local(CliState(scope=scope, overrides=HostOverrides()), GlobalArgs())


def test_local_flag_is_global_and_on_chat(scope: Scope, monkeypatch: pytest.MonkeyPatch) -> None:
    from indus.cli import chat as chat_mod

    seen: list[GlobalArgs] = []

    def fake(state: CliState, args: GlobalArgs, chat: ChatArgs) -> int:
        seen.append(args)
        return 0

    monkeypatch.setattr(chat_mod, "run_chat", fake)
    assert app_mod.dispatch(["chat", "--local"], CliState(scope=scope)) == 0
    assert seen[0].local is True
    assert app_mod.dispatch(["chat"], CliState(scope=scope)) == 0
    assert seen[1].local is False


def test_a_driver_with_its_own_model_never_ensures(scope: Scope, backend: StubBackend) -> None:
    server = FakeModelServer(script_of(text_turn("from this process")))
    overrides = HostOverrides(
        route=fake_route(server), client_factory=lambda _r: server.client(), list_models=any_window
    )
    ran = invoke(scope, "chat", "-q", "hi", "--oneshot", overrides=overrides)
    assert ran.code == 0, ran.err
    assert ran.out.strip() == "from this process"
    assert backend.ensured == []


def test_local_runs_in_process_and_never_ensures(
    scope: Scope, backend: StubBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    from indus.cli import chat as chat_mod

    ran_here: list[bool] = []

    class Here:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            ran_here.append(True)

        def run(self) -> int:
            return 0

    monkeypatch.setattr(chat_mod, "_ChatRun", Here)
    assert invoke(scope, "--local", "chat", "-q", "hi").code == 0
    assert ran_here == [True]
    assert backend.ensured == []


# -- refused flags --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "flag"),
    [
        (["-w", "chat", "-q", "x"], "-w/--worktree"),
        (["--no-restore-cwd", "-c"], "--no-restore-cwd"),
        (["chat", "--voice"], "--voice"),
        (["chat", "--save-trajectories", "-q", "x"], "--save-trajectories"),
        (["-w", "-z", "x"], "-w/--worktree"),
    ],
)
def test_each_refused_flag_exits_2_and_names_local(
    scope: Scope, backend: StubBackend, argv: list[str], flag: str
) -> None:
    ran = invoke(scope, *argv)
    assert ran.code == 2
    assert flag in ran.err and "--local" in ran.err and "nothing was started" in ran.err
    assert backend.ensured == []
    assert backend.remote.calls == []


def test_refused_flags_are_named_together() -> None:
    refused = attach.refused_flags(
        GlobalArgs(worktree="x", no_restore_cwd=True), ChatArgs(voice=True)
    )
    assert [flag for flag, _ in refused] == ["-w/--worktree", "--no-restore-cwd", "--voice"]
    assert attach.refused_flags(GlobalArgs(), ChatArgs()) == []


# -- finite runs ----------------------------------------------------------------------------------


def test_chat_q_attaches_by_default(scope: Scope, backend: StubBackend, tmp_path: Path) -> None:
    ran = invoke(scope, "chat", "-q", "say hello")
    assert ran.code == 0, ran.err
    assert ran.out == "hello from the daemon\n"
    assert "session_id: s-new-1" in ran.err
    assert backend.ensured == [scope.home]
    assert backend.surfaces == ["cli"]
    (created,) = calls(backend.remote, "create")
    assert created["source"] == "cli"
    assert created["policy"]["cwd"] == str(Path.cwd())
    assert calls(backend.remote, "submit")[0]["unattended"] is False
    assert backend.remote.closed


def test_z_attaches_by_default_and_prints_only_the_reply(
    scope: Scope, backend: StubBackend, tmp_path: Path
) -> None:
    usage = tmp_path / "u.json"
    ran = invoke(scope, "-z", "say hello", "--usage-file", str(usage), "-m", "m1", "-t", "web")
    assert ran.code == 0, ran.err
    assert ran.out == "hello from the daemon\n"
    assert ran.err == ""
    (submitted,) = calls(backend.remote, "submit")
    assert submitted == {"text": "say hello", "unattended": True, "turn": "adm-1"}
    policy = calls(backend.remote, "create")[0]["policy"]
    assert policy["model"] == "m1" and policy["toolsets"] == ["web"]
    import json

    report = json.loads(usage.read_text(encoding="utf-8"))
    assert report["completed"] is True and report["session_id"] == "s-new-1"


def test_launch_settings_the_daemon_cannot_take_yet_are_refused(
    scope: Scope, backend: StubBackend
) -> None:
    # Launch env vars become creation fields; the wire carries only cwd, model and toolsets for
    # now (P17-25), so anything else is refused, never silently dropped (R04.1).
    scoped = scope.with_env(scope.env.with_overlay({"INDUS_IGNORE_RULES": "1"}))
    ran = invoke(scoped, "-z", "hi")
    assert ran.code == 2
    assert "INDUS_IGNORE_RULES" in ran.err and "--local" in ran.err
    for argv in (["--yolo", "-z", "hi"], ["--provider", "openai", "chat", "-q", "hi"]):
        refused = invoke(scope, *argv)
        assert refused.code == 2, refused.err
        assert "cannot be sent to the gateway daemon" in refused.err
    assert backend.ensured == [] and backend.remote.calls == []


def test_ensure_failure_exits_1_and_runs_nothing_locally(
    scope: Scope, backend: StubBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    from indus.cli import runtime

    backend.ensure_error = RuntimeError("the daemon did not answer within 60 s")
    monkeypatch.setattr(runtime, "build_service", lambda *_a, **_k: pytest.fail("ran locally"))
    for argv in (["chat", "-q", "hi"], ["-z", "hi"]):
        ran = invoke(scope, *argv)
        assert ran.code == 1
        assert "did not answer within 60 s" in ran.err and "--local" in ran.err
        assert ran.out == ""


def test_a_refused_connection_exits_1_without_a_traceback(
    scope: Scope, backend: StubBackend
) -> None:
    backend.connect_error = ConnectionRefusedError("the upgrade was refused (4401)")
    for argv in (["chat", "-q", "hi"], ["-z", "hi"]):
        ran = invoke(scope, *argv)
        assert ran.code == 1
        assert "4401" in ran.err and "--local" in ran.err
    assert backend.remote.calls == []


def test_the_default_backend_is_the_shared_daemon_client() -> None:
    from indus.cli.attach_remote import DaemonBackend

    assert isinstance(attach.backend_factory(), DaemonBackend)


@pytest.mark.parametrize(
    ("result", "chat_code", "z_code"),
    [
        (lambda s, t: turn_result(s, t, "partial", failure="rate_limit"), 1, 2),
        (lambda s, t: turn_result(s, t, None), 0, 1),
        (lambda s, t: turn_result(s, t, "", reason=ExitReason.INTERRUPTED_BY_USER), 130, 130),
    ],
)
def test_finite_exit_codes_follow_the_in_process_tables(
    scope: Scope,
    backend: StubBackend,
    result: Callable[[str, str], Any],
    chat_code: int,
    z_code: int,
) -> None:
    backend.remote.wait_with = result
    assert invoke(scope, "chat", "-q", "hi").code == chat_code
    assert invoke(scope, "-z", "hi").code == z_code


def test_an_unknown_turn_blocks_a_finite_query_with_exit_3(
    scope: Scope, backend: StubBackend
) -> None:
    backend.remote.pending_rows = [
        {"admission_id": "adm-lost", "state": "unknown", "generation": 4, "text": "earlier"}
    ]
    for argv in (["chat", "-q", "hi"], ["-z", "hi"]):
        ran = invoke(scope, *argv)
        assert ran.code == 3
        assert "lost" in ran.err and "/discard" in ran.err
    assert calls(backend.remote, "submit") == []


def test_a_query_queued_behind_a_lost_owner_exits_3(scope: Scope, backend: StubBackend) -> None:
    def blocked(_s: str, _t: str) -> Any:
        raise RemoteFailure("blocked_by_unknown")

    backend.remote.wait_with = blocked
    ran = invoke(scope, "chat", "-q", "hi")
    assert ran.code == 3
    assert "nothing was sent" in ran.err
    assert len(calls(backend.remote, "submit")) == 1


def test_transport_lost_mid_turn_is_outcome_unknown_and_never_resent(
    scope: Scope, backend: StubBackend
) -> None:
    def lost(_s: str, _t: str) -> Any:
        raise RemoteFailure("outcome_unknown", "the connection dropped")

    backend.remote.wait_with = lost
    chat = invoke(scope, "chat", "-q", "hi")
    oneshot = invoke(scope, "-z", "hi")
    assert chat.code == 1 and oneshot.code == 2
    for ran in (chat, oneshot):
        assert "unknown" in ran.err and "do not send the input again" in ran.err
    assert len(calls(backend.remote, "submit")) == 2  # one per run, never a resend


def test_ctrl_c_interrupts_by_generation_and_waits_to_settle(
    scope: Scope, backend: StubBackend
) -> None:
    remote = backend.remote

    async def interrupted(session_id: str, turn_id: str) -> Any:
        import asyncio

        signal.raise_signal(signal.SIGINT)  # the person presses Ctrl-C while the turn runs
        await asyncio.to_thread(remote.released.wait, 10.0)
        return turn_result(session_id, turn_id, "", reason=ExitReason.INTERRUPTED_BY_USER)

    remote.wait_with = interrupted
    ran = invoke(scope, "chat", "-q", "hi")
    assert ran.code == 130
    assert calls(remote, "interrupt") == [1]
    assert "Detached" not in ran.err


def test_ctrl_c_on_a_queued_input_never_interrupts_another_surfaces_turn(
    scope: Scope, backend: StubBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(attach, "SETTLE_S", 0.2)
    remote = backend.remote
    remote.queued_behind = 7  # the TUI's turn 7 runs; ours waits behind it
    never = threading.Event()

    async def waiting(_s: str, _t: str) -> Any:
        import asyncio

        signal.raise_signal(signal.SIGINT)
        await asyncio.to_thread(never.wait, 10.0)

    remote.wait_with = waiting
    try:
        ran = invoke(scope, "chat", "-q", "hi")
    finally:
        never.set()
    assert ran.code == 130
    assert calls(remote, "interrupt") == []  # turn 7 is not ours
    assert calls(remote, "own_generation") == ["adm-1"]
    assert "Detached" in ran.err


def test_a_turn_that_does_not_settle_detaches(
    scope: Scope, backend: StubBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(attach, "SETTLE_S", 0.2)
    never = threading.Event()

    async def stuck(_s: str, _t: str) -> Any:
        import asyncio

        signal.raise_signal(signal.SIGINT)
        await asyncio.to_thread(never.wait, 10.0)

    backend.remote.wait_with = stuck
    try:
        ran = invoke(scope, "-z", "hi")
    finally:
        never.set()
    assert ran.code == 130
    assert "Detached" in ran.err and "goes on in the daemon" in ran.err


def _approval(session_id: str, request_id: str = "req-1") -> ApprovalRequest:
    return ApprovalRequest(
        request_id=request_id,
        session_id=session_id,
        command="rm -rf build",
        description="recursive delete",
    )


def test_a_question_nobody_can_answer_detaches_a_finite_run_with_exit_3(
    scope: Scope, backend: StubBackend
) -> None:
    remote = backend.remote
    done = threading.Event()

    async def asks(session_id: str, _turn: str) -> Any:
        import asyncio

        remote.ask(session_id, {"kind": "clarify"})
        await asyncio.to_thread(done.wait, 10.0)

    remote.wait_with = asks
    try:
        ran = invoke(scope, "chat", "-q", "clean up")
    finally:
        done.set()
    assert ran.code == 3
    assert "waiting for an answer" in ran.err and "indus -r s-new-1" in ran.err
    assert calls(remote, "interrupt") == []  # detached without cancelling


def _answered_run(scope: Scope, backend: StubBackend) -> Any:
    remote = backend.remote

    async def asks(session_id: str, turn_id: str) -> Any:
        import asyncio

        remote.ask(session_id, _approval(session_id))
        assert await asyncio.to_thread(remote.answered.wait, 10.0)
        return turn_result(session_id, turn_id, "I left the build folder alone.")

    remote.wait_with = asks
    return invoke(scope, "chat", "-q", "clean up")


def test_an_approval_in_a_finite_chat_is_refused_as_local_does(
    scope: Scope, backend: StubBackend
) -> None:
    """W46 F-5: with no terminal, attached ``chat -q`` refuses a risky command the way
    ``--local`` does (``approvals.single_query_mode: deny``) and the turn ends normally."""
    ran = _answered_run(scope, backend)
    assert ran.code == 0, ran.err
    assert "I left the build folder alone." in ran.out
    (answer,) = backend.remote.answers
    assert isinstance(answer, ApprovalAnswer)
    assert answer.choice == ApprovalChoice.DENY and answer.reason == attach.ONE_SHOT_DENY
    assert "approvals.single_query_mode" in attach.ONE_SHOT_DENY
    assert "waiting for an answer" not in ran.err


def test_single_query_mode_approve_approves_once_as_local_does(
    scope: Scope, backend: StubBackend
) -> None:
    (scope.home / "config.yaml").write_text(
        "approvals:\n  single_query_mode: approve\n", encoding="utf-8"
    )
    ran = _answered_run(scope, backend)
    assert ran.code == 0, ran.err
    (answer,) = backend.remote.answers
    assert isinstance(answer, ApprovalAnswer) and answer.choice == ApprovalChoice.ONCE


def test_an_interrupt_without_a_reply_says_it_was_not_run_again(
    scope: Scope, backend: StubBackend
) -> None:
    backend.remote.wait_with = lambda s, t: turn_result(
        s, t, "", reason=ExitReason.INTERRUPTED_BY_SYSTEM
    )
    ran = invoke(scope, "chat", "-q", "hi")
    assert ran.code == 130
    assert "not run again" in ran.err


# -- launch policy --------------------------------------------------------------------------------


def _seed_launched(scope: Scope, sid: str, **launch: Any) -> None:
    policy = LaunchPolicy(cwd=str(Path.cwd()), **launch)
    with registry.open_unshared(scope) as store:
        add_session(store, sid, title=f"{sid} talk", model_config={"launch": policy.record()})


def test_a_changed_frozen_flag_on_resume_exits_2(scope: Scope, backend: StubBackend) -> None:
    _seed_launched(scope, "s-old", model="m1")
    ran = invoke(scope, "-r", "s-old", "-m", "m2", "chat", "-q", "hi")
    assert ran.code == 2
    assert "different settings for --model" in ran.err
    assert backend.ensured == []
    same = invoke(scope, "-r", "s-old", "-m", "m1", "chat", "-q", "hi")
    assert same.code == 0, same.err
    (resumed,) = calls(backend.remote, "resume")
    assert resumed == {"session_id": "s-old", "policy": {"model": "m1"}}


def test_the_daemons_policy_conflict_exits_2(scope: Scope, backend: StubBackend) -> None:
    with registry.open_unshared(scope) as store:
        add_session(store, "s-old", title="made elsewhere")
    backend.remote.conflict = ("toolsets",)
    ran = invoke(scope, "-r", "s-old", "-t", "web", "-z", "hi")
    assert ran.code == 2
    assert "different settings for toolsets" in ran.err
    assert calls(backend.remote, "submit") == []


def test_a_bypass_launch_without_a_route_is_refused(scope: Scope, backend: StubBackend) -> None:
    ran = invoke(scope, "--safe-mode", "-z", "hi")
    assert ran.code == 2
    assert "--model and --provider" in ran.err
    assert backend.ensured == []
    named = invoke(scope, "--safe-mode", "-m", "m1", "--provider", "openai", "-z", "hi")
    assert named.code == 2  # a full route, but the daemon cannot take a bypass launch yet
    assert "cannot be sent to the gateway daemon" in named.err and "--local" in named.err
    assert backend.ensured == []


def test_resume_by_title_reads_the_store_read_only(
    scope: Scope, backend: StubBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    with registry.open_unshared(scope) as store:
        add_session(store, "s-old", title="old talk")
    registry.close_all()
    monkeypatch.setattr(registry, "acquire", lambda *_a, **_k: pytest.fail("opened for writing"))
    ran = invoke(scope, "-c", "old talk", "chat", "-q", "hi")
    assert ran.code == 0, ran.err
    assert calls(backend.remote, "resume")[0]["session_id"] == "s-old"


def test_a_missing_session_is_a_selection_error(scope: Scope, backend: StubBackend) -> None:
    ran = invoke(scope, "-r", "nothing-like-it", "chat", "-q", "hi")
    assert ran.code == 1
    assert "No session found matching 'nothing-like-it'" in ran.err
    assert backend.ensured == []


# -- interactive ----------------------------------------------------------------------------------


class FakeRepl:
    """The parts of ``Repl`` the attach hook and the launch use."""

    def __init__(self, session_id: str = "s-new-1", *, busy: bool = False) -> None:
        self.session_id = session_id
        self.busy = busy
        self.notes: list[str] = []
        self.errors: list[str] = []
        self.printed: list[str] = []
        self.sent: list[str] = []
        self.exited = False
        self.interrupts = 0

    def note(self, text: str) -> None:
        self.notes.append(text)

    def error(self, text: str) -> None:
        self.errors.append(text)

    def print(self, renderable: Any, *, style: str = "") -> None:
        self.printed.append(str(renderable))

    def request_exit(self) -> None:
        self.exited = True

    def interrupt(self) -> bool:
        self.interrupts += 1
        return self.busy

    def post(self, fn: Callable[[], Any]) -> None:
        fn()

    def send(self, text: str) -> None:
        self.sent.append(text)


@pytest.fixture
def host(scope: Scope) -> Iterator[tuple[attach.AttachedHost, StubRemote]]:
    stub = StubBackend()
    attached = attach.AttachedHost(stub, scope, object()).start()
    try:
        yield attached, stub.remote
    finally:
        attached.stop()


def test_discard_with_a_stale_generation_is_refused(
    host: tuple[attach.AttachedHost, StubRemote],
) -> None:
    attached, remote = host
    remote.pending_rows = [
        {"admission_id": "adm-9", "state": "unknown", "generation": 3, "current_generation": 4},
        {"admission_id": "adm-10", "state": "unknown", "generation": 5},
        {"admission_id": "adm-11", "state": "queued", "generation": 0},
    ]
    hook = attach.attached_commands(attached)
    repl = FakeRepl()
    assert hook(cast(Any, repl), "/discard adm-9")
    assert "stale generation" in repl.errors[-1]
    assert calls(remote, "discard") == [("adm-9", 3)]
    assert hook(cast(Any, repl), "/discard adm-11")  # queued, not lost: nothing sent
    assert "not a lost turn" in repl.errors[-1]
    assert hook(cast(Any, repl), "/discard")
    assert "usage" in repl.errors[-1]
    assert hook(cast(Any, repl), "/discard adm-10")
    assert repl.notes[-1].startswith("discarded adm-10")
    assert calls(remote, "discard") == [("adm-9", 3), ("adm-10", 5)]


def test_the_slash_subset(host: tuple[attach.AttachedHost, StubRemote]) -> None:
    attached, _remote = host
    hook = attach.attached_commands(attached)
    repl = FakeRepl(busy=True)
    assert hook(cast(Any, repl), "/stop") and repl.interrupts == 1
    assert hook(cast(Any, repl), "/help") and "/discard" in repl.printed[-1]
    assert hook(cast(Any, repl), "/model gpt-x")
    assert "not available while attached" in repl.errors[-1] and "--local" in repl.errors[-1]
    assert hook(cast(Any, repl), "!ls")
    assert "--local" in repl.errors[-1]
    assert not hook(cast(Any, repl), "plain text goes to the model")
    assert not repl.exited
    assert hook(cast(Any, repl), "/detach") and repl.exited


def test_the_feed_carries_remote_events_and_detaches(
    host: tuple[attach.AttachedHost, StubRemote],
) -> None:
    attached, remote = host
    feed, detach = attached.feed("s-1")
    ticket = attached.submit("s-1", "hello")
    remote.emit("s-1", ticket.turn_id, "hi there")
    items = [feed.get(timeout=5.0) for _ in range(3)]
    assert [type(i).__name__ for i in items] == ["TurnStarted", "MessageDelta", "TurnDone"]
    assert isinstance(items[1], MessageDelta) and items[1].text == "hi there"
    assert isinstance(items[2], TurnDone)
    assert attached.interrupt("s-1", 1).interrupted
    detach()
    assert ("unsubscribe", "s-1") in remote.calls


def test_interactive_chat_runs_the_repl_over_the_daemon_and_only_detaches(
    scope: Scope, backend: StubBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    from indus.cli.repl import app as repl_app

    backend.remote.pending_rows = [
        {"admission_id": "adm-7", "state": "unknown", "generation": 2, "text": "lost words"}
    ]
    seen: dict[str, Any] = {}

    def fake_run_repl(host: Any, session_id: str, **kw: Any) -> int:
        repl = FakeRepl(session_id)
        kw["on_ready"](repl)
        seen.update(session=session_id, notes=repl.notes, sent=repl.sent, host=host)
        ticket = host.submit(session_id, "typed")
        seen["generation"] = ticket.generation
        return 0

    monkeypatch.setattr(repl_app, "run_repl", fake_run_repl)
    ran = invoke(scope, "chat", "-q", "seeded words", tty=True)
    assert ran.code == 0, ran.err
    assert seen["session"] == "s-new-1"
    assert seen["sent"] == ["seeded words"]
    assert any("/discard adm-7" in note for note in seen["notes"])
    assert any("--local" in note for note in seen["notes"])
    assert isinstance(seen["host"], attach.AttachedHost)
    assert calls(backend.remote, "interrupt") == []  # leaving never stops the daemon's work
    assert backend.remote.closed


def test_failure_reasons_are_read_from_errors_and_answers() -> None:
    assert attach.failure_reason(RemoteFailure("outcome_unknown")) == "outcome_unknown"

    class WithData(Exception):
        data = {"reason": "policy_conflict"}

    assert attach.failure_reason(WithData()) == "policy_conflict"
    assert attach.failure_reason(object()) is None
