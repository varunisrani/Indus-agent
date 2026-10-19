"""What a slash handler works with, and what it hands back to the REPL (P06-09).

The REPL (``indus.cli.repl``) builds one ``SlashContext`` per chat and passes it to every
``dispatch`` call. Handlers never touch the terminal or the agent directly:

- they print through ``ctx.out`` (one call per line of plain text);
- they reach the session service through ``ctx.port`` (a ``SessionPort``; ``port.HostPort`` adapts
  the in-process host) and the store through ``ctx.store``; the P10 session features (``/goal``,
  ``/loop``, ``/heartbeat``, ``/btw``, ``/moa``) through ``ctx.automation`` when the host runs them,
  and the REPL's voice mode (``/voice``) through ``ctx.voice``;
- they answer with a ``SlashResult`` saying what the REPL must do next — leave, switch to another
  session, clear the screen, send a message as a turn, or put text back in the composer.

``ReplState`` is the part of the REPL's state that commands read or change: the queued messages,
the armed ``/resume`` list, the reasoning display switch, the session's toolset selection and the
row changes that wait for the session row to exist (a fresh session has no row until its first turn
is stored).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from indus.foundation.clock import Clock
from indus.foundation.scope import Scope
from indus.protocol.methods import SessionCreateParams
from indus.protocol.server_requests import ApprovalAnswer, ApprovalRequest
from indus.providers.route import RuntimeRoute
from indus.state.registry import StoreHandle

__all__ = [
    "AutomationPort",
    "AutomationReply",
    "CompressReport",
    "Extensions",
    "ExtensionHandler",
    "Handler",
    "Observer",
    "Output",
    "ReplState",
    "RouteInfo",
    "SessionPort",
    "SlashContext",
    "SlashResult",
    "VoiceControl",
]

type Output = Callable[[str], None]
"""Prints one line (or several joined by newlines) of plain text."""


@dataclass(frozen=True)
class SlashResult:
    """What the REPL does after a command.

    ``switch_to``: the session the REPL is on from now (``ctx.session_id`` is already updated);
    ``resumed``: that session has history (show the recap). ``submit``: send this text as a turn.
    ``prefill``: put this text back in the composer. ``clear_screen``: clear before going on.
    ``exit``: leave the REPL.
    """

    exit: bool = False
    switch_to: str | None = None
    resumed: bool = False
    clear_screen: bool = False
    submit: str | None = None
    prefill: str | None = None


@dataclass(frozen=True)
class RouteInfo:
    """The route a session is bound to, as commands show and change it."""

    route: RuntimeRoute
    context_length: int
    reasoning_effort: str | None
    requested_effort: str | None

    @property
    def provider(self) -> str:
        return self.route.provider

    @property
    def model(self) -> str:
        return self.route.model

    @property
    def base_url(self) -> str:
        return self.route.base_url


@dataclass(frozen=True)
class CompressReport:
    """What a manual ``/compress`` came to (P08-05). ``status``: ``compressed``, ``nothing``,
    ``blocked``, ``aborted`` or ``preview``; ``session_id`` is where the conversation goes on
    (a new id after a rotating compaction)."""

    status: str
    session_id: str
    reason: str = ""
    before: int = 0
    after: int = 0
    condensed: int = 0
    mode: str = ""


class SessionPort(Protocol):
    """What commands may ask of the session service (blocking calls)."""

    def create(self, params: SessionCreateParams) -> str:
        """Create and attach a session; its id."""
        ...

    def resume(self, session_id: str) -> None:
        """Attach a stored session."""
        ...

    def close(self, session_id: str) -> None:
        """Detach a session (unknown ids are ignored)."""
        ...

    def interrupt(self, session_id: str) -> bool:
        """Stop the running turn; False when none runs."""
        ...

    def wait_idle(self, session_id: str, timeout: float) -> bool:
        """Wait up to ``timeout`` s for the running turn to end; True when nothing runs now."""
        ...

    def steer(self, session_id: str, text: str) -> bool:
        """Hand guidance to the running turn; False when none runs."""
        ...

    def route(self, session_id: str) -> RouteInfo:
        """The session's current route."""
        ...

    def switch_route(
        self, session_id: str, route: RuntimeRoute, *, effort: str | None = None, keep_effort: bool
    ) -> RouteInfo:
        """Rebind the session to ``route`` all or nothing (raises and stays put on failure).
        ``keep_effort`` keeps the requested reasoning effort, else ``effort`` replaces it."""
        ...

    def tool_names(self, session_id: str) -> list[str]:
        """The tools the session's agent offers the model."""
        ...

    def stop_processes(self, session_id: str, *, all_jobs: bool) -> int:
        """Stop the session's background processes (``all_jobs`` False keeps the ones marked to
        outlive the session); how many were stopped."""
        ...

    def compress(
        self, session_id: str, focus: str | None, *, keep_last: int | None, preview: bool
    ) -> CompressReport:
        """Condense the session's older turns now (between turns; raises when busy)."""
        ...


