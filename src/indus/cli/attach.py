"""Classic chat and one-shots as clients of the profile's daemon (P17-18, R04.1, D-16.10).

By default ``indus``, ``indus chat``, ``chat -q`` and ``indus -z`` do not run an agent here. They
make sure the profile's gateway daemon runs (``ensure``, P17-15), attach to it (P17-16) and drive
its sessions through a remote session service (P17-17): the daemon runs the tools and writes the
store, this process shows the events and sends what the person types. ``--local`` keeps the
in-process route of P06 unchanged; so does a run whose driver injects its own model client
(``HostOverrides``, test drivers only), which no daemon could use.

Attach mode rules:

- **Refused flags** (exit 2 before anything starts; nothing falls back to a local agent):
  ``-w/--worktree``, ``--no-restore-cwd``, ``--voice``, ``--save-trajectories`` — each needs this
  process; the message names ``--local`` and where the capability lives.
- **Launch policy**: a new session sends its creation policy (P17-09; launch env vars become
  fields); a bypass launch (safe mode, ignore-user-config) must name model and provider (exit 2).
  A resume sends only the flags given; a different frozen value is refused (exit 2), first by a
  read-only check here, then by the daemon (``policy_conflict``).
- **Selection** (``-c``, ``-r``, ``latest``, the breadcrumb) reads the store read-only.
- **Finite runs** print as the in-process tables do (``oneshot.finite_exit_code`` /
  ``oneshot_exit_code``). A session paused behind a lost turn (an ``unknown`` admission), or a
  query that ends up queued behind one, exits **3** and nothing is resent. A transport lost while
  our own turn ran is "outcome unknown": never resubmitted (exit 1 for ``chat -q``, 2 for ``-z``).
  Ctrl-C interrupts the turn by its generation and waits up to ``SETTLE_S`` (10 s) for it to
  settle, then exits 130. A finite ``chat -q`` (``--oneshot``, ``-Q`` or no terminal) answers an
  approval request the way ``--local`` decides one in a one-shot run: refused, with the same
  reason, unless ``approvals.single_query_mode`` is ``approve`` (then approved once); the turn goes
  on and ends normally (W46 F-5). Any other question (clarify, sudo, a secret) still detaches
  with exit 3 and the resume hint.
- **Interactive**: the classic REPL and its one renderer (``repl.app.run_repl``) over
  ``AttachedHost``. Slash subset: ``/help``, ``/quit`` (``/exit``, ``/detach``), ``/stop``,
  ``/discard <admission_id>`` (resolves a lost turn; its generation is sent and checked). Every
  other slash or ``!`` line is answered "not available while attached". Leaving only detaches: the
  daemon finishes accepted work. Lost turns are listed at start and again after any gap in the
  session's events (a reconnect to a daemon that restarted mid-turn): outcome unknown, never resent.

``AttachBackend`` / ``AttachedService`` are the calls this module needs from the shared-daemon
client; ``attach_remote.DaemonBackend`` is the real one (P17-15 ensure, P17-16 channel, P17-17
remote session service). ``session.create`` carries the creation policy's ``cwd``, ``model`` and
``toolsets`` today: a new session that names any other frozen field (``--provider``,
``--reasoning``, the switches and their env vars, ``--max-turns``, ``-s``) is refused with exit 2
and a message naming ``--local`` (never silently dropped) until P17-25 puts the policy on the
wire.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import logging
import queue
import threading
import time
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol, Self

import click

from indus.cli.args import ChatArgs, CliState, GlobalArgs, split_csv
from indus.protocol.events import ExitReason
from indus.protocol.methods import (
    PromptSubmitResult,
    ProtocolError,
    ServerRequestAnswerResult,
    SessionCreateParams,
    SessionInterruptResult,
    SessionRedirectResult,
    SessionSteerResult,
    SubmitStatus,
)
from indus.protocol.server_requests import ApprovalRequest

if TYPE_CHECKING:
    from indus.cli.oneshot import RunOutcome
    from indus.cli.repl.app import Repl
    from indus.foundation.scope import Scope
    from indus.protocol.requests_port import AnswerModel
    from indus.service.creation_policy import CreationPolicy

__all__ = [
    "BLOCKED_EXIT",
    "ONE_SHOT_DENY",
    "REFUSED_FLAGS",
    "SETTLE_S",
    "AttachBackend",
    "AttachUnavailable",
    "AttachedHost",
    "AttachedService",
    "attached_commands",
    "backend_factory",
    "failure_reason",
    "one_shot_answer",
    "refused_flags",
    "run_attached_chat",
    "run_attached_oneshot",
    "wants_local",
]

log = logging.getLogger(__name__)

ENSURE_TIMEOUT_S: Final = 60.0
SETTLE_S: Final = 10.0
"""How long Ctrl-C waits for the interrupted turn to settle before detaching."""
CALL_TIMEOUT_S: Final = 120.0
BLOCKED_EXIT: Final = 3
"""A finite query that cannot run because the session waits behind a lost turn."""

REFUSED_FLAGS: Final[tuple[tuple[str, str, str], ...]] = (
    ("worktree", "-w/--worktree", "a new worktree is made in this terminal's repository"),
    (
        "no_restore_cwd",
        "--no-restore-cwd",
        "the daemon keeps a session's own folder (give --in DIR for a new session)",
    ),
    ("voice", "--voice", "recording and speech-to-text run in this terminal"),
    (
        "save_trajectories",
        "--save-trajectories",
        "the samples are written by an agent running in this process",
    ),
)
"""``(field, flag as typed, why it needs this process)``; the field is on ``GlobalArgs`` or
``ChatArgs``."""

_BYPASS_EXAMPLE = 'indus --safe-mode --provider openrouter --model <model> -q "hello"'
_UNKNOWN_HINT = (
    "Resume this session in a terminal and /discard the lost turn (its outcome is unknown); "
    "do not send the input again."
)
_HELP = (
    "Attached to the gateway daemon. Commands here: /stop (interrupt the reply), "
    "/discard <admission_id> (resolve a turn lost in a daemon restart), /quit or /detach (leave; "
    "accepted work goes on), /help. Approvals and questions are answered in place. Everything "
    "else runs with `indus --local`."
)


class AttachUnavailable(Exception):
    """The daemon could not be reached or started; nothing ran in this process."""


class AttachedService(Protocol):
    """What the CLI calls on the remote session service (methods may be sync or async)."""

    def create(
        self, params: SessionCreateParams, *, source: str, policy: Mapping[str, Any]
    ) -> Any: ...
    def resume(self, session_id: str, *, policy: Mapping[str, Any] | None) -> Any: ...
    def submit(self, session_id: str, text: str, *, unattended: bool = False) -> Any: ...
    def wait(self, session_id: str, turn_id: str) -> Any: ...
    def interrupt(self, session_id: str, generation: int | None, *, hard: bool = False) -> Any: ...
    def steer(self, session_id: str, text: str) -> Any: ...
    def redirect(self, session_id: str, text: str) -> Any: ...
    def answer_server_request(
        self, session_id: str, request_id: str, answer: Mapping[str, Any] | AnswerModel
    ) -> Any: ...
    def subscribe(self, session_id: str, listener: Callable[[Any], None]) -> Any: ...
    def on_request(self, session_id: str, listener: Callable[[Any], None]) -> Any: ...
    def pending(self, session_id: str) -> Any: ...
    def discard_unknown(self, session_id: str, admission_id: str, generation: int) -> Any: ...
    def own_generation(self, session_id: str, turn_id: str) -> int | None: ...
    def aclose(self) -> Any: ...


class AttachBackend(Protocol):
    """How the CLI reaches the daemon: ``ensure`` it, then ``connect`` a remote service."""

    def ensure(self, scope: Scope, *, timeout: float) -> Any: ...
    def connect(self, scope: Scope, endpoint: Any, *, surface: str) -> Awaitable[Any]: ...


def _default_backend() -> AttachBackend:
    from indus.cli.attach_remote import DaemonBackend

    return DaemonBackend()


backend_factory: Callable[[], AttachBackend] = _default_backend
"""Builds the backend for a run (tests put a stand-in here)."""


# -- small rules ----------------------------------------------------------------------------------


def wants_local(state: CliState, args: GlobalArgs) -> bool:
    """``--local``, or a driver that injected its own model client (no daemon could use it)."""
    return args.local or state.overrides is not None


def refused_flags(args: GlobalArgs, chat: ChatArgs | None = None) -> list[tuple[str, str]]:
    """``(flag, why)`` for each given flag that needs this process."""
    out: list[tuple[str, str]] = []
    for name, flag, why in REFUSED_FLAGS:
        value = getattr(args, name, None)
        if value is None and chat is not None:
            value = getattr(chat, name, None)
        if value not in (None, False):
            out.append((flag, why))
    return out


def _refusal_lines(prefix: str, refused: Sequence[tuple[str, str]]) -> list[str]:
    flags = ", ".join(flag for flag, _ in refused)
    lines = [
        f"{prefix}: {flags} cannot be used while attached to the gateway daemon; nothing was "
        "started. Run with --local to use an agent in this terminal:"
    ]
    lines.extend(f"  {flag}: {why}" for flag, why in refused)
    return lines


def failure_reason(obj: object) -> str | None:
    """The short reason a remote refusal or result carries (``outcome_unknown``,
    ``blocked_by_unknown``, ``policy_conflict``, ``stale_generation`` ...), or None."""
    for name in ("reason", "code_name", "code"):
        value = getattr(obj, name, None)
        if isinstance(value, str) and value:
            return value
    data = getattr(obj, "data", None)
    if isinstance(data, Mapping):
        value = data.get("reason")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if isinstance(value, str) and value:
            return value
    return None


def _message(exc: BaseException) -> str:
    text = getattr(exc, "message", None)
    return text if isinstance(text, str) and text else str(exc) or type(exc).__name__


async def _settle(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


@dataclass(frozen=True)
class PendingRow:
    """One not-yet-finished admission of the session, as the daemon lists it."""

    admission_id: str
    state: str
    generation: int
    text: str = ""

    @classmethod
    def of(cls, raw: Any) -> PendingRow:
        get: Callable[[str], Any] = (
            raw.get if isinstance(raw, Mapping) else (lambda k: getattr(raw, k, None))  # pyright: ignore[reportUnknownMemberType]
        )
        generation = get("generation")
        return cls(
            str(get("admission_id") or ""),
            str(get("state") or ""),
            generation if isinstance(generation, int) else 0,
            str(get("text") or ""),
        )


def _policy_wire(policy: CreationPolicy) -> dict[str, Any]:
    return {k: list(v) if isinstance(v, tuple) else v for k, v in policy.given().items()}


def _creation_policy(state: CliState, args: GlobalArgs, chat: ChatArgs, cwd: Path) -> Any:
    from indus.service.creation_policy import CreationPolicy, check_launch, launch_flags_from_env

    launch: dict[str, Any] = {
        "cwd": str(cwd),
        "model": args.model,
        "provider": args.provider,
        "reasoning": args.reasoning,
        "toolsets": split_csv(args.toolsets),
        "max_turns": chat.max_turns,
        "skills": split_csv(list(args.skills)) or [],
        "checkpoints": args.checkpoints,
        "yolo": args.yolo,
        "accept_hooks": args.accept_hooks,
        "ignore_rules": args.ignore_rules,
        "safe_mode": args.safe_mode,
        "ignore_user_config": args.ignore_user_config,
        "pass_session_id": args.pass_session_id,
    }
    launch.update(launch_flags_from_env(state.scope.env))
    policy = CreationPolicy.from_launch(launch)
    check_launch(policy)
    return policy


def _resume_policy(args: GlobalArgs, chat: ChatArgs, cwd: Path) -> dict[str, Any] | None:
    from indus.cli.session_select import launch_from_args

    _policy, given = launch_from_args(args, chat, cwd=cwd)
    wire: dict[str, Any] = {k: list(v) if isinstance(v, tuple) else v for k, v in given.items()}
    if args.checkpoints:
        wire["checkpoints"] = True
    if args.accept_hooks:
        wire["accept_hooks"] = True
    return wire or None


# -- the host the REPL drives ---------------------------------------------------------------------


class AttachedHost:
    """The remote service on its own loop thread, offered as blocking calls (a ``ReplHost``)."""

    def __init__(
        self,
        backend: AttachBackend,
        scope: Scope,
        endpoint: Any,
        *,
        surface: str = "cli",
        call_timeout: float | None = CALL_TIMEOUT_S,
    ) -> None:
        self.backend = backend
        self.scope = scope
        self.endpoint = endpoint
        self.surface = surface
        self.call_timeout = call_timeout
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._service: Any = None
        self._stopped = False

    def start(self) -> Self:
        self._thread = threading.Thread(target=self._run, name="indus-attach", daemon=True)
        self._thread.start()
        if not self._ready.wait(10.0):
            raise AttachUnavailable("the attach loop did not start")
        try:
            self._service = self.call(
                self.backend.connect(self.scope, self.endpoint, surface=self.surface)
            )
        except Exception as exc:
            self.stop()
            if isinstance(exc, AttachUnavailable):
                raise
            raise AttachUnavailable(_message(exc)) from exc
        return self

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        loop.call_soon(self._ready.set)
        try:
            loop.run_forever()
        finally:
            loop.close()

    @property
    def service(self) -> Any:
        if self._service is None:
            raise RuntimeError("the attached host is not connected")
        return self._service

    def future(self, name: str, *args: Any, **kwargs: Any) -> concurrent.futures.Future[Any]:
        """``service.<name>(...)`` started on the loop (sync or async alike)."""
        service = self.service

        async def step() -> Any:
            return await _settle(getattr(service, name)(*args, **kwargs))

        return self._schedule(step())

    def _schedule(self, coro: Coroutine[Any, Any, Any]) -> concurrent.futures.Future[Any]:
        loop = self._loop
        if loop is None or loop.is_closed():
            coro.close()
            raise RuntimeError("the attached host is not running")
        return asyncio.run_coroutine_threadsafe(coro, loop)

    def call(self, coro: Awaitable[Any], *, timeout: float | None = -1.0) -> Any:
        async def run() -> Any:
            return await coro

        fut = self._schedule(run())
        wait = self.call_timeout if timeout == -1.0 else timeout
        try:
            return fut.result(wait)
        except TimeoutError:
            fut.cancel()
            raise

    def invoke(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """Call ``service.<name>(...)`` on the loop (sync or async alike) and wait for it."""
        fut = self.future(name, *args, **kwargs)
        try:
            return fut.result(self.call_timeout)
        except TimeoutError:
            fut.cancel()
            raise

    def stop(self) -> None:
        """Detach (never interrupt) and stop the loop."""
        if self._stopped or self._thread is None:
            return
        self._stopped = True
        try:
            if self._service is not None:
                self.invoke("aclose")
        except Exception:
            log.warning("detaching from the daemon failed", exc_info=True)
        finally:
            loop = self._loop
            if loop is not None and not loop.is_closed():
                loop.call_soon_threadsafe(loop.stop)
            self._thread.join(10.0)

    # -- ReplHost ---------------------------------------------------------------------------------

    def submit(self, session_id: str, text: str) -> PromptSubmitResult:
        return self.invoke("submit", session_id, text)

    def interrupt(
        self, session_id: str, generation: int | None = None, *, hard: bool = False
    ) -> SessionInterruptResult:
        return self.invoke("interrupt", session_id, generation, hard=hard)

    def steer(self, session_id: str, text: str) -> SessionSteerResult:
        return self.invoke("steer", session_id, text)

    def redirect(self, session_id: str, text: str) -> SessionRedirectResult:
        return self.invoke("redirect", session_id, text)

    def answer_server_request(
        self, session_id: str, request_id: str, answer: Mapping[str, Any] | AnswerModel
    ) -> ServerRequestAnswerResult:
        return self.invoke("answer_server_request", session_id, request_id, answer)

    def feed(self, session_id: str) -> tuple[queue.Queue[Any], Callable[[], None]]:
        feed: queue.Queue[Any] = queue.Queue()

        async def attach() -> Callable[[], Awaitable[None]]:
            off_events = await _settle(self.service.subscribe(session_id, feed.put))
            off_requests = await _settle(self.service.on_request(session_id, feed.put))

            async def detach() -> None:
                for off in (off_events, off_requests):
                    if callable(off):
                        await _settle(off())

            return detach

        detach = self.call(attach())
        return feed, lambda: self.call(detach())

    # -- the rest ---------------------------------------------------------------------------------

    def pending(self, session_id: str) -> list[PendingRow]:
        rows = self.invoke("pending", session_id) or []
        return [PendingRow.of(row) for row in rows]

    def on_gap(self, session_id: str, listener: Callable[[Mapping[str, Any]], object]) -> None:
        """Call ``listener`` (on the attach loop) when the session's event stream has a gap — as
        after a reconnect to a daemon that restarted. A service without gaps never calls it."""

        async def register() -> None:
            hook = getattr(self.service, "on_gap", None)
            if callable(hook):
                await _settle(hook(session_id, listener))

        self.call(register())


# -- opening the session --------------------------------------------------------------------------


@dataclass
class _Plan:
    """What the run attaches to: an existing session (and its row) or a new one's policy."""

    session_id: str | None
    row: Mapping[str, Any] | None
    create_policy: dict[str, Any] | None
    resume_policy: dict[str, Any] | None
    cwd: Path


