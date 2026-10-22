# pyright: strict
"""The hooks bus and the event bus (P13-17).

Plugins observe and steer the agent through named hooks (generic names, D-16.39). A callback gets
keyword arguments only — every one when it takes ``**kwargs``, else just the names it declares —
and is isolated: its exception is logged (once per distinct failure) and skipped.

Synchronous callbacks run on the ``hooks`` blocking pool (D-04.1); ``async`` ones on the loop.
Hooks on the hot path (``BOUNDED``) get ``plugins.hook_callback_timeout`` (30 s; 0 = off; 600 at
most). A callback that misses it is abandoned (its thread runs on, never joined), skipped for
``SUPPRESS_S`` (60 s), and counted until its thread really returns; with ``MAX_ABANDONED`` (3)
still running it is skipped outright. A pool that refuses the work counts as a failure too.

Failure policy (R07.18): ``pre_tool_call`` fails closed — a guard that timed out, raised, is
suppressed or stuck made no decision, so the tool is blocked. Every other hook fails open.
``subagent_stop`` runs in its caller's task, never on a worker.

The event bus lets plugins publish and listen to dotted events. The host publishes ``indus.*``;
a plugin's events are ``<plugin key>.<event>``. Delivery is fire-and-forget on the emitter's
loop; a subscriber that emits carries its depth (cap 8), and at most 64 events may be queued or
running at once — beyond either cap the emit is dropped with a warning.
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import functools
import inspect
import logging
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from indus.foundation.blocking import BlockingError, BlockingPool, DeadlineMissed, run_blocking
from indus.foundation.clock import Clock
from indus.foundation.scope import Scope
from indus.plugins.settings import bounded_seconds, plugins_setting
from indus.safety.approvals.tool_gate import approve_tool_call
from indus.tools.calls import ToolCall, ToolContext, ToolResult
from indus.tools.hooks import BLOCKED, BeforeVerdict

__all__ = [
    "BOUNDED",
    "CALLER_TASK",
    "DEFAULT_TIMEOUT_S",
    "FAIL_CLOSED",
    "HOOK_NAMES",
    "MAX_ABANDONED",
    "MAX_TIMEOUT_S",
    "SUPPRESS_S",
    "AskApproval",
    "EventBus",
    "HookBus",
    "PluginToolHooks",
    "Registration",
    "ToolDirective",
    "finished_within",
    "hook_timeout",
    "narrow_kwargs",
    "with_llm_context",
]

log = logging.getLogger(__name__)

DIRECTIVE_HOOKS = frozenset(
    {
        "pre_tool_call",
        "pre_llm_call",
        "pre_verify",
        "pre_gateway_dispatch",
        "transform_api_error_classification",
    }
)
TRANSFORM_HOOKS = frozenset(
    {
        "transform_tool_result",
        "transform_terminal_output",
        "transform_llm_output",
        "pre_transcription",
    }
)
OBSERVER_HOOKS = frozenset(
    {
        "post_tool_call",
        "post_llm_call",
        "pre_api_request",
        "post_api_request",
        "api_request_error",
        "pre_auxiliary_call",
        "post_auxiliary_call",
        "on_stream_start",
        "on_stream_delta",
        "on_stream_end",
        "on_interim_message",
        "on_session_start",
        "on_session_end",
        "on_session_finalize",
        "on_session_reset",
        "on_session_delete",
        "post_compaction",
        "agent_loop_stopped",
        "on_skill_lifecycle",
        "subagent_start",
        "subagent_stop",
        "pre_approval_request",
        "post_approval_response",
        "on_human_input_request",
        "on_human_input_resolved",
        "pre_command",
        "post_gateway_admission",
        "gateway_platform_event",
    }
)
HOOK_NAMES = DIRECTIVE_HOOKS | TRANSFORM_HOOKS | OBSERVER_HOOKS

# The hot path: these get the callback deadline. Gates whose either failure mode hurts
# (pre_gateway_dispatch), last-chance flushes (on_session_finalize/reset) and approval observers
# (their prompt has its own timeout) run to completion instead.
BOUNDED = frozenset(
    {
        "pre_tool_call",
        "post_tool_call",
        "transform_tool_result",
        "transform_terminal_output",
        "transform_llm_output",
        "pre_llm_call",
        "post_llm_call",
        "pre_api_request",
        "post_api_request",
        "api_request_error",
        "pre_auxiliary_call",
        "post_auxiliary_call",
        "on_stream_start",
        "on_stream_delta",
        "on_stream_end",
        "pre_verify",
        "on_session_start",
        "on_session_end",
        "post_gateway_admission",
        "pre_command",
        "post_compaction",
    }
)
FAIL_CLOSED = frozenset({"pre_tool_call"})
CALLER_TASK = frozenset({"subagent_stop"})

DEFAULT_TIMEOUT_S = 30.0
MAX_TIMEOUT_S = 600.0
SUPPRESS_S = 60.0
MAX_ABANDONED = 3
_PRE_TOOL = "pre_tool_call"

Callback = Callable[..., Any]
AskApproval = Callable[[str, str, str], Awaitable[bool]]


# -- registrations ------------------------------------------------------------------------------


@dataclass(eq=False)
class _Entry:
    name: str
    callback: Callback
    owner: str
    suppressed_until: float = 0.0
    abandoned: int = 0


@dataclass(frozen=True, eq=False)
class Registration:
    """Handle to one registered callback; ``dispose`` removes it (idempotent)."""

    remove: Callable[[], bool]
    kind: str
    name: str
    owner: str

    def dispose(self) -> bool:
        return self.remove()


def _label(callback: Callback) -> str:
    target = getattr(callback, "func", callback)
    return str(getattr(target, "__qualname__", None) or type(target).__name__)


def _is_async(callback: Callback) -> bool:
    if inspect.iscoroutinefunction(callback):
        return True
    call = cast(object, type(callback).__dict__.get("__call__"))
    return call is not None and inspect.iscoroutinefunction(call)


def narrow_kwargs(callback: Callback, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Everything for ``**kwargs`` callbacks, else only the keyword names the callback declares."""
    try:
        params = inspect.signature(callback).parameters
    except (TypeError, ValueError):
        return dict(payload)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return dict(payload)
    named = {
        n
        for n, p in params.items()
        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    return {k: v for k, v in payload.items() if k in named}


def _resolve_on_worker(value: Any) -> Any:
    """A sync callback that handed back an awaitable: finish it on this worker thread."""
    if inspect.isawaitable(value):

        async def finish() -> Any:
            return await value

        return asyncio.run(finish())
    return value


async def finished_within(task: asyncio.Future[Any], seconds: float, clock: Clock | None) -> bool:
    """True once ``task`` is done; False when ``seconds`` passed first (on ``clock``)."""
    if clock is None:
        await asyncio.wait({task}, timeout=seconds)
        return task.done()
    timer = asyncio.ensure_future(clock.sleep(seconds))
    try:
        await asyncio.wait({task, timer}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        timer.cancel()
    return task.done()


@dataclass(frozen=True)
class _Outcome:
    value: Any = None
    failure: str | None = None  # why the callback made no decision


@dataclass
class _Run:
    """Book-keeping for one sync call on a worker, shared with that worker."""

    finished: bool = False
    counted: bool = False


@dataclass(frozen=True)
class ToolDirective:
    """The merged ``pre_tool_call`` answer: block > approve > modify (``args``) > nothing."""

    action: Literal["block", "approve"] | None = None
    message: str | None = None
    rule_key: str | None = None
    args: Mapping[str, Any] | None = None


# -- the hooks bus ------------------------------------------------------------------------------


class HookBus:
    """Callbacks per hook name, called with deadlines and the R07.18 failure policy."""

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        suppress_s: float = SUPPRESS_S,
        max_abandoned: int = MAX_ABANDONED,
        pool: str | BlockingPool = "hooks",
    ) -> None:
        self.clock = clock
        self.timeout_s = timeout_s
        self.suppress_s = suppress_s
        self.max_abandoned = max_abandoned
        self.pool = pool
        self._hooks: dict[str, list[_Entry]] = {}
        self._lock = threading.Lock()
        self._reported: set[tuple[str, str, str, str]] = set()
        self._unknown_warned: set[str] = set()
        self.events = EventBus(pool=pool)

    @classmethod
    def from_scope(
        cls, scope: Scope | None, *, clock: Clock | None = None, pool: str | BlockingPool = "hooks"
    ) -> HookBus:
        """A bus with ``plugins.hook_callback_timeout`` from the scope's config."""
        timeout = hook_timeout(scope)
        return cls(clock=clock, timeout_s=timeout, pool=pool)

    # -- registering ----------------------------------------------------------------------------

    def register(self, name: str, callback: Callback, *, owner: str = "") -> Registration:
        """Add ``callback`` for hook ``name``. An unknown name warns (once) but is stored."""
        if not callable(callback):
            raise TypeError(f"hook {name!r}: the callback must be callable")
        if name not in HOOK_NAMES and name not in self._unknown_warned:
            self._unknown_warned.add(name)
            log.warning("plugin %s registered unknown hook %r; it is kept", owner or "?", name)
        entry = _Entry(name, callback, owner)
        with self._lock:
            self._hooks.setdefault(name, []).append(entry)

        def remove() -> bool:
            with self._lock:
                entries = self._hooks.get(name, [])
                if entry not in entries:
                    return False
                entries.remove(entry)
                if not entries:
                    self._hooks.pop(name, None)
                return True

        return Registration(remove, "hook", name, owner)

    def drop_owner(self, owner: str) -> int:
        """Remove every hook and event subscription of ``owner``; returns how many went."""
        removed = 0
        with self._lock:
            for name in list(self._hooks):
                kept = [e for e in self._hooks[name] if e.owner != owner]
                removed += len(self._hooks[name]) - len(kept)
                if kept:
                    self._hooks[name] = kept
                else:
                    del self._hooks[name]
        return removed + self.events.drop_owner(owner)

    def has(self, name: str) -> bool:
        with self._lock:
            return bool(self._hooks.get(name))

    def names(self, owner: str | None = None) -> list[str]:
        with self._lock:
            return sorted(
                n
                for n, es in self._hooks.items()
                if owner is None or any(e.owner == owner for e in es)
            )

    def _snapshot(self, name: str) -> list[_Entry]:
        with self._lock:
            return list(self._hooks.get(name, ()))

    # -- calling --------------------------------------------------------------------------------

    def _now(self) -> float:
        return self.clock.monotonic() if self.clock is not None else time.monotonic()

    def _report(
        self, name: str, entry: _Entry, exc: BaseException, provided: Mapping[str, Any]
    ) -> None:
        key = (name, _label(entry.callback), type(exc).__name__, str(exc)[:200])
        if key in self._reported:
            log.debug("hook %s callback %s raised again: %s", name, key[1], exc)
            return
        self._reported.add(key)
        log.warning(
            "hook %s callback %s of plugin %s raised %s: %s (this hook provides: %s; identical "
            "failures are logged at debug from now on)",
            name,
            key[1],
            entry.owner or "?",
            type(exc).__name__,
            exc,
            ", ".join(sorted(provided)) or "nothing",
            exc_info=exc,
        )

    def _gate(self, name: str, entry: _Entry) -> str | None:
        """Why ``entry`` must be skipped now (suppressed or stuck), or None to run it."""
        with self._lock:
            if entry.suppressed_until > self._now():
                return "skipped: it timed out recently"
            if entry.abandoned >= self.max_abandoned:
                return f"skipped: {entry.abandoned} of its calls are still stuck"
        return None

    def _timed_out(self, entry: _Entry, run: _Run | None) -> None:
        with self._lock:
            entry.suppressed_until = self._now() + self.suppress_s
            if run is not None and not run.finished:
                run.counted = True
                entry.abandoned += 1

    async def _call(self, name: str, entry: _Entry, payload: Mapping[str, Any]) -> _Outcome:
        kwargs = narrow_kwargs(entry.callback, payload)
        if name in CALLER_TASK:
            return await self._call_inline(name, entry, kwargs, payload)
        bounded = name in BOUNDED and self.timeout_s > 0
        if bounded:
            why = self._gate(name, entry)
            if why is not None:
                log.warning("hook %s callback %s %s", name, _label(entry.callback), why)
                return _Outcome(failure=why)
        deadline = self.timeout_s if bounded else None
        try:
            if _is_async(entry.callback):
                return await self._call_async(name, entry, kwargs, deadline)
            return await self._call_sync(name, entry, kwargs, deadline)
        except Exception as exc:
            self._report(name, entry, exc, payload)
            return _Outcome(failure=f"raised {type(exc).__name__}")

    async def _call_inline(
        self, name: str, entry: _Entry, kwargs: dict[str, Any], payload: Mapping[str, Any]
    ) -> _Outcome:
        try:
            value = entry.callback(**kwargs)
            if inspect.isawaitable(value):
                value = await value
        except Exception as exc:
            self._report(name, entry, exc, payload)
            return _Outcome(failure=f"raised {type(exc).__name__}")
        return _Outcome(value)

    async def _call_async(
        self, name: str, entry: _Entry, kwargs: dict[str, Any], deadline: float | None
    ) -> _Outcome:
        task = asyncio.ensure_future(entry.callback(**kwargs))
        if deadline is None:
            return _Outcome(await task)
        if not await finished_within(task, deadline, self.clock):
            task.cancel()
            self._timed_out(entry, None)
            log.warning(
                "hook %s callback %s did not answer within %g s; cancelled and skipped for %g s",
                name,
                _label(entry.callback),
                deadline,
                self.suppress_s,
            )
            return _Outcome(failure=f"timed out after {deadline:g} s")
        return _Outcome(task.result())

    async def _call_sync(
        self, name: str, entry: _Entry, kwargs: dict[str, Any], deadline: float | None
    ) -> _Outcome:
        run = _Run()

        def job() -> Any:
            try:
                return _resolve_on_worker(entry.callback(**kwargs))
            finally:
                with self._lock:
                    run.finished = True
                    if run.counted:
                        entry.abandoned -= 1

        label = f"hook {name} ({_label(entry.callback)})"
        try:
            value = await run_blocking(
                job, pool=self.pool, deadline=deadline, clock=self.clock, label=label
            )
        except DeadlineMissed as missed:
            self._timed_out(entry, run if missed.started else None)
            log.warning(
                "%s did not answer within %g s; abandoned and skipped for %g s",
                label,
                deadline or 0.0,
                self.suppress_s,
            )
            return _Outcome(failure=f"timed out after {deadline or 0.0:g} s")
        except BlockingError as exc:  # pool full or closed: nothing ran
            log.warning("%s could not run: %s", label, exc)
            return _Outcome(failure="no hook worker was free")
        return _Outcome(value)

    async def invoke(self, name: str, **payload: Any) -> list[Any]:
        """Call every callback of ``name`` in registration order; their non-None answers.

        For ``pre_tool_call`` a callback that made no decision adds a block directive.
        """
        results: list[Any] = []
        for entry in self._snapshot(name):
            outcome = await self._call(name, entry, payload)
            if outcome.failure is not None:
                if name in FAIL_CLOSED:
                    results.append(
                        {
                            "action": "block",
                            "message": (
                                f"Tool blocked: plugin check {_label(entry.callback)} "
                                f"({entry.owner or 'unknown plugin'}) {outcome.failure}"
                            ),
                        }
                    )
                continue
            if outcome.value is not None:
                results.append(outcome.value)
        return results

    # -- typed helpers --------------------------------------------------------------------------

    async def pre_tool_call(
        self,
        tool_name: str,
        args: Mapping[str, Any],
        **ids: Any,
    ) -> ToolDirective:
        """Merge the ``pre_tool_call`` answers: the first block with a message wins over any
        approve (the first valid approve is kept); ``modify`` args are shallow-merged in order
        over the original arguments (a later hook wins on the same key)."""
        if not self.has(_PRE_TOOL):
            return ToolDirective()
        original = dict(args)
        answers = await self.invoke(_PRE_TOOL, tool_name=tool_name, args=dict(original), **ids)
        merged: dict[str, Any] | None = None
        approve: tuple[str | None, str | None] | None = None
        for answer in answers:
            if not isinstance(answer, Mapping):
                continue
            directive = cast(Mapping[str, Any], answer)
            action = directive.get("action")
            if action == "modify":
                partial = directive.get("args")
                if isinstance(partial, Mapping) and partial:
                    base = merged if merged is not None else original
                    merged = {**base, **cast(Mapping[str, Any], partial)}
                continue
            message = directive.get("message")
            text = message if isinstance(message, str) and message.strip() else None
            if action == "block" and text is not None:
                return ToolDirective("block", text, args=merged)
            if action == "approve" and approve is None:
                rule = directive.get("rule_key")
                approve = (text, rule.strip() or None if isinstance(rule, str) else None)
        if approve is not None:
            return ToolDirective("approve", approve[0], approve[1], merged)
        return ToolDirective(args=merged)

    async def pre_llm_call(self, **payload: Any) -> str | None:
        """Context for the user message: each answer's text (a string or ``{context}``), joined
        with blank lines in registration order; None when nobody added any."""
        parts: list[str] = []
        for answer in await self.invoke("pre_llm_call", **payload):
            text: object = answer
            if isinstance(answer, Mapping):
                text = cast(Mapping[str, Any], answer).get("context")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        return "\n\n".join(parts) if parts else None

    async def transform(self, name: str, **payload: Any) -> Any | None:
        """The first non-None answer of a transform hook (all callbacks still run)."""
        answers = await self.invoke(name, **payload)
        if len(answers) > 1:
            log.info("hook %s: %d answers, the first one wins", name, len(answers))
        return answers[0] if answers else None

    async def pre_transcription(self, **payload: Any) -> dict[str, Any]:
        """Changes to ``prompt``, ``language`` and ``model``; the last writer wins per key."""
        changes: dict[str, Any] = {}
        for answer in await self.invoke("pre_transcription", **payload):
            if isinstance(answer, Mapping):
                found = cast(Mapping[str, Any], answer)
                changes.update({k: found[k] for k in ("prompt", "language", "model") if k in found})
        return changes

    async def pre_verify(self, **payload: Any) -> str | None:
        """The first ``{action: continue, message}`` message: keep the turn going with it."""
        for answer in await self.invoke("pre_verify", **payload):
            if isinstance(answer, Mapping):
                found = cast(Mapping[str, Any], answer)
                message = found.get("message")
                if (
                    found.get("action") == "continue"
                    and isinstance(message, str)
                    and message.strip()
                ):
                    return message.strip()
        return None

    async def pre_gateway_dispatch(self, **payload: Any) -> dict[str, Any] | None:
        """The first valid ``skip {reason}`` or ``rewrite {text}`` directive; None lets it pass."""
        for answer in await self.invoke("pre_gateway_dispatch", **payload):
            if not isinstance(answer, Mapping):
                continue
            found = cast(Mapping[str, Any], answer)
            action = found.get("action")
            if action == "skip":
                return {"action": "skip", "reason": str(found.get("reason") or "")}
            if action == "rewrite" and isinstance(found.get("text"), str):
                return {"action": "rewrite", "text": found["text"]}
        return None

    async def classify_api_error(self, **payload: Any) -> dict[str, Any] | None:
        """The first answer carrying a ``reason``; flags kept as booleans, message ≤ 500 chars."""
        valid: list[dict[str, Any]] = []
        for answer in await self.invoke("transform_api_error_classification", **payload):
            if not isinstance(answer, Mapping):
                continue
            found = cast(Mapping[str, Any], answer)
            reason = found.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                continue
            clean: dict[str, Any] = {"reason": reason.strip()}
            for flag in (
                "retryable",
                "should_compress",
                "should_rotate_credential",
                "should_fallback",
            ):
                if isinstance(found.get(flag), bool):
                    clean[flag] = found[flag]
            if isinstance(found.get("message"), str):
                clean["message"] = found["message"][:500]
            valid.append(clean)
        if len(valid) > 1:
            log.warning("%d plugins classified one API error; the first one wins", len(valid))
        return valid[0] if valid else None

    async def observe(self, name: str, **payload: Any) -> None:
        """Run an observer hook; answers are ignored."""
        if self.has(name):
            await self.invoke(name, **payload)


def hook_timeout(scope: Scope | None) -> float:
    """``plugins.hook_callback_timeout`` (30 s default; 0 = off; clamped to 600)."""
    raw = plugins_setting(scope, "hook_callback_timeout", None)
    return bounded_seconds(
        raw,
        name="plugins.hook_callback_timeout",
        default=DEFAULT_TIMEOUT_S,
        maximum=MAX_TIMEOUT_S,
    )


def with_llm_context(message: Mapping[str, Any], context: str | None) -> dict[str, Any]:
    """The user message with ``pre_llm_call`` context added to what is sent (``api_content``).

    ``content`` (what the person typed) is kept; the system prompt is never touched, so the
    provider's prompt cache stays valid (R07.18, R01.1). Multimodal content gets one extra text
    part.
    """
    staged = dict(message)
    if not context:
        return staged
    sent = staged.get("api_content", staged.get("content"))
    if isinstance(sent, list):
        parts = list(cast(list[Any], sent))
        staged["api_content"] = [*parts, {"type": "text", "text": context}]
    else:
        text = sent if isinstance(sent, str) else ""
        staged["api_content"] = f"{text}\n\n{context}" if text else context
    return staged


# -- the ToolHooks seam -------------------------------------------------------------------------


class PluginToolHooks:
    """P04's ``ToolHooks`` seam backed by the bus: ``pre_tool_call``, ``post_tool_call`` and
    ``transform_tool_result``.

    ``ask_approval(tool_name, message, rule_key)`` is the human gate an ``approve`` directive goes
    to. Without one, the directive asks through the P05 per-tool gate
    (``safety.approvals.tool_gate``) with the call's own profile, session, broker and run
    context; nobody to ask means the context's unattended policy (deny by default). A denial or a
    gate that fails blocks the call.
    """

    # Each callback has its own deadline; no extra backstop around the whole seam.
    before_deadline_s: float | None = None

    def __init__(self, bus: HookBus, *, ask_approval: AskApproval | None = None) -> None:
        self.bus = bus
        self.ask_approval = ask_approval

    @property
    def empty(self) -> bool:
        return not (
            self.bus.has(_PRE_TOOL)
            or self.bus.has("post_tool_call")
            or self.bus.has("transform_tool_result")
        )

    @staticmethod
    def _ids(call: ToolCall, ctx: ToolContext) -> dict[str, Any]:
        return {
            "task_id": ctx.task_id or "",
            "session_id": ctx.session_id or "",
            "turn_id": ctx.turn_id or "",
            "tool_call_id": call.call_id,
        }

    async def before(self, call: ToolCall, ctx: ToolContext) -> BeforeVerdict | None:
        directive = await self.bus.pre_tool_call(call.name, call.args, **self._ids(call, ctx))
        if directive.action == "block":
            return BeforeVerdict(block=directive.message)
        if directive.action == "approve":
            refusal = await self._approve(call.name, directive, ctx)
            if refusal is not None:
                return BeforeVerdict(block=refusal)
        if directive.args is not None:
            return BeforeVerdict(args=directive.args)
        return None

    async def _approve(
        self, tool_name: str, directive: ToolDirective, ctx: ToolContext
    ) -> str | None:
        if self.ask_approval is None:
            extras = ctx.extras
            verdict = await approve_tool_call(
                f"{tool_name} (a plugin asked for approval)",
                directive.message or f"a plugin hook asked for approval of {tool_name}",
                f"plugin:{directive.rule_key or tool_name}",
                scope=ctx.scope,
                session_id=ctx.session_id,
                turn_id=ctx.turn_id,
                broker=extras.get("broker"),
                run_context=extras.get("run_context"),
                judge=extras.get("approval_judge"),
            )
            if verdict.approved:
                return None
            return verdict.message or f"Tool blocked: {tool_name} was not approved"
        try:
            approved = await self.ask_approval(
                tool_name, directive.message or "", directive.rule_key or tool_name
            )
        except Exception as exc:
            log.warning("approval for %s failed (%s); the call is blocked", tool_name, exc)
            return f"Tool blocked: the approval check for {tool_name} failed"
        if approved is not True:
            return directive.message or f"Tool blocked: {tool_name} was not approved"
        return None

    async def after(
        self, call: ToolCall, ctx: ToolContext, result: ToolResult, duration_s: float
    ) -> None:
        await self.bus.observe(
            "post_tool_call",
            tool_name=call.name,
            args=dict(call.args),
            result=result.text,
            is_error=result.is_error,
            status="error" if result.is_error else "ok",
            error_type=result.error_type or "",
            blocked=result.error_type == BLOCKED,
            duration_ms=int(duration_s * 1000),
            **self._ids(call, ctx),
        )

    async def transform(
        self, call: ToolCall, ctx: ToolContext, result: ToolResult
    ) -> ToolResult | None:
        if not self.bus.has("transform_tool_result"):
            return None
        replaced = await self.bus.transform(
            "transform_tool_result",
            tool_name=call.name,
            args=dict(call.args),
            result=result.text,
            is_error=result.is_error,
            status="error" if result.is_error else "ok",
            error_type=result.error_type or "",
            **self._ids(call, ctx),
        )
        return ToolResult.ok(replaced) if isinstance(replaced, str) else None


# -- the event bus ------------------------------------------------------------------------------

EMIT_DEPTH_CAP = 8
PENDING_CAP = 64
HOST_NAMESPACE = "indus"
_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar("indus_plugin_event_depth", default=0)


@dataclass(eq=False)
class _Listener:
    callback: Callback
    owner: str


@dataclass
class EventBus:
    """Fire-and-forget events between plugins and the host."""

    pool: str | BlockingPool = "hooks"
    depth_cap: int = EMIT_DEPTH_CAP
    pending_cap: int = PENDING_CAP
    _listeners: dict[str, list[_Listener]] = field(default_factory=dict[str, list[_Listener]])
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _pending: int = 0
    _loop: asyncio.AbstractEventLoop | None = None
    _tasks: set[asyncio.Task[None]] = field(default_factory=set[asyncio.Task[None]])
    _reported: set[tuple[str, str, str]] = field(default_factory=set[tuple[str, str, str]])

    @property
    def pending(self) -> int:
        with self._lock:
            return self._pending

    def listen(self, name: str, callback: Callback, *, owner: str = "") -> Registration:
        if not name:
            raise ValueError("an event name must be a non-empty string")
        if not callable(callback):
            raise TypeError("an event listener must be callable")
        listener = _Listener(callback, owner)
        with self._lock:
            self._listeners.setdefault(name, []).append(listener)

        def remove() -> bool:
            with self._lock:
                listeners = self._listeners.get(name, [])
                if listener not in listeners:
                    return False
                listeners.remove(listener)
                if not listeners:
                    self._listeners.pop(name, None)
                return True

        return Registration(remove, "event", name, owner)

    def drop_owner(self, owner: str) -> int:
        removed = 0
        with self._lock:
            for name in list(self._listeners):
                kept = [x for x in self._listeners[name] if x.owner != owner]
                removed += len(self._listeners[name]) - len(kept)
                if kept:
                    self._listeners[name] = kept
                else:
                    del self._listeners[name]
        return removed

    def emit(self, name: str, payload: Mapping[str, Any] | None = None) -> int:
        """Queue ``name`` for its listeners; returns how many were scheduled (0 when dropped).

        Never waits. Called on a loop thread it schedules there; from a worker thread it uses the
        loop the bus last ran on.
        """
        depth = _DEPTH.get()
        if depth >= self.depth_cap:
            log.warning(
                "event %s dropped: emitted %d levels deep (cap %d)", name, depth + 1, self.depth_cap
            )
            return 0
        try:
            loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        with self._lock:
            listeners = tuple(self._listeners.get(name, ()))
            if not listeners:
                return 0
            if self._pending >= self.pending_cap:
                log.warning("event %s dropped: %d events already pending", name, self._pending)
                return 0
            target = loop or self._loop
            if target is None or target.is_closed():
                log.warning("event %s dropped: no running event loop to deliver it on", name)
                return 0
            self._loop = target
            self._pending += 1
        data = dict(payload or {})
        context = contextvars.copy_context()
        context.run(_DEPTH.set, depth + 1)

        def start() -> None:
            task = target.create_task(self._deliver(name, data, listeners), context=context)
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        if loop is target:
            start()
        else:
            try:
                target.call_soon_threadsafe(start)
            except RuntimeError:
                with self._lock:
                    self._pending -= 1
                return 0
        return len(listeners)

    async def _deliver(
        self, name: str, payload: dict[str, Any], listeners: tuple[_Listener, ...]
    ) -> None:
        try:
            for listener in listeners:
                with self._lock:
                    if listener not in self._listeners.get(name, []):
                        continue  # unsubscribed after the emit
                data = copy.deepcopy(payload)
                try:
                    if _is_async(listener.callback):
                        await listener.callback(**data)
                    else:
                        await run_blocking(
                            functools.partial(listener.callback, **data),
                            pool=self.pool,
                            label=f"event {name}",
                        )
                except Exception as exc:
                    key = (name, _label(listener.callback), type(exc).__name__)
                    if key not in self._reported:
                        self._reported.add(key)
                        log.warning("event %s listener %s raised %s: %s", name, key[1], key[2], exc)
        finally:
            with self._lock:
                self._pending -= 1

    async def drain(self, *, poll_s: float = 0.001, limit_s: float = 5.0) -> bool:
        """Wait (real time, tests and shutdown) until nothing is pending; False at the limit."""
        loop = asyncio.get_running_loop()
        started = loop.time()
        while self.pending:
            if loop.time() - started >= limit_s:
                return False
            never = loop.create_future()
            await asyncio.wait({never}, timeout=poll_s)
            never.cancel()
        return True
