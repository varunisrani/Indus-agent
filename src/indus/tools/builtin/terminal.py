# pyright: strict
"""The ``terminal`` tool: schema, normalisation, planning, guards and approval (P05-11).

One call goes through these steps, each of which can end it with a result from
``terminal_result``:

1. Normalise the arguments (``normalize``): a stray ``code`` argument points to ``execute_code``;
   background-only arguments on a foreground call are refused with the corrected call, except a
   foreground ``heartbeat``, which is dropped (refusing it made models resend the same call,
   R02.11); ``notify: true`` means "tell me when it exits", ``notify: [..]`` lists watch patterns,
   and a heartbeat implies notify.
2. Plan (``plan``): the command must be text and the timeout positive. A foreground command whose
   only backgrounding is one trailing ``&`` runs as a background job of the command without it;
   other backgrounding shapes (inline ``&``, nohup/disown/setsid, dev servers and the like) are
   refused with guidance unless the call asks only for help or a version. A foreground timeout
   above ``terminal.max_foreground_timeout`` (600 s) becomes a tracked background job with
   notify-on-exit and a note not to run it again.
3. Environment: from the process's ``EnvironmentRegistry`` for this profile and session; an
   unusable backend answers ``disabled``, an unreachable one ``degraded`` (P05-04).
4. Pre-exec guards (``GUARDS``) on the shared ``tools`` pool under a deadline of
   ``max(timeout, 30 s)``; a guard that does not finish refuses the call (fail closed).
5. Approval through the P05-08 pipeline behind ``ApprovalGate``: blocked and pending answers end
   the call; a user approval is noted in the result.
6. Run: in the foreground (``terminal_result.run_foreground`` + ``finish``) or as a background job
   (``ProcessRegistry``), where notify flags are kept only when this session can receive the
   messages (``Deliveries``, R02.17).

Configuration is read from the call's ``Scope`` on every call (D-16.75). Work without a profile
scope is refused before anything is touched (R02.8).
"""

from __future__ import annotations

import logging
import os
import re
import weakref
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

from indus.backends.contract import (
    BackendFlags,
    BackendUnavailable,
    CancelSignal,
    Environment,
    EnvironmentConnectionError,
    ExecResult,
)
from indus.backends.docker import recovery
from indus.backends.process_notify import HEARTBEAT_MIN_S, Deliveries
from indus.backends.processes import ProcessLimit, ProcessRegistry, SpawnRequest
from indus.backends.registry import BackendStatus, EnvironmentRegistry, EnvKey
from indus.foundation.blocking import run_blocking
from indus.foundation.clock import Clock
from indus.foundation.config.schema import default_for
from indus.foundation.scope import Scope, scope_or_none
from indus.protocol.requests_port import ServerRequestPort
from indus.safety import approvals
from indus.safety.approvals.decision import ApprovalDecision, InterruptFlag, RunContext
from indus.safety.redaction import redact_output
from indus.tools.builtin import mutation_hooks
from indus.tools.builtin import terminal_result as results
from indus.tools.registry import CrossRef, ToolRegistry, ToolTraits

__all__ = [
    "GUARDS",
    "GUARD_MIN_S",
    "TERMINAL_SCHEMA",
    "ApprovalGate",
    "Call",
    "GateRequest",
    "GateVerdict",
    "GuardInput",
    "Plan",
    "Refusal",
    "TerminalRuntime",
    "TerminalTool",
    "foreground_guidance",
    "normalize",
    "pipeline_gate",
    "plan",
    "run_context_of",
    "clear_stale_interrupt",
    "register_tools",
    "runtime",
    "trailing_amp_command",
    "workdir_guard",
]

log = logging.getLogger(__name__)

TOOL_NAME = "terminal"
TOOLSET = "terminal"
MAX_RESULT_CHARS = 100_000
GUARD_MIN_S = 30.0
DEFAULT_FOREGROUND_CAP = 600
RUN_CONTEXTS: frozenset[str] = frozenset({"interactive", "cron", "single_query", "unattended"})


# -- the process-wide pieces ----------------------------------------------------------------------