def _select(state: CliState, args: GlobalArgs, cwd: Path) -> tuple[str | None, Any]:
    """The session to resume (read-only) and its row; raises ``SelectionError`` on a miss."""
    from indus.cli import resume, session_select
    from indus.cli.commands.sessions import read_view
    from indus.state import sessions

    if args.resume is None and args.continue_ is None:
        return None, None
    latest = (args.resume or "").strip().lower() == session_select.LATEST or (
        args.resume is None and args.continue_ == session_select.BARE_CONTINUE
    )
    crumb = resume.read_breadcrumb(state.scope) if latest else None
    with read_view(state.scope) as view:
        if view is None:
            wanted = args.resume or args.continue_
            if latest:
                raise session_select.SelectionError("No earlier session to continue here.")
            raise session_select.SelectionError(f"No session found matching '{wanted}'.")
        handle = view.as_handle()
        sid = session_select.select_session(handle, args, cwd=cwd, breadcrumb=crumb)
        row = sessions.get(handle, sid) if sid is not None else None
    return sid, row


def _plan(
    state: CliState, args: GlobalArgs, chat: ChatArgs, prefix: str, err: Callable[[str], None]
) -> _Plan | int:
    """Select the session and build its policy; an int is the exit code (already explained)."""
    from indus.cli import session_select
    from indus.cli.commands.sessions import StoreProblem
    from indus.service.creation_policy import PolicyRefused

    if args.in_dir is not None:
        try:
            session_select.enter_in_dir(args.in_dir)
        except session_select.SelectionError as exc:
            for line in exc.lines():
                err(line)
            return exc.code
    cwd = Path.cwd()
    try:
        sid, row = _select(state, args, cwd)
    except session_select.SelectionError as exc:
        for line in exc.lines():
            err(line)
        return exc.code
    except StoreProblem as exc:
        err(exc.format_message())
        return 1
    if sid is None:
        try:
            policy = _creation_policy(state, args, chat, cwd)
        except PolicyRefused as exc:
            if exc.reason == "bypass_needs_route":
                err(
                    f"{prefix}: --safe-mode / --ignore-user-config read no profile defaults, so "
                    f"a new session needs --model and --provider too. Example: {_BYPASS_EXAMPLE}"
                )
            else:
                err(f"{prefix}: this launch cannot be used: {exc.message}")
            return 2
        wire = _policy_wire(policy)
        uncarried = [name for name in wire if name not in CARRIED_POLICY]
        if uncarried:
            _uncarried(prefix, uncarried, err)
            return 2
        return _Plan(None, None, wire, None, cwd)
    stored = session_select.stored_launch(row)
    _launch, given = session_select.launch_from_args(args, chat, cwd=cwd)
    if stored is not None:
        conflicts = session_select.check_frozen(stored, given)
        if conflicts:
            _conflict(prefix, conflicts, err)
            return 2
    return _Plan(sid, row, None, _resume_policy(args, chat, cwd), cwd)


