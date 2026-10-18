"""The dispatch contract: one parsed tool call in, one bounded ``ToolResult`` out.

Steps: refuse names the agent loop runs itself (the inline table) → look the tool up (unknown →
``Unknown tool: <name>``) → repair argument types (``coerce``) → the ``before`` hook → run the
handler with only the context keywords its signature declares (R02.28): async handlers are
awaited, sync handlers run on a worker thread with the entry's deadline → check the result
contract → ``after`` and ``transform`` hooks.

Nothing a handler does escapes as an exception: a raise becomes
``Tool failed: <Type>: <message>`` with chat framing removed and the text bounded; a result that is
neither text nor the multimodal envelope becomes a contract error.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import inspect
import logging
import re
import time
from collections.abc import Callable, Container, Mapping
from typing import Any, cast

from indus.tools.calls import (
    MAX_LOGGED_ERROR_CHARS,
    ToolCall,
    ToolContext,
    ToolResult,
    bound_error_field,
    is_envelope,
)
from indus.tools.coerce import coerce_args
from indus.tools.hooks import NO_HOOKS, ToolHooks, run_after, run_before, run_transform
from indus.tools.registry import ToolEntry, ToolRegistry

log = logging.getLogger(__name__)

CONTRACT_ERROR = "tool_result_contract"

# Tools that need live agent state; the agent loop runs them through its own table (wave 6).
INLINE_TOOL_NAMES: frozenset[str] = frozenset(
    {"todo_list", "memory", "session_search", "delegate_task"}
)


class InlineTable[E]:
    """Name → executor for agent-state tools: a lookup table, never an if-chain.

    The core fills it with its executors; ``dispatch`` refuses every name in it.
    """

    def __init__(self, executors: Mapping[str, E] | None = None) -> None:
        self._executors: dict[str, E] = dict(executors or {})

    def add(self, name: str, executor: E) -> None:
        self._executors[name] = executor

    def get(self, name: str) -> E | None:
        return self._executors.get(name)

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._executors)

    def __contains__(self, name: object) -> bool:
        return name in self._executors

    def __len__(self) -> int:
        return len(self._executors)


# -- error text ---------------------------------------------------------------------------------

_FRAMING_TAGS = (
    "tool_call",
    "tool_calls",
    "tool_result",
    "tool_use",
    "function_call",
    "function_results",
    "system",
    "user",
    "assistant",
    "result",
    "output",
    "input",
    "response",
)
_STRIP = (
    re.compile(r"</?(?:" + "|".join(_FRAMING_TAGS) + r")\s*>", re.IGNORECASE),
    re.compile(r"<\|[A-Za-z0-9_]+\|>"),  # chat-template special tokens
    re.compile(r"<!\[CDATA\[|\]\]>"),
    re.compile(r"```[A-Za-z0-9_+-]*"),  # code fences, with or without a language
)


def clean_error_text(text: str) -> str:
    """Remove chat framing (role tags, template tokens, CDATA markers, fence lines)."""
    for pattern in _STRIP:
        text = pattern.sub("", text)
    return text.strip()


def failure_text(exc: BaseException) -> str:
    return clean_error_text(f"Tool failed: {type(exc).__name__}: {exc}")


# -- running handlers ---------------------------------------------------------------------------


def narrow_kwargs(handler: Callable[..., Any], offered: Mapping[str, Any]) -> dict[str, Any]:
    """``offered`` cut down to the keywords ``handler`` declares; all of it for ``**kwargs``."""
    try:
        params = inspect.signature(handler).parameters
    except (TypeError, ValueError):
        return dict(offered)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return dict(offered)
    named = {
        name
        for name, p in params.items()
        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    first = next(iter(params), None)
    named.discard(first or "")  # the first parameter receives the arguments dict
    return {k: v for k, v in offered.items() if k in named}


async def run_sync(fn: Callable[[], Any], deadline_s: float | None) -> Any:
    """Run blocking ``fn`` on the loop's bounded worker pool, in a copy of this context.

    Past ``deadline_s`` the caller gets ``TimeoutError``; the thread is abandoned, not killed.
    """
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    future = loop.run_in_executor(None, ctx.run, fn)
    if deadline_s is None:
        return await future
    return await asyncio.wait_for(future, deadline_s)


async def _invoke(entry: ToolEntry, args: dict[str, Any], kwargs: dict[str, Any]) -> Any:
    if entry.is_async:
        produced = entry.handler(args, **kwargs)
        if not inspect.isawaitable(produced):
            return produced
        awaitable = cast(Any, produced)
        if entry.timeout_s is None:
            return await awaitable
        return await asyncio.wait_for(awaitable, entry.timeout_s)
    return await run_sync(functools.partial(entry.handler, args, **kwargs), entry.timeout_s)


def check_result(name: str, raw: object) -> ToolResult:
    if isinstance(raw, str):
        return ToolResult.ok(bound_error_field(raw))
    if is_envelope(raw):
        return ToolResult.ok(cast(Mapping[str, Any], raw))
    kind = type(raw).__name__
    log.error("tool %s returned %s, which is neither text nor a multimodal envelope", name, kind)
    return ToolResult.error(
        f"Tool returned a {kind} instead of text or a multimodal envelope",
        error_type=CONTRACT_ERROR,
        tool=name,
    )


def _now(ctx: ToolContext) -> float:
    return ctx.scope.clock.monotonic() if ctx.scope is not None else time.monotonic()


async def dispatch(
    call: ToolCall,
    ctx: ToolContext,
    *,
    registry: ToolRegistry,
    hooks: ToolHooks = NO_HOOKS,
    inline: Container[str] = INLINE_TOOL_NAMES,
) -> ToolResult:
    if call.name in inline:
        return ToolResult.error(
            f"{call.name} is run by the agent loop and cannot be dispatched as a registry tool",
            error_type="inline_tool",
        )
    entry = registry.get(call.name, ctx.scope)
    if entry is None:
        return ToolResult.error(f"Unknown tool: {call.name}", error_type="unknown_tool")
    args = coerce_args(call.args, entry.parameters)
    prepared = ToolCall(call.name, args, call.call_id)
    verdict = await run_before(hooks, prepared, ctx)
    if verdict.block is not None:
        return ToolResult.error(verdict.block, error_type="blocked")
    if verdict.args is not None:
        args = dict(verdict.args)
        prepared = ToolCall(call.name, args, call.call_id)
    kwargs = narrow_kwargs(entry.handler, ctx.handler_kwargs(prepared))
    started = _now(ctx)
    try:
        raw = await _invoke(entry, args, kwargs)
    except TimeoutError:
        result = ToolResult.error(
            f"Tool failed: TimeoutError: {entry.name} did not finish within {entry.timeout_s:g} s",
            error_type="timeout",
        )
    except Exception as exc:
        text = f"{type(exc).__name__}: {exc}"
        log.error("tool %s raised %s", entry.name, text[:MAX_LOGGED_ERROR_CHARS], exc_info=exc)
        result = ToolResult.error(failure_text(exc), error_type="exception")
    else:
        result = check_result(entry.name, raw)
    await run_after(hooks, prepared, ctx, result, max(0.0, _now(ctx) - started))
    return await run_transform(hooks, prepared, ctx, result)