@dataclass
class TerminalRuntime:
    """What every terminal call in the process shares: environments, background jobs and the
    table of sessions that can receive job messages."""

    environments: EnvironmentRegistry
    processes: ProcessRegistry
    deliveries: Deliveries

    lost_handler: Callable[[str], object] | None = None

    @classmethod
    def create(cls, *, clock: Clock | None = None) -> TerminalRuntime:
        deliveries = Deliveries()
        processes = ProcessRegistry(deliveries, clock=clock)
        environments = EnvironmentRegistry(
            clock=clock, background_alive=processes.alive_for_env_key
        )
        made = cls(environments, processes, deliveries)
        made.lost_handler = _lost_handler(processes)
        recovery.register_lost_handler(made.lost_handler)
        return made

    async def shutdown(self) -> None:
        """Process exit: stop background jobs, then clean every environment."""
        if self.lost_handler is not None:
            recovery.unregister_lost_handler(self.lost_handler)
            self.lost_handler = None
        await self.processes.shutdown()
        await self.environments.shutdown()


def _lost_handler(processes: ProcessRegistry) -> Callable[[str], object]:
    """Docker recovery replaced a sandbox: end that environment's jobs as ``lost`` (P11-06).
    Holds the registry weakly, so a runtime that was never shut down is not kept alive."""
    ref = weakref.ref(processes)

    def handler(env_key: str) -> object:
        registry = ref()
        return recovery.mark_lost(registry, env_key) if registry is not None else None

    return handler


_shared: dict[str, TerminalRuntime] = {}


def runtime() -> TerminalRuntime:
    """The process's runtime, made on first use. The host calls ``shutdown`` at exit."""
    found = _shared.get("default")
    if found is None:
        found = _shared["default"] = TerminalRuntime.create()
    return found


# -- step 1: normalisation ------------------------------------------------------------------------


class Refusal(Exception):
    """Carries a finished result out of the planning steps."""

    def __init__(self, result: str) -> None:
        super().__init__(result)
        self.result = result


@dataclass(frozen=True)
class Call:
    command: object
    background: bool = False
    timeout: object = None
    workdir: str | None = None
    pty: bool = False
    notify_on_exit: bool = False
    watch_patterns: tuple[str, ...] = ()
    heartbeat: int = 0
    persist_on_release: bool = False


def _flag(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


def normalize(args: Mapping[str, Any]) -> Call:
    """The call's arguments, checked and normalised; raises ``Refusal``."""
    if "command" not in args and "code" in args:
        raise Refusal(
            results.rejected(
                "terminal runs shell commands given in 'command'; this call sent 'code'. "
                "For Python use execute_code(code=...); for a shell command call "
                "terminal(command=...)."
            )
        )
    background = _flag(args.get("background", False))
    raw_beat = args.get("heartbeat") or 0
    if isinstance(raw_beat, bool) or not isinstance(raw_beat, int) or raw_beat < 0:
        raise Refusal(
            results.rejected(
                "heartbeat is a whole number of seconds: 0 turns it off, any other value is "
                f"raised to at least {HEARTBEAT_MIN_S}."
            )
        )
    heartbeat = int(raw_beat)
    notify = args.get("notify")
    notify_on_exit = _flag(args.get("notify_on_complete", False))
    raw_watch = args.get("watch_patterns")
    pty = _flag(args.get("pty", False))
    persist = _flag(args.get("persist_on_release", False))
    if not background:
        if (
            (notify is not None and notify is not False and notify != [])
            or notify_on_exit
            or raw_watch
        ):
            raise Refusal(
                results.rejected(
                    "notify is for background commands only: a foreground result comes back "
                    "directly. Leave it out, or run terminal(command=..., background=true, "
                    "notify=...)."
                )
            )
        if pty:
            raise Refusal(
                results.rejected(
                    "pty needs background=true: a terminal session is driven afterwards with "
                    "process_manage write/submit. Run terminal(command=..., background=true, "
                    "pty=true)."
                )
            )
        if persist:
            raise Refusal(
                results.rejected(
                    "persist_on_release is for background commands only. Run "
                    "terminal(command=..., background=true, persist_on_release=true)."
                )
            )
        heartbeat = 0  # meaningless here; dropped rather than refused (R02.11)
    watch: tuple[str, ...] = ()
    if isinstance(raw_watch, list):
        watch = tuple(str(p) for p in cast(list[object], raw_watch) if str(p))
    if notify is not None:
        if isinstance(notify, bool):
            notify_on_exit, watch = notify, ()
        elif isinstance(notify, list) and all(
            isinstance(p, str) for p in cast(list[object], notify)
        ):
            watch = tuple(p for p in cast(list[str], notify) if p)
            notify_on_exit = False
        else:
            raise Refusal(
                results.rejected(
                    "notify is true or false (a message when it exits) or a list of texts (a "
                    "message when an output line contains one)."
                )
            )
    if heartbeat:
        notify_on_exit = True  # heartbeats travel the same way as the exit message
    workdir = args.get("workdir")
    if workdir is not None and not isinstance(workdir, str):
        raise Refusal(results.rejected("workdir must be a path given as text."))
    return Call(
        command=args.get("command"),
        background=background,
        timeout=args.get("timeout"),
        workdir=workdir or None,
        pty=pty,
        notify_on_exit=notify_on_exit,
        watch_patterns=watch,
        heartbeat=heartbeat,
        persist_on_release=persist,
    )


# -- step 2: planning -----------------------------------------------------------------------------

_QUOTED = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"|`[^`]*`")
_WRAPPERS = re.compile(
    r"(?:^|[;&|(]\s*|\$\(\s*)(?:nohup|disown|setsid)\b", re.IGNORECASE | re.MULTILINE
)
# '&' spellings that do not put anything in the background.
_NOT_BACKGROUND = re.compile(r"&&|\d*[<>]&(?:\d+|-)?|&>>?|\|&")
_SUBSTITUTIONS = ("$(", "`", "<(", ">(")
_LONG_LIVED = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:dev|start|serve|watch)\b",
        r"\bdocker[\s-]+compose\s+up\b(?!.*\s(?:-d|--detach)\b)",
        r"\bnext\s+dev\b",
        r"(?:^|[\s;&|])vite(?:\s|$)",
        r"\bnodemon\b",
        r"\buvicorn\b",
        r"\bgunicorn\b",
        r"\bflask\s+run\b",
        r"\bpython3?\s+-m\s+http\.server\b",
        r"\brails\s+(?:s|server)\b",
        r"\bphp\s+-S\b",
    )
)