CARRIED_POLICY: Final = frozenset({"cwd", "model", "toolsets"})
"""Creation-policy fields ``session.create`` carries to the daemon today."""
_POLICY_FLAGS: Final[Mapping[str, str]] = {
    "provider": "--provider",
    "reasoning": "--reasoning",
    "max_turns": "--max-turns",
    "skills": "-s/--skills",
    "checkpoints": "--checkpoints",
    "yolo": "--yolo (or INDUS_YOLO_MODE)",
    "accept_hooks": "--accept-hooks (or INDUS_ACCEPT_HOOKS)",
    "ignore_rules": "--ignore-rules (or INDUS_IGNORE_RULES)",
    "safe_mode": "--safe-mode (or INDUS_SAFE_MODE)",
    "ignore_user_config": "--ignore-user-config (or INDUS_IGNORE_USER_CONFIG)",
    "pass_session_id": "--pass-session-id",
}


def _uncarried(prefix: str, names: Sequence[str], err: Callable[[str], None]) -> None:
    flags = ", ".join(_POLICY_FLAGS.get(name, name) for name in names)
    err(
        f"{prefix}: {flags} cannot be sent to the gateway daemon yet; nothing was started. "
        "Run with --local to use these settings with an agent in this terminal."
    )


def _conflict(prefix: str, names: Sequence[str], err: Callable[[str], None]) -> None:
    err(
        f"{prefix}: this session was started with different settings for {', '.join(names)}; "
        "a resumed session keeps its own."
    )
    err("Drop those flags, or start a new session to change them.")


