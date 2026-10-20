"""The one interface every ACP handler uses, and how a process picks its host (P19-03, D-16.29).

``AcpBackend`` is implemented twice: ``attached_host.AttachedHost`` (default; a viewer of the
profile's daemon, P17) and ``local_host.LocalHost`` (``indus acp --local``; an in-process
``SessionService``). Handlers never ask which one they have: both create sessions with source
``acp`` and the same editor policy, both advertise the same single mode, and both answer with the
same shapes defined here.

Mode selection (``HostSelector``): ``--local`` → local. Otherwise attached; when no daemon answers
for this profile, ``acp.allow_local_fallback: true`` switches to local once, and the default
``false`` (D-16.83) keeps ``initialize`` working but fails every session call with
``DAEMON_UNAVAILABLE_TEXT``.

Errors leave as ``BackendError(code, message)``; ``code`` is a short stable reason the handlers map
onto JSON-RPC errors (``agent.error_for``).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal, Protocol

from indus.core.transcript import TranscriptRow
from indus.protocol.events import EventBase
from indus.protocol.server_requests import ServerRequest

__all__ = [
    "CODES",
    "DAEMON_UNAVAILABLE_TEXT",
    "MUTATIONS",
    "AcpBackend",
    "Admission",
    "BackendError",
    "CancelOutcome",
    "CommandReply",
    "CreateRequest",
    "EditorPolicy",
    "EventSink",
    "HostMode",
    "HostSelector",
    "LaunchPolicy",
    "ListedSession",
    "LostTurn",
    "MutationResult",
    "PendingPrompt",
    "SessionSnapshot",
    "Settled",
    "UsageInfo",
]

log = logging.getLogger(__name__)

type HostMode = Literal["attached", "local"]
type Mutation = Literal[
    "workspace", "branch", "rename", "compress", "editor_mcp", "model", "rewind", "reset"
]
MUTATIONS: Final[tuple[str, ...]] = (
    "workspace",
    "branch",
    "rename",
    "compress",
    "editor_mcp",
    "model",
    "rewind",
    "reset",
)

CODES: Final[frozenset[str]] = frozenset(
    {
        "not_found",
        "daemon_unavailable",
        "profile_mismatch",
        "acp_policy_unavailable",
        "launch_unavailable",
        "creation_refused",
        "mutation_unavailable",
        "session_busy",
        "outcome_unknown",
        "unknown_execution",
        "unsupported_command",
        "backend_failed",
    }
)

DAEMON_UNAVAILABLE_TEXT: Final = (
    "No Indus gateway daemon is running for this profile. Start it with `indus gateway start`, "
    "or run the editor agent in-process with `indus acp --local`."
)


class BackendError(Exception):
    """A backend call that did not happen (``code`` from ``CODES``; ``message`` for people)."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


# -- what goes in ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class EditorPolicy:
    """What the editor asks for a session: its own MCP servers and the edit approval policy.

    ``mcp_servers`` are the editor's specs as sent (commands, env, headers). They live in this
    process's memory only: never in the store, never in a log."""

    mcp_servers: tuple[Mapping[str, Any], ...] = field(default=(), repr=False)
    edit_approval_policy: Literal["ask"] = "ask"

    @property
    def server_names(self) -> list[str]:
        return [str(spec.get("name", "")) for spec in self.mcp_servers]

    def wire(self) -> dict[str, Any]:
        return {
            "mcp_servers": [dict(spec) for spec in self.mcp_servers],
            "edit_approval_policy": self.edit_approval_policy,
        }


@dataclass(frozen=True)
class LaunchPolicy:
    """The launch choices frozen into a new session (``creation_policy`` field names)."""

    fields: Mapping[str, Any] = field(default_factory=dict[str, Any])

    @property
    def bypass(self) -> bool:
        return bool(self.fields.get("safe_mode") or self.fields.get("ignore_user_config"))


@dataclass(frozen=True)
class CreateRequest:
    cwd: str
    request_id: str
    editor: EditorPolicy = EditorPolicy()
    launch: LaunchPolicy = LaunchPolicy()


# -- what comes back ------------------------------------------------------------------------------


@dataclass(frozen=True)
class PendingPrompt:
    """An open approval/clarify request of the session and the generation it belongs to."""

    request: ServerRequest
    generation: int | None = None


@dataclass(frozen=True)
class LostTurn:
    """An admission whose turn the daemon lost in a restart (state ``unknown``)."""

    admission_id: str
    generation: int


@dataclass(frozen=True)
class SessionSnapshot:
    """A session as the backend holds it right after create / resume."""

    session_id: str
    cwd: str | None = None
    title: str | None = None
    model: str | None = None
    provider: str | None = None
    history: tuple[TranscriptRow, ...] = ()
    pending: tuple[PendingPrompt, ...] = ()
    lost: tuple[LostTurn, ...] = ()
    replay_epoch: str | None = None
    last_seq: int = 0
    created: bool = False


@dataclass(frozen=True)
class ListedSession:
    session_id: str
    cwd: str | None
    title: str | None
    preview: str
    updated_at: float | None


type AdmissionState = Literal["queued", "started", "terminal", "unknown"]


@dataclass(frozen=True)
class Admission:
    """A submit's admission (and a receipt's answer)."""

    admission_id: str
    generation: int | None
    """The turn that runs (or ran) it; None while the daemon has not claimed it (D-16.555)."""
    state: AdmissionState
    outcome: str | None = None
    final_text: str | None = None