def _mask_quotes(command: str) -> str:
    """Quoted text blanked so a quoted '&' or keyword is never mistaken for shell syntax."""
    return _QUOTED.sub("''", command)


def _asks_help_or_version(command: str) -> bool:
    words = command.lower().split()
    return any(w in ("--help", "--version") for w in words) or (
        bool(words) and words[-1] in ("-h", "-v")
    )


def foreground_guidance(command: str) -> str | None:
    """Why this foreground command should be a background job instead, or None."""
    if _asks_help_or_version(command):
        return None
    plain = _mask_quotes(command)
    if _WRAPPERS.search(plain):
        return (
            "This foreground command detaches itself (nohup/disown/setsid), so its output and "
            'exit would be lost. Send it without the wrapper as terminal(command="...", '
            "background=true, notify=true) so it is tracked; check readiness and run tests in "
            "separate calls."
        )
    if "&" in _NOT_BACKGROUND.sub("", plain):
        return (
            "This foreground command puts work in the background with '&'. Send it without the "
            "'&' as terminal(command=\"...\", background=true) (add notify=true for a job that "
            "ends), then check on it in follow-up calls."
        )
    if any(p.search(plain) for p in _LONG_LIVED):
        return (
            "This looks like a server or watcher that keeps running. Start it with "
            "background=true, wait until it is ready (a health URL or a log line), then run "
            "tests in a separate call."
        )
    return None


def _strip_trailing_amp(command: str) -> str:
    body = command.rstrip()[:-1]
    end = len(body)
    while end and body[end - 1].isspace():
        slashes = len(body[: end - 1]) - len(body[: end - 1].rstrip("\\"))
        if slashes % 2:  # an escaped space belongs to the last word
            break
        end -= 1
    return body[:end]


def trailing_amp_command(command: str) -> str | None:
    """The command without its trailing ``&`` when that ``&`` is its only backgrounding."""
    if _asks_help_or_version(command) or any(s in command for s in _SUBSTITUTIONS):
        return None
    if not command.rstrip().endswith("&"):
        return None
    plain = _mask_quotes(command).rstrip()
    if not re.search(r"\s&$", plain):
        return None
    body = plain[:-1]
    if any(c in body for c in "\n\r;#") or _WRAPPERS.search(body):
        return None
    if "&" in _NOT_BACKGROUND.sub("", body):
        return None
    stripped = _strip_trailing_amp(command)
    return stripped if stripped.strip() else None


@dataclass(frozen=True)
class Plan:
    command: str
    background: bool
    timeout: float
    workdir: str | None
    auto_background: bool = False
    promoted_from: int | None = None


def _config(scope: Scope, key: str, fallback: Any = None) -> Any:
    value = scope.config.get(key) if scope.config is not None else None
    if value is None:
        value = default_for(key, fallback)
    return value