def _ensure(backend: AttachBackend, scope: Scope) -> Any:
    try:
        return backend.ensure(scope, timeout=ENSURE_TIMEOUT_S)
    except AttachUnavailable:
        raise
    except Exception as exc:
        raise AttachUnavailable(_message(exc)) from exc


def _unreachable(prefix: str, exc: BaseException, err: Callable[[str], None]) -> int:
    err(
        f"{prefix}: could not attach to this profile's gateway daemon: {_message(exc)}. "
        "Nothing ran in this terminal; run with --local to use an agent here."
    )
    return 1


def _open(host: AttachedHost, plan: _Plan, source: str) -> str:
    """Create or resume on the daemon; returns the session id (``ProtocolError`` and policy
    refusals propagate)."""
    if plan.session_id is None:
        params = SessionCreateParams(cwd=str(plan.cwd))
        created = host.invoke("create", params, source=source, policy=plan.create_policy or {})
        return str(created.session_id)
    resumed = host.invoke("resume", plan.session_id, policy=plan.resume_policy)
    return str(getattr(resumed, "session_id", None) or plan.session_id)


# -- finite runs ----------------------------------------------------------------------------------


_SLICE_S: Final = 0.1


class _NeedsInput(Exception):
    """The daemon asked a question nobody at this finite run can answer."""