@dataclass(frozen=True)
class AutomationReply:
    """A P10 command's answer: lines to show, and text to send as the next message."""

    lines: tuple[str, ...]
    submit: str | None = None


class AutomationPort(Protocol):
    """The host's P10 session features (``service.automation.SessionAutomation``), blocking."""

    def run(self, command: str, session_id: str, args: str) -> AutomationReply:
        """Run ``/command args`` for the session."""
        ...


class VoiceControl(Protocol):
    """The REPL's voice mode (``indus.cli.voice.VoiceMode``) as ``/voice`` drives it (P10-19)."""

    def turn_on(self) -> bool: ...

    def turn_off(self) -> None: ...

    def status_lines(self) -> list[str]: ...


type Handler = Callable[["SlashContext", str], SlashResult]
"""A built-in command handler: ``(ctx, arguments)``; arguments keep their case."""

type ExtensionHandler = Callable[[str, "SlashContext"], SlashResult | None]
"""A plugin or skill command: ``(arguments, ctx)``; None means "done, nothing more"."""

type Observer = Callable[[str, str, "SlashContext"], object]
"""Called with ``(command, arguments, ctx)`` before built-ins run; its result is ignored."""


@dataclass
class Extensions:
    """Seams for commands that do not come from the registry (filled by P09 and P13)."""

    plugin_commands: Mapping[str, ExtensionHandler] = field(
        default_factory=dict[str, ExtensionHandler]
    )
    skill_commands: Mapping[str, ExtensionHandler] = field(
        default_factory=dict[str, ExtensionHandler]
    )
    observers: list[Observer] = field(default_factory=list[Observer])


@dataclass
class ReplState:
    """REPL state commands read and change (see the module notes)."""

    queue: list[str] = field(default_factory=list[str])
    resume_choices: list[str] | None = None
    show_reasoning: bool = False
    toolsets: list[str] | None = None
    pending_title: str | None = None
    pending_config: dict[str, Any] = field(default_factory=dict[str, Any])

    def forget_session(self) -> None:
        """Called when the REPL moves to another session."""
        self.queue.clear()
        self.resume_choices = None
        self.pending_title = None
        self.pending_config.clear()


def _no(_question: str) -> bool:
    return False


def _idle() -> bool:
    return False


@dataclass
class SlashContext:
    """Everything a slash handler may use (see the module notes)."""

    scope: Scope
    port: SessionPort
    session_id: str
    out: Output
    store: StoreHandle | None = None
    state: ReplState = field(default_factory=ReplState)
    create_params: SessionCreateParams = field(default_factory=SessionCreateParams)
    is_busy: Callable[[], bool] = _idle
    confirm: Callable[[str], bool] = _no
    ask_approval: Callable[[ApprovalRequest], ApprovalAnswer | None] | None = None
    session_cwd: Callable[[], str | None] | None = None
    clock: Clock | None = None
    interactive: bool = True
    extensions: Extensions = field(default_factory=Extensions)
    catalogue: Mapping[str, str] | None = None
    busy_wait_s: float = 30.0
    automation: AutomationPort | None = None
    voice: VoiceControl | None = None

    @property
    def time(self) -> Clock:
        return self.clock if self.clock is not None else self.scope.clock

    def say(self, *lines: str) -> None:
        for line in lines:
            self.out(line)