def plan(call: Call, scope: Scope) -> Plan:
    """Decide how the call runs; raises ``Refusal``."""
    command = call.command
    if not isinstance(command, str):
        raise Refusal(results.rejected(f"command must be text, got {type(command).__name__}."))
    if not command.strip():
        raise Refusal(results.rejected("command is empty."))
    raw_timeout = call.timeout
    if raw_timeout is None:
        timeout = float(_config(scope, "terminal.timeout", 180))
    elif isinstance(raw_timeout, bool) or not isinstance(raw_timeout, int | float):
        raise Refusal(results.rejected("timeout must be a whole number of seconds."))
    else:
        timeout = float(raw_timeout)
    if timeout <= 0:
        raise Refusal(
            results.rejected(f"timeout must be a positive number of seconds (got {timeout:g}).")
        )
    if call.workdir is not None and not os.path.isabs(os.path.expanduser(call.workdir)):
        raise Refusal(results.rejected("workdir must be an absolute path."))
    if call.background:
        return Plan(command, True, timeout, call.workdir)
    converted = trailing_amp_command(command)
    if converted is not None:
        return Plan(converted, True, timeout, call.workdir, auto_background=True)
    guidance = foreground_guidance(command)
    if guidance:
        raise Refusal(results.rejected(guidance))
    cap = int(_config(scope, "terminal.max_foreground_timeout", DEFAULT_FOREGROUND_CAP))
    if timeout > cap:
        return Plan(command, True, timeout, call.workdir, promoted_from=int(timeout))
    return Plan(command, False, timeout, call.workdir)


# -- step 4: guards -------------------------------------------------------------------------------


@dataclass(frozen=True)
class GuardInput:
    command: str
    workdir: str | None
    cwd: str
    backend: str


type PreExecGuard = Callable[[GuardInput], str | None]
"""Returns why the command must not run, or None. Runs on a worker thread."""

_WORKDIR_PUNCTUATION = frozenset("/\\:_-.~ +@=,")


def workdir_guard(guard: GuardInput) -> str | None:
    """The per-call workdir may hold letters, digits and ``/ \\ : _ - . ~ space + @ = ,`` only."""
    for char in guard.workdir or "":
        if (
            ord(char) < 32
            or ord(char) == 127
            or not (char.isalnum() or char in _WORKDIR_PUNCTUATION)
        ):
            return (
                f"Blocked: workdir contains the character {char!r}, which is not allowed. Use a "
                "plain directory path without shell syntax."
            )
    return None


GUARDS: tuple[PreExecGuard, ...] = (workdir_guard,)


def _run_guards(guards: Sequence[PreExecGuard], data: GuardInput) -> str | None:
    for guard in guards:
        problem = guard(data)
        if problem:
            return problem
    return None


# -- step 5: approval -----------------------------------------------------------------------------


@dataclass(frozen=True)
class GateRequest:
    command: str
    scope: Scope
    session_id: str | None
    turn_id: str | None
    broker: object | None
    flags: BackendFlags
    background: bool = False
    cwd: str | None = None
    run_context: object | None = None
    judge: object | None = None  # the smart pre-judge (``approval_judge`` extra, P08-14)


@dataclass(frozen=True)
class GateVerdict:
    status: Literal["approved", "blocked", "pending"]
    note: str | None = None  # the audit note for an approved run
    error: str | None = None
    user_summary: str | None = None
    description: str = ""
    pattern_key: str = ""
    allow_permanent: bool = True
    decision: ApprovalDecision | None = None  # the pipeline's own decision, when there is one


type ApprovalGate = Callable[[GateRequest], Awaitable[GateVerdict]]

NO_ANSWER_TEXT = {
    "timeout": (
        "No answer to the approval request came in time, so the command was not run. This is "
        "not a refusal: ask the user, then try again."
    ),
    "not_answered": (
        "The approval request could not be answered here, so the command was not run. This is "
        "not a refusal."
    ),
}


def run_context_of(value: object) -> RunContext:
    """The approval pipeline's run context for the tool context's ``run_context`` value: one of
    ``interactive``, ``cron``, ``single_query``, ``unattended``; anything else is interactive
    (the pipeline still treats a call without a broker as unattended)."""
    if isinstance(value, str) and value in RUN_CONTEXTS:
        return cast(RunContext, value)
    return "interactive"