def _result(
    fut: concurrent.futures.Future[Any],
    timeout: float | None,
    asked: threading.Event | None = None,
) -> Any:
    """``fut.result(timeout)`` in short slices: a Ctrl-C the OS hands to another thread is only
    noticed when this (main) thread runs Python again. ``asked`` set → ``_NeedsInput``."""
    end = None if timeout is None else time.monotonic() + timeout
    while True:
        if asked is not None and asked.is_set() and not fut.done():
            raise _NeedsInput
        left = _SLICE_S if end is None else min(_SLICE_S, end - time.monotonic())
        if left <= 0:
            raise TimeoutError
        try:
            return fut.result(left)
        except TimeoutError:
            if fut.done():
                raise
            continue


def _own_generation(host: AttachedHost, session_id: str, ticket: PromptSubmitResult) -> int | None:
    """The generation of the turn our input started (Ctrl-C stops that turn, never one another
    surface started): the submit's own when it started at once, else the one its turn started
    with since, or None while it is still queued."""
    if ticket.status is SubmitStatus.STREAMING and ticket.generation:
        return ticket.generation
    try:
        return host.invoke("own_generation", session_id, ticket.turn_id)
    except Exception:
        log.warning("could not tell which turn our input started", exc_info=True)
        return None


@dataclass
class _Finite:
    outcome: RunOutcome
    blocked: bool = False
    needs_input: bool = False
    unknown: bool = False
    detached: bool = False
    failure: str | None = None


ONE_SHOT_DENY: Final = (
    "BLOCKED: a one-shot run has nobody present to approve it. Nothing ran. Find another way "
    "that avoids this command; to allow such commands here the user sets "
    "approvals.single_query_mode: approve."
)
"""The reason a finite attached ``chat -q`` gives the model with its refusal (as ``--local``)."""


def one_shot_answer(scope: Scope) -> AnswerModel:
    """How a finite ``chat -q`` answers an approval nobody can see: ``--local``'s single-query
    rule (``approvals.single_query_mode``, deny unless set to approve)."""
    from indus.foundation.config import loader
    from indus.protocol.server_requests import ApprovalAnswer, ApprovalChoice
    from indus.safety.approvals.decision import setting

    read = scope if scope.config is not None else scope.with_config(loader.load(scope))
    if str(setting(read, "approvals.single_query_mode")).strip().lower() == "approve":
        return ApprovalAnswer(choice=ApprovalChoice.ONCE)
    return ApprovalAnswer(choice=ApprovalChoice.DENY, reason=ONE_SHOT_DENY)