type CancelOutcome = Literal["cancelled", "interrupted", "settled", "not_found", "unknown"]


@dataclass(frozen=True)
class MutationResult:
    """``confirm``: nothing changed; the change needs the person's yes first (``data.message``
    says why; the same call with ``payload.confirm`` true applies it)."""

    status: Literal["applied", "deferred", "cancelled", "refused", "confirm"]
    data: Mapping[str, Any] = field(default_factory=dict[str, Any])


@dataclass(frozen=True)
class Settled:
    """How an admission's turn ended: ``outcome`` is ``completed``, ``cancelled`` or ``failed``
    (``failure`` says why); ``turn_id`` names the turn in the session's events."""

    turn_id: str
    outcome: str
    failure: str | None = None


@dataclass(frozen=True)
class CommandReply:
    """A slash command's answer: ``text`` to show, and/or ``submit`` to run as the prompt."""

    text: str | None = None
    submit: str | None = None


@dataclass(frozen=True)
class UsageInfo:
    """``size``: the context window in tokens; ``used``: its current occupancy."""

    size: int
    used: int


class EventSink(Protocol):
    """Where a subscribed session's traffic goes (the editor projection, P19-10/11)."""

    def on_event(self, session_id: str, event: EventBase) -> None: ...

    def on_request(
        self, session_id: str, request: ServerRequest, generation: int | None
    ) -> None: ...

    def on_gap(self, session_id: str, info: Mapping[str, Any]) -> None: ...

    def on_lost(self, session_id: str, lost: LostTurn) -> None: ...


class AcpBackend(Protocol):
    """See the module notes; every id argument may be an id the session had before (aliases)."""

    @property
    def mode(self) -> HostMode: ...

    async def create(self, request: CreateRequest) -> SessionSnapshot: ...

    async def resume(
        self, session_id: str, *, editor: EditorPolicy | None = None
    ) -> SessionSnapshot: ...

    async def fork(self, session_id: str, *, editor: EditorPolicy | None = None) -> str: ...

    async def mutate(
        self, session_id: str, operation: str, payload: Mapping[str, Any]
    ) -> MutationResult: ...

    async def list_sessions(self, *, limit: int = 1000) -> list[ListedSession]: ...

    async def submit(self, session_id: str, text: str, *, input_id: str) -> Admission: ...

    async def cancel(self, session_id: str, admission_id: str) -> CancelOutcome: ...

    async def receipt(self, session_id: str, admission_id: str) -> Admission: ...

    async def wait(self, session_id: str, admission_id: str) -> Settled:
        """How the admission's turn ended; ``BackendError("outcome_unknown")`` when the
        transport went first (never resend), ``unknown_execution`` when it was lost."""
        ...

    async def lost(self, session_id: str) -> list[LostTurn]:
        """The session's turns lost in a daemon restart (state ``unknown``), oldest first."""
        ...

    async def steer(self, session_id: str, text: str, *, admission_id: str | None) -> bool:
        """Guidance for the running turn of ``admission_id`` only; False when it is not
        running (nothing was sent)."""
        ...

    async def run_command(self, session_id: str, name: str, arg: str) -> CommandReply:
        """Run a registry slash command for the session (``BackendError`` when refused)."""
        ...

    async def answer(
        self,
        session_id: str,
        request_id: str,
        generation: int | None,
        answer: Mapping[str, Any],
    ) -> bool: ...

    async def usage(self, session_id: str) -> UsageInfo | None: ...

    async def show_reasoning(self, session_id: str) -> None: ...

    def subscribe(self, session_id: str, sink: EventSink) -> Callable[[], None]: ...

    def current_id(self, session_id: str) -> str: ...

    async def close_session(self, session_id: str) -> None: ...

    async def aclose(self) -> None: ...


# -- picking a host -------------------------------------------------------------------------------


type BackendFactory = Callable[[], Awaitable[AcpBackend]]


class HostSelector:
    """Builds the host on first use (see the module notes).

    ``attached`` builds the attached host; its ``BackendError("daemon_unavailable")`` from
    ``start()`` is the "no daemon" signal. ``local`` builds the in-process host.
    """

    def __init__(
        self,
        *,
        local_only: bool,
        allow_fallback: bool,
        attached: BackendFactory,
        local: BackendFactory,
    ) -> None:
        self.local_only = local_only
        self.allow_fallback = allow_fallback
        self._attached = attached
        self._local = local
        self._backend: AcpBackend | None = None

    @property
    def built(self) -> AcpBackend | None:
        return self._backend

    async def get(self) -> AcpBackend:
        if self._backend is not None:
            return self._backend
        if self.local_only:
            self._backend = await self._local()
            return self._backend
        try:
            self._backend = await self._attached()
        except BackendError as exc:
            if exc.code != "daemon_unavailable":
                raise
            if not self.allow_fallback:
                raise BackendError("daemon_unavailable", DAEMON_UNAVAILABLE_TEXT) from None
            log.warning("no gateway daemon answers; acp.allow_local_fallback runs sessions here")
            self._backend = await self._local()
        return self._backend

    async def aclose(self) -> None:
        backend, self._backend = self._backend, None
        if backend is not None:
            await backend.aclose()


def history_tuple(rows: Sequence[TranscriptRow]) -> tuple[TranscriptRow, ...]:
    return tuple(rows)