async def pipeline_gate(request: GateRequest) -> GateVerdict:
    """The P05-08/09 approval pipeline (``indus.safety.approvals.decide``). Fails closed when it
    cannot be used."""
    try:
        context = approvals.ApprovalContext(
            scope=request.scope,
            session_id=request.session_id,
            turn_id=request.turn_id,
            run_context=run_context_of(request.run_context),
            broker=cast(ServerRequestPort | None, request.broker),
            isolated=request.flags.isolated,
            cwd=request.cwd,
            judge=request.judge,
        )
        decided = await approvals.decide(request.command, context)
    except Exception as exc:
        log.error("approval pipeline unusable: %s: %s", type(exc).__name__, exc)
        return GateVerdict(
            "blocked",
            error="Blocked: the approval check could not run, so the command was not run.",
        )
    description = decided.description or ""
    if decided.approved:
        note = None
        if decided.user_approved:
            what = description or "flagged"
            note = f"This command needed approval ({what}) and the user approved it."
        elif decided.outcome == "smart_approved":
            what = description or "flagged"
            note = f"This command was flagged ({what}); the safety reviewer approved this run."
        return GateVerdict("approved", note=note, description=description, decision=decided)
    if decided.status == "pending":
        return GateVerdict(
            "pending",
            description=description or "command flagged",
            pattern_key=decided.pattern_key or "",
            decision=decided,
        )
    reason = decided.deny_reason or decided.message
    if reason:
        error = reason
    elif decided.outcome in NO_ANSWER_TEXT:
        error = NO_ANSWER_TEXT[decided.outcome]  # silence and withdrawal are not refusals (R02.5)
    else:
        error = (
            f"Command refused: {description or 'it was flagged'}. "
            "Ask the user, or do it another way."
        )
    return GateVerdict(
        "blocked",
        error=error,
        user_summary=decided.user_summary,
        description=description,
        decision=decided,
    )


def clear_stale_interrupt(verdict: GateVerdict, cancel: CancelSignal | None) -> None:
    """Once, before the run and its retries: let the pipeline drop an interrupt that landed while
    the user was being asked (R02.9). A no-op without a pipeline decision, or when the signal
    cannot be cleared."""
    decision = verdict.decision
    if verdict.status != "approved" or decision is None:
        return
    flag = cast(InterruptFlag, cancel) if callable(getattr(cancel, "clear", None)) else None
    try:
        decision.clear_stale_interrupt(flag)
    except Exception:
        log.warning("clearing a stale interrupt failed", exc_info=True)


# -- the tool -------------------------------------------------------------------------------------

PIPED_STDIN_NOTE = (
    "This command reads its input until end-of-file, which a terminal never sends, so it runs on "
    "plain pipes: write the input with process_manage write, then process_manage close."
)
NOT_DELIVERABLE_NOTE = (
    "This session cannot be told later that the process finished (it ends with this turn, or it "
    "runs unattended), so notify is off. Check it with process_manage poll or wait."
)
PROMOTED_NOTE = (
    "The requested timeout ({requested}s) is above the {cap}s foreground limit, so the command "
    "was started as a tracked background process instead. Do not run it again. A message with "
    "its exit code and last output arrives when it ends; process_manage poll shows progress "
    "sooner."
)
PROMOTED_POLL_NOTE = (
    "The requested timeout ({requested}s) is above the {cap}s foreground limit, so the command "
    "was started as a tracked background process instead. Do not run it again. This session "
    "cannot be told when it ends: check it with process_manage poll or wait."
)
AUTO_BACKGROUND_NOTE = (
    "The trailing '&' was turned into background=true, so the process is tracked. Do not run it "
    "again; check it with process_manage poll, log, wait or kill."
)
SILENT_HINT = (
    "Without notify=true nothing tells you when this process ends. For a job that finishes "
    "(build, tests, a deploy) start it with notify=true, or check it with process_manage "
    "poll/wait; servers and watchers are fine without it."
)
WATCH_DROPPED_NOTE = (
    "watch patterns were dropped because notify is on: together they would announce the same "
    "process twice."
)


def _needs_piped_stdin(command: str) -> bool:
    words = " ".join(command.lower().split())
    return words.startswith("gh auth login") and "--with-token" in words


@dataclass(frozen=True)
class _CallContext:
    session_id: str | None
    turn_id: str | None
    broker: object | None
    cancel: CancelSignal | None
    environments: EnvironmentRegistry
    run_context: object | None
    judge: object | None = None