def _answer_unseen(
    host: AttachedHost, sid: str, requests: queue.SimpleQueue[Any], approve: AnswerModel | None
) -> bool:
    """Answer the approvals waiting in ``requests`` with ``approve``; False when one of them is
    a question this run cannot answer (or no rule was given)."""
    while True:
        try:
            request = requests.get_nowait()
        except queue.Empty:
            return True
        if approve is None or not isinstance(request, ApprovalRequest):
            return False
        host.answer_server_request(sid, request.request_id, approve)


def _wait_answering(
    host: AttachedHost,
    sid: str,
    waiting: concurrent.futures.Future[Any],
    asked: threading.Event,
    requests: queue.SimpleQueue[Any],
    approve: AnswerModel | None,
) -> Any:
    """The turn's result, answering approvals by ``approve`` meanwhile; ``_NeedsInput`` when a
    request comes that this run cannot answer."""
    while True:
        try:
            return _result(waiting, None, asked)
        except _NeedsInput:
            asked.clear()  # before draining: a request offered meanwhile sets it again
            if not _answer_unseen(host, sid, requests, approve):
                raise


def _run_finite(
    host: AttachedHost,
    plan: _Plan,
    text: str,
    *,
    source: str,
    unattended: bool,
    approve: AnswerModel | None = None,
) -> _Finite:
    from indus.cli.oneshot import RunOutcome

    run = _Finite(RunOutcome())
    outcome = run.outcome
    sid = _open(host, plan, source)
    outcome.session_id = sid
    if any(row.state == "unknown" for row in host.pending(sid)):
        run.blocked = True
        return run
    asked = threading.Event()
    requests: queue.SimpleQueue[Any] = queue.SimpleQueue()

    def offered(request: Any) -> None:
        requests.put(request)
        asked.set()

    host.invoke("on_request", sid, offered)
    try:
        ticket = host.invoke("submit", sid, text, unattended=unattended)
    except Exception as exc:
        if failure_reason(exc) != "outcome_unknown":
            raise
        outcome.started = True
        run.unknown = True
        outcome.failure = "the outcome of this input is unknown (the connection was lost)"
        return run
    outcome.started = True
    waiting = host.future("wait", sid, ticket.turn_id)
    try:
        result = _wait_answering(host, sid, waiting, asked, requests, approve)
    except _NeedsInput:
        waiting.cancel()
        run.needs_input = True
        return run
    except KeyboardInterrupt:
        outcome.interrupted = True
        try:
            generation = _own_generation(host, sid, ticket)
            if generation is None:
                raise TimeoutError  # still queued: nothing of ours runs to interrupt
            host.invoke("interrupt", sid, generation)
            result = _result(waiting, SETTLE_S)
        except (TimeoutError, KeyboardInterrupt):
            waiting.cancel()
            run.detached = True
            return run
    except Exception as exc:
        reason = failure_reason(exc)
        if reason == "blocked_by_unknown":
            run.blocked = True
            return run
        if reason == "outcome_unknown":
            run.unknown = True
            outcome.failure = "the outcome of this turn is unknown (the daemon lost it)"
            return run
        raise
    outcome.session_id = getattr(result, "session_id", None) or sid
    outcome.exit_reason = result.exit_reason
    outcome.final_text = result.final_text
    outcome.api_calls = result.api_calls
    if result.exit_reason in (ExitReason.INTERRUPTED_BY_USER, ExitReason.INTERRUPTED_BY_SYSTEM):
        outcome.interrupted = True
    elif result.failure is not None:
        outcome.failure = result.failure.reason
    return run


def _finite_session(
    state: CliState,
    plan: _Plan,
    text: str,
    *,
    source: str,
    unattended: bool,
    prefix: str,
    err: Callable[[str], None],
    approve: AnswerModel | None = None,
) -> _Finite | int:
    backend = backend_factory()
    try:
        endpoint = _ensure(backend, state.scope)
        host = AttachedHost(backend, state.scope, endpoint).start()
    except AttachUnavailable as exc:
        return _unreachable(prefix, exc, err)
    try:
        return _run_finite(host, plan, text, source=source, unattended=unattended, approve=approve)
    except ProtocolError as exc:
        err(f"{prefix}: {exc.message}")
        return 1
    except Exception as exc:
        reason = failure_reason(exc)
        if reason == "policy_conflict":
            fields = getattr(exc, "fields", None) or [getattr(exc, "field", "launch settings")]
            _conflict(prefix, [str(f) for f in fields], err)
            return 2
        log.warning("the attached run failed", exc_info=True)
        err(f"{prefix}: the attached run failed: {_message(exc)}")
        return 1
    finally:
        host.stop()


def _finite_stop(run: _Finite, err: Callable[[str], None]) -> int | None:
    """The exit code (and its lines) of a finite run that ended without a turn result."""
    from indus.cli.oneshot import INTERRUPTED

    sid = run.outcome.session_id
    if run.blocked:
        for line in _blocked_lines(sid):
            err(line)
        return BLOCKED_EXIT
    if run.needs_input:
        err(
            "The reply is waiting for an answer (an approval or a question) that nobody here can "
            "give; detached without cancelling it."
        )
        err(f"Resume the session in a terminal to answer it: `indus -r {sid}`.")
        return BLOCKED_EXIT
    if run.detached:
        err("Detached: the turn is still stopping; accepted work goes on in the daemon.")
        return INTERRUPTED
    if run.outcome.interrupted and not (run.outcome.final_text or "").strip():
        err("The turn was interrupted; it was not run again. Resume the session to go on.")
    return None


def _blocked_lines(session_id: str | None) -> list[str]:
    return [
        "This session is waiting behind a turn the daemon lost (its outcome is unknown); "
        "nothing was sent, and any accepted input stays queued.",
        f"Resume it with `indus -r {session_id}` and /discard the lost turn, then try again."
        if session_id
        else "Resume the session and /discard the lost turn, then try again.",
    ]


def run_attached_chat(
    state: CliState, args: GlobalArgs, chat: ChatArgs, *, query: str | None, finite: bool
) -> int:
    """``indus chat`` attached to the daemon; returns the exit code."""
    prefix = "indus"

    def err(line: str) -> None:
        click.echo(line, err=True)

    refused = refused_flags(args, chat)
    if refused:
        for line in _refusal_lines(prefix, refused):
            err(line)
        return 2
    plan = _plan(state, args, chat, prefix, err)
    if isinstance(plan, int):
        return plan
    source = chat.source or "cli"
    if finite:
        assert query is not None
        return _finite_chat(state, plan, query, source=source, quiet=chat.quiet, err=err)
    return _interactive(state, args, chat, plan, query, source=source, err=err)


def _finite_chat(
    state: CliState,
    plan: _Plan,
    query: str,
    *,
    source: str,
    quiet: bool,
    err: Callable[[str], None],
) -> int:
    from indus.cli import resume
    from indus.cli.oneshot import finite_exit_code

    del quiet  # nothing but the reply and the session line is printed either way
    run = _finite_session(
        state,
        plan,
        query,
        source=source,
        unattended=False,
        prefix="indus chat",
        err=err,
        approve=one_shot_answer(state.scope),
    )
    if isinstance(run, int):
        return run
    outcome = run.outcome
    if outcome.session_id:
        resume.write_breadcrumb(state.scope, outcome.session_id, str(plan.cwd))
    stop = _finite_stop(run, err)
    if stop is not None:
        return stop
    reply = (outcome.final_text or "").encode("utf-8", "replace").decode("utf-8")
    if reply:
        click.echo(reply.rstrip("\n"))
    if outcome.failure:
        err(f"indus chat: {outcome.failure}")
    if run.unknown:
        err(_UNKNOWN_HINT)
    if outcome.session_id:
        err(f"session_id: {outcome.session_id}")
    return finite_exit_code(outcome)


def run_attached_oneshot(
    state: CliState, args: GlobalArgs, prompt: str, usage_file: str | None
) -> int:
    """``indus -z`` attached to the daemon (after ``oneshot``'s own checks)."""
    from indus.cli.oneshot import oneshot_exit_code, write_usage_file

    prefix = "indus -z"
    messages: list[str] = []

    def err(line: str) -> None:
        messages.append(line)
        click.echo(line, err=True)

    refused = refused_flags(args)
    if refused:
        for line in _refusal_lines(prefix, refused):
            err(line)
        return 2
    plan = _plan(state, args, ChatArgs(), prefix, err)
    if isinstance(plan, int):
        _write_failed_usage(usage_file, args, messages)
        return plan
    run = _finite_session(
        state, plan, prompt, source="cli", unattended=True, prefix=prefix, err=err
    )
    if isinstance(run, int):
        _write_failed_usage(usage_file, args, messages)
        return run
    outcome = run.outcome
    outcome.model = outcome.model or args.model
    outcome.provider = outcome.provider or args.provider
    write_usage_file(usage_file, outcome, outcome.failure)
    stop = _finite_stop(run, err)
    if stop is not None:
        return stop
    reply = (outcome.final_text or "").encode("utf-8", "replace").decode("utf-8")
    if reply:
        if outcome.failure is not None:
            err(f"{prefix}: {reply.rstrip()}")
        else:
            click.echo(reply.rstrip("\n"))
    code = oneshot_exit_code(outcome)
    if code == 1:
        err(f"{prefix}: the run finished without a reply.")
    elif code == 2 and outcome.failure:
        err(f"{prefix}: {outcome.failure}")
        if run.unknown:
            err(_UNKNOWN_HINT)
    return code


def _write_failed_usage(path: str | None, args: GlobalArgs, messages: Sequence[str]) -> None:
    from indus.cli.oneshot import RunOutcome, write_usage_file

    reason = messages[0] if messages else "could not start"
    write_usage_file(path, RunOutcome(model=args.model, provider=args.provider), reason)


# -- interactive ----------------------------------------------------------------------------------