class TerminalTool:
    """The ``terminal`` handler and its per-session cwd record (see the module notes)."""

    def __init__(
        self,
        shared: TerminalRuntime | None = None,
        *,
        gate: ApprovalGate = pipeline_gate,
        guards: Sequence[PreExecGuard] = GUARDS,
        clock: Clock | None = None,
    ) -> None:
        self._shared = shared
        self.gate = gate
        self.guards = tuple(guards)
        self._clock = clock
        self._cwds: dict[tuple[str, str], str] = {}

    @property
    def shared(self) -> TerminalRuntime:
        return self._shared if self._shared is not None else runtime()

    def clock_for(self, scope: Scope) -> Clock:
        return self._clock if self._clock is not None else scope.clock

    def session_cwd(self, scope: Scope, session_id: str | None) -> str | None:
        return self._cwds.get((str(scope.home), session_id or "default"))

    def probe(self, scope: Scope) -> bool:
        """Tool availability: the configured backend exists and can run here."""
        environments = self.shared.environments
        entry = environments.entry(EnvironmentRegistry.backend_name(scope))
        if entry is None:
            return False
        return entry.probe is None or entry.probe(scope) is None

    async def __call__(
        self,
        args: Mapping[str, Any],
        *,
        scope: Scope | None = None,
        session_id: str | None = None,
        turn_id: str | None = None,
        broker: object | None = None,
        cancel: CancelSignal | None = None,
        environments: EnvironmentRegistry | None = None,
        run_context: object | None = None,
        approval_judge: object | None = None,
    ) -> str:
        """The handler. ``broker``, ``cancel``, ``environments``, ``run_context`` and
        ``approval_judge`` come from ``ToolContext.extras``; without ``environments`` the process
        runtime's registry is used."""
        ctx = _CallContext(
            session_id,
            turn_id,
            broker,
            cancel,
            environments if environments is not None else self.shared.environments,
            run_context,
            approval_judge,
        )
        try:
            return await self._handle(args, scope, ctx)
        except Refusal as refusal:
            return refusal.result

    async def _handle(self, args: Mapping[str, Any], scope: Scope | None, ctx: _CallContext) -> str:
        session_id = ctx.session_id
        call = normalize(args)
        scope = scope if scope is not None else scope_or_none()
        if scope is None:
            raise Refusal(
                results.rejected(
                    "No profile is active for this call, so its terminal settings are unknown; "
                    "the command was not run."
                )
            )
        steps = plan(call, scope)
        try:
            key, env = await ctx.environments.acquire(scope, session_id=session_id)
        except BackendUnavailable as exc:
            return results.backend_status(
                BackendStatus("disabled", f"Terminal backend disabled: {exc}")
            )
        except EnvironmentConnectionError as exc:
            return await self._degraded(ctx.environments, scope, session_id, exc)
        start_cwd = steps.workdir or self.session_cwd(scope, session_id) or env.cwd
        await self._guard(steps, start_cwd, env.name, scope)
        verdict = await self.gate(
            GateRequest(
                steps.command,
                scope,
                session_id,
                ctx.turn_id,
                ctx.broker,
                env.flags,
                steps.background,
                cwd=start_cwd,
                run_context=ctx.run_context,
                judge=ctx.judge,
            )
        )
        if verdict.status == "pending":
            return results.pending(
                redact_output(steps.command, scope=scope),
                verdict.description,
                verdict.pattern_key,
                allow_permanent=verdict.allow_permanent,
            )
        if verdict.status != "approved":
            return results.blocked(
                verdict.error or "Command refused.", user_summary=verdict.user_summary
            )
        notes = await mutation_hooks.before_command(
            scope,
            session_id=session_id,
            turn_id=ctx.turn_id,
            command=steps.command,
            cwd=os.path.expanduser(start_cwd),
            on_host=env.flags.host_local and not env.flags.container_paths,
        )
        if steps.background:
            started = await self._background(
                call, steps, scope, session_id, key, env, start_cwd, verdict
            )
            return mutation_hooks.with_notes(started, notes)
        finished = await self._foreground(steps, scope, ctx, env, start_cwd, verdict)
        return mutation_hooks.with_notes(finished, notes)

    async def _degraded(
        self,
        environments: EnvironmentRegistry,
        scope: Scope,
        session_id: str | None,
        exc: EnvironmentConnectionError,
    ) -> str:
        await environments.release(scope, session_id=session_id)
        if str(_config(scope, "terminal.degraded_mode", "warn")).strip().lower() == "fail":
            return results.backend_status(
                BackendStatus("error", f"Terminal backend failed: {exc.reason}")
            )
        return results.backend_status(
            BackendStatus(
                "degraded",
                f"Terminal backend degraded: {exc.reason}",
                reason=exc.reason,
                retry_hint=exc.retry_hint,
            )
        )

    async def _guard(self, steps: Plan, cwd: str, backend: str, scope: Scope) -> None:
        if not self.guards:
            return
        data = GuardInput(steps.command, steps.workdir, cwd, backend)
        deadline = max(steps.timeout, GUARD_MIN_S)
        try:
            problem = await run_blocking(
                _run_guards,
                self.guards,
                data,
                pool="tools",
                deadline=deadline,
                clock=self.clock_for(scope),
                label="terminal pre-exec guards",
            )
        except TimeoutError:
            raise Refusal(
                results.rejected(
                    f"The pre-run safety checks did not finish within {deadline:g}s, so the "
                    "command was not run. Try the call again."
                )
            ) from None
        except Exception as exc:
            log.error("terminal guard failed: %s: %s", type(exc).__name__, exc)
            raise Refusal(
                results.blocked(
                    "Blocked: a pre-run safety check failed, so the command was not run."
                )
            ) from None
        if problem:
            raise Refusal(results.blocked(problem))

    # -- foreground ------------------------------------------------------------------------------

    async def _foreground(
        self,
        steps: Plan,
        scope: Scope,
        ctx: _CallContext,
        env: Environment,
        start_cwd: str,
        verdict: GateVerdict,
    ) -> str:
        session_id = ctx.session_id
        recorded = self.session_cwd(scope, session_id)
        if steps.workdir:
            cwd: str | None = os.path.expanduser(steps.workdir)
        elif recorded and recorded != env.cwd:
            cwd = recorded  # the environment was rebuilt; the session stays where it was
        else:
            cwd = None
        clear_stale_interrupt(verdict, ctx.cancel)  # once, never inside the retries
        outcome = await results.run_foreground(
            ctx.environments,
            steps.command,
            scope=scope,
            session_id=session_id,
            timeout=steps.timeout,
            cwd=cwd,
            clock=self.clock_for(scope),
            cancel=ctx.cancel,
            broker=ctx.broker,
            turn_id=ctx.turn_id,
            interactive=run_context_of(ctx.run_context) == "interactive",
        )
        if isinstance(outcome, str):
            return outcome
        if isinstance(outcome, BackendStatus):
            return results.backend_status(outcome)
        self._record_cwd(scope, session_id, steps.workdir, outcome)
        return results.finish(
            steps.command,
            outcome,
            scope=scope,
            start_cwd=start_cwd,
            workdir=steps.workdir,
            approval_note=verdict.note,
            max_chars=int(_config(scope, "tool_output.max_chars", 50_000)),
        )

    def _record_cwd(
        self, scope: Scope, session_id: str | None, workdir: str | None, result: ExecResult
    ) -> None:
        """The session cwd moves only when this command printed its marker and ran without a
        per-call workdir (R02.10)."""
        if workdir or not result.cwd_observed:
            return
        self._cwds[(str(scope.home), session_id or "default")] = result.cwd_observed

    # -- background ------------------------------------------------------------------------------

    async def _background(
        self,
        call: Call,
        steps: Plan,
        scope: Scope,
        session_id: str | None,
        key: EnvKey,
        env: Environment,
        cwd: str,
        verdict: GateVerdict,
    ) -> str:
        run_env: Any = getattr(env, "run_env", None)
        remote = not env.flags.host_local  # the job runs inside the backend (P11-12)
        if not remote and not callable(run_env):
            return results.background_failed(
                f"background processes are not available on the {env.name} backend"
            )
        snapshot: str | None = None
        if not remote and getattr(env, "snapshot_ready", False):
            snapshot = str(getattr(env, "snapshot_path", "") or "") or None
        notes: dict[str, Any] = {}
        notify, watch, beat = call.notify_on_exit, call.watch_patterns, call.heartbeat
        if steps.promoted_from is not None:
            notify, watch = True, ()
        deliveries = self.shared.deliveries
        deliverable = deliveries.deliverable(str(scope.home), session_id)
        if (notify or watch or beat) and not deliverable:
            notes["notify_on_complete"] = False
            notes["notify_unsupported"] = NOT_DELIVERABLE_NOTE
            if beat:
                notes["heartbeat_ignored"] = "heartbeats need a session that can receive messages"
            notify, watch, beat = False, (), 0
        if notify and watch:
            notes["watch_patterns_ignored"] = WATCH_DROPPED_NOTE
            watch = ()
        use_pty = call.pty
        if use_pty and _needs_piped_stdin(steps.command):
            use_pty = False
            notes["pty_note"] = PIPED_STDIN_NOTE
        request = SpawnRequest(
            command=steps.command,
            scope=scope,
            session_id=session_id,
            env_key=str(key),
            cwd=cwd if remote else os.path.expanduser(cwd),
            env={} if remote else cast(Mapping[str, str], run_env()),
            snapshot=snapshot,
            environment=env if remote else None,
            use_pty=use_pty,
            notify_on_exit=notify,
            watch_patterns=watch,
            heartbeat_s=beat,
            persist_on_release=call.persist_on_release,
        )
        try:
            record = await self.shared.processes.spawn(request)
        except ProcessLimit as exc:
            return results.background_failed(str(exc))
        if record.exited:
            return results.background_failed(redact_output(record.buffer, scope=scope))
        extra: dict[str, Any] = {}
        if call.persist_on_release:
            extra["persist_on_release"] = True
        if verdict.note:
            extra["approval"] = verdict.note
        if record.pty_note:
            extra["pty_note"] = record.pty_note
        if steps.auto_background:
            extra["auto_backgrounded"] = AUTO_BACKGROUND_NOTE
        elif not (call.notify_on_exit or call.watch_patterns or steps.promoted_from):
            extra["hint"] = SILENT_HINT
        extra.update(notes)
        if notify:
            extra["notify_on_complete"] = True
        if record.heartbeat is not None:
            extra["heartbeat_seconds"] = record.heartbeat.every_s
        if watch:
            extra["watch_patterns"] = list(watch)
        if steps.promoted_from is not None:
            cap = int(_config(scope, "terminal.max_foreground_timeout", DEFAULT_FOREGROUND_CAP))
            template = PROMOTED_NOTE if notify else PROMOTED_POLL_NOTE
            extra["promoted_from_foreground"] = template.format(
                requested=steps.promoted_from, cap=cap
            )
        return results.background_started(record.id, record.pid, extra)

    # -- registration ----------------------------------------------------------------------------

    def register(self, registry: ToolRegistry) -> None:
        registry.register(
            TOOL_NAME,
            TOOLSET,
            TERMINAL_SCHEMA,
            self,
            probe=self.probe,
            is_async=True,
            max_result_chars=MAX_RESULT_CHARS,
            cross_refs=CROSS_REFS,
            traits=ToolTraits(mutating=True),
        )