def _pending_notes(rows: Sequence[PendingRow]) -> list[str]:
    unknown = [row for row in rows if row.state == "unknown"]
    if not unknown:
        return []
    notes = [
        "A turn was lost when the daemon restarted: its outcome is unknown and it was not run "
        "again. /discard it to let queued work go on (do not resend it)."
    ]
    for row in unknown:
        preview = f" — {row.text[:60]}" if row.text else ""
        notes.append(f"  /discard {row.admission_id}{preview}")
    return notes


def _report_lost_turns(host: AttachedHost, repl: Repl, session_id: str) -> None:
    """After a gap (the daemon came back, or this terminal fell behind): read the session's lost
    turns again and show each with its ``/discard`` line. Runs off the attach loop: the ledger
    read waits on it."""

    def report() -> None:
        try:
            notes = _pending_notes(host.pending(session_id))
        except Exception:
            log.debug("could not read the pending admissions after a gap", exc_info=True)
            return
        if notes:
            repl.post(lambda: [repl.note(note) for note in notes])

    threading.Thread(target=report, name="indus-attach-gap", daemon=True).start()


def attached_commands(host: AttachedHost) -> Callable[[Repl, str], bool]:
    """The REPL's command hook in attach mode (see the module notes)."""

    def hook(repl: Repl, text: str) -> bool:
        line = text.strip()
        if line.startswith("!"):
            repl.error("shell commands (!) run in this terminal: start with `indus --local`")
            return True
        if not line.startswith("/"):
            return False
        name, _, rest = line[1:].partition(" ")
        name = name.lower()
        if name in ("quit", "exit", "detach", "q"):
            repl.note("detached; accepted work goes on in the daemon")
            repl.request_exit()
        elif name == "help":
            repl.print(_HELP)
        elif name == "stop":
            if not repl.interrupt():
                repl.note("nothing is running")
        elif name == "discard":
            _discard(repl, host, rest.strip())
        else:
            repl.error(
                f"/{name} is not available while attached to the gateway daemon; /help lists "
                "what works here, or start with `indus --local`."
            )
        return True

    return hook


def _discard(repl: Repl, host: AttachedHost, admission_id: str) -> None:
    if not admission_id or " " in admission_id:
        repl.error("usage: /discard <admission_id>")
        return
    try:
        rows = host.pending(repl.session_id)
    except Exception as exc:
        repl.error(f"could not list the session's lost turns: {_message(exc)}")
        return
    row = next((r for r in rows if r.admission_id == admission_id and r.state == "unknown"), None)
    if row is None:
        repl.error(f"{admission_id} is not a lost turn of this session; nothing was discarded")
        return
    try:
        result = host.invoke("discard_unknown", repl.session_id, admission_id, row.generation)
    except Exception as exc:
        result = exc
    reason = failure_reason(result)
    refused = getattr(result, "discarded", True) is False
    if isinstance(result, BaseException) or reason == "stale_generation" or refused:
        if reason == "stale_generation":
            repl.error(
                f"refused: {admission_id} changed since it was listed (stale generation); "
                "nothing was discarded"
            )
        elif isinstance(result, BaseException):
            repl.error(f"could not discard {admission_id}: {_message(result)}")
        else:
            repl.error(f"refused: {admission_id} is not a lost turn of this session ({reason})")
        return
    repl.note(f"discarded {admission_id}; queued work can go on")


def _interactive(
    state: CliState,
    args: GlobalArgs,
    chat: ChatArgs,
    plan: _Plan,
    seed: str | None,
    *,
    source: str,
    err: Callable[[str], None],
) -> int:
    from indus.cli import resume
    from indus.cli.repl.app import ReplOptions, run_repl

    backend = backend_factory()
    try:
        endpoint = _ensure(backend, state.scope)
        host = AttachedHost(backend, state.scope, endpoint).start()
    except AttachUnavailable as exc:
        return _unreachable("indus", exc, err)
    try:
        try:
            sid = _open(host, plan, source)
            rows = host.pending(sid)
        except ProtocolError as exc:
            err(f"indus: {exc.message}")
            return 1
        except Exception as exc:
            if failure_reason(exc) == "policy_conflict":
                fields = getattr(exc, "fields", None) or ["launch settings"]
                _conflict("indus", [str(f) for f in fields], err)
                return 2
            log.warning("opening the attached session failed", exc_info=True)
            err(f"indus: the session could not be opened on the daemon: {_message(exc)}")
            return 1
        resume.write_breadcrumb(state.scope, sid, str(plan.cwd))
        notes = [
            "Attached to this profile's gateway daemon: it runs the agent, this terminal shows "
            "it. /help lists what works here; `indus --local` runs the agent in this terminal.",
            *_pending_notes(rows),
        ]
        title = (plan.row or {}).get("title")
        options = ReplOptions.from_scope(
            state.scope, model=args.model or "", title=str(title or "")
        )

        def ready(repl: Repl) -> None:
            for note in notes:
                repl.note(note)
            host.on_gap(sid, lambda _gap: _report_lost_turns(host, repl, sid))
            if seed is not None and seed.strip():
                text = seed
                repl.post(lambda: repl.send(text))

        return run_repl(
            host,
            sid,
            scope=state.scope,
            options=options,
            commands=attached_commands(host),
            on_ready=ready,
        )
    finally:
        host.stop()