# Named only when that tool is offered too (R02.21).
CROSS_REFS = (
    CrossRef("read_file", "To read a file use read_file rather than cat, head or tail."),
    CrossRef("patch", "To change part of a file use patch rather than sed or a heredoc."),
    CrossRef("search_files", "To search file contents or names use search_files, not grep/find."),
)

TERMINAL_SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Run a shell command (bash) on the user's machine or the configured sandbox. The working "
        "directory and exported variables carry over between calls. Output is cut to its "
        "beginning and end when it is long (the full text is saved to a file named in the "
        "result) and secrets are masked. Use background=true for servers, watchers and long "
        "jobs, and manage them with process_manage. Risky commands may need the user's approval "
        "first."
    ),
    "parameters": {
        "type": "object",
        "required": ["command"],
        "properties": {
            "command": {"type": "string", "description": "The shell command to run."},
            "background": {
                "type": "boolean",
                "default": False,
                "description": "Keep it running after this call returns; the result gives its "
                "session_id for process_manage.",
            },
            "timeout": {
                "type": "integer",
                "minimum": 1,
                "description": "Seconds before the command is stopped (default 180, foreground "
                "maximum 600). A command that finishes early returns at once, so be generous. "
                "Above 600 the command is started as a tracked background process (the result "
                "says so; do not run it again).",
            },
            "workdir": {
                "type": "string",
                "description": "Absolute directory to run this one command in.",
            },
            "pty": {
                "type": "boolean",
                "default": False,
                "description": "With background=true: run under a pseudo-terminal, for "
                "interactive programs driven with process_manage write/submit.",
            },
            "notify": {
                "description": "With background=true. true: a message when it ends (use it for "
                "anything that finishes: builds, tests, deploys). A list of texts: a message "
                "when an output line contains one, for a one-time sign on a process that keeps "
                "running (rate-limited). Leave it out for servers.",
                "anyOf": [
                    {"type": "boolean"},
                    {"type": "array", "items": {"type": "string"}},
                ],
            },
            "heartbeat": {
                "type": "integer",
                "minimum": 0,
                "default": 0,
                "description": "With background=true: every N seconds (at least 60) a message "
                "with the new output; quiet periods send nothing. Turns notify on. 0 = off.",
            },
            "persist_on_release": {
                "type": "boolean",
                "default": False,
                "description": "With background=true: keep running after the session ends or "
                "/new (not after Indus exits). Only when the user wants it to outlive the "
                "conversation.",
            },
        },
    },
}


DEFAULT_TOOL = TerminalTool()


def register_tools(registry: ToolRegistry) -> None:
    DEFAULT_TOOL.register(registry)
