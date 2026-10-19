# pyright: strict
"""The ``execute_code`` tool: Python that calls Indus tools (P10-08 … P10-11).

One call: check the arguments (``command`` instead of ``code`` points to ``terminal``) → read the
settings from the scope → take the session's terminal environment → whole-script approval
(``gate``, E9) → build the in-script tool list (the session's tools ∩ ``schema.ALLOWED``) and a
dispatcher that runs each in-script call through ``tools.dispatch`` with this call's context →
run it:

- on this machine: in the session kernel (``kernel``, the default) or as a fresh script
  (``runner``, ``code_execution.kernel: off``);
- elsewhere (Docker, SSH, …): ``remote`` (a remote kernel when one starts, else one shipped
  script).

Then ``runner.finish_result`` writes the JSON result. Context keywords beyond the usual ones:
``tool_registry``, ``tool_hooks`` and ``enabled_tools`` (offered by the turn loop) and
``code_kernel_owner`` (set for delegated children, P10-06).
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import threading
from collections.abc import Iterable, Mapping
from typing import Any, cast

from indus.backends.contract import BackendUnavailable, CancelSignal, EnvironmentConnectionError
from indus.backends.local import nearest_usable_dir
from indus.backends.registry import EnvironmentRegistry
from indus.foundation.clock import Clock
from indus.foundation.scope import Scope, scope_or_none
from indus.tools.builtin.code import gate, kernel, launch, remote, runner, schema
from indus.tools.builtin.code.rpc import RpcPipeline, ToolInvoker
from indus.tools.builtin.code.settings import (
    MAX_RESULT_CHARS,
    TOOL_NAME,
    TOOLSET,
    CodeSettings,
    Mode,
)
from indus.tools.calls import ToolCall, ToolContext
from indus.tools.dispatch import dispatch
from indus.tools.hooks import NO_HOOKS, ToolHooks
from indus.tools.registry import ToolRegistry, ToolTraits

log = logging.getLogger(__name__)

SETTINGS_KEY = "x-indus-code"  # carried from schema_overrides to rewrite, never sent

MISROUTED_COMMAND = (
    "execute_code takes Python source in 'code'; this call sent 'command'. For a shell command "
    "call terminal(command=...); for Python call execute_code(code=...)."
)


def _error(message: str, **extra: Any) -> str:
    return runner.error_result(message, **extra)


def _session_cwd(scope: Scope, session_id: str | None, fallback: str) -> str:
    from indus.tools.builtin import terminal

    recorded = terminal.DEFAULT_TOOL.session_cwd(scope, session_id)
    return nearest_usable_dir(os.path.expanduser(recorded or fallback))


class ExecuteCodeTool:
    """The handler (see the module notes). ``clock`` overrides the scope's clock in tests."""

    def __init__(self, *, clock: Clock | None = None) -> None:
        self._clock = clock
        self._fallback_registry: ToolRegistry | None = None
        self._lock = threading.Lock()
        self._call_ids = itertools.count(1)

    # -- pieces ---------------------------------------------------------------------------------

    def registry_for(self, offered: object) -> ToolRegistry:
        if isinstance(offered, ToolRegistry):
            return offered
        with self._lock:
            if self._fallback_registry is None:
                from indus.tools.builtin import load_builtins

                made = ToolRegistry()
                load_builtins(made)
                self._fallback_registry = made
            return self._fallback_registry

    def tools_for(self, enabled: object, registry: ToolRegistry, scope: Scope) -> tuple[str, ...]:
        if isinstance(enabled, Iterable) and not isinstance(enabled, str | bytes):
            names = [n for n in cast(Iterable[object], enabled) if isinstance(n, str)]
        else:
            names = registry.names(scope)
        present = set(registry.names(scope))
        return schema.allowed_for(n for n in names if n in present)

    def invoker(self, registry: ToolRegistry, hooks: ToolHooks, ctx: ToolContext) -> ToolInvoker:
        async def invoke(name: str, args: dict[str, Any]) -> str:
            call = ToolCall(name, args, f"code_{next(self._call_ids)}")
            result = await dispatch(call, ctx, registry=registry, hooks=hooks)
            return result.text

        return invoke

    # -- the call -------------------------------------------------------------------------------

    async def __call__(
        self,
        args: Mapping[str, Any],
        *,
        scope: Scope | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        turn_id: str | None = None,
        cancel: CancelSignal | None = None,
        broker: object | None = None,
        environments: EnvironmentRegistry | None = None,
        run_context: object | None = None,
        approval_judge: object | None = None,
        tool_registry: object | None = None,
        tool_hooks: object | None = None,
        enabled_tools: object | None = None,
        code_kernel_owner: str | None = None,
    ) -> str:
        if "code" not in args and "command" in args:
            return _error(MISROUTED_COMMAND)
        code = args.get("code")
        if not isinstance(code, str):
            return _error(
                f"'code' must be Python source text, got {type(code).__name__}. Retry as "
                'execute_code(code="...").'
            )
        if not code.strip():
            return _error(
                "No code was given. Send Python source in 'code'; for a shell command use "
                "terminal(command=...)."
            )
        scope = scope if scope is not None else scope_or_none()
        if scope is None:
            return _error("No profile is active for this call, so nothing was run.")
        reset = args.get("reset") is True or str(args.get("reset", "")).strip().lower() == "true"
        settings = CodeSettings.from_scope(scope)
        clock = self._clock if self._clock is not None else scope.clock
        if environments is None:
            from indus.tools.builtin import terminal

            environments = terminal.runtime().environments
        try:
            env_key, env = await environments.acquire(scope, session_id=session_id)
        except BackendUnavailable as exc:
            return _error(f"The terminal backend is not available, so nothing was run: {exc}")
        except EnvironmentConnectionError as exc:
            return _error(
                f"The terminal backend could not be reached, so nothing was run: {exc.reason}"
            )
        verdict = await gate.approve_script(
            gate.GateRequest(
                code=code,
                scope=scope,
                session_id=session_id,
                turn_id=turn_id,
                broker=broker,
                run_context=run_context,
                isolated=env.flags.isolated,
                judge=approval_judge,
            )
        )
        if not verdict.approved:
            return _error(
                verdict.error or "The script was not approved.", user_summary=verdict.user_summary
            )
        registry = self.registry_for(tool_registry)
        hooks = cast(ToolHooks, tool_hooks) if tool_hooks is not None else NO_HOOKS
        tools = self.tools_for(enabled_tools, registry, scope)
        extras: dict[str, Any] = {"cancel": cancel, "environments": environments}
        for name, value in (
            ("broker", broker),
            ("run_context", run_context),
            ("approval_judge", approval_judge),
        ):
            if value is not None:
                extras[name] = value
        ctx = ToolContext(
            scope=scope, task_id=task_id, session_id=session_id, turn_id=turn_id, extras=extras
        )
        pipeline = RpcPipeline(
            token="",  # set by the path that owns the transport
            allowed=frozenset(tools),
            max_calls=settings.max_tool_calls,
            invoke=self.invoker(registry, hooks, ctx),
            clock=clock,
        )
        owner = code_kernel_owner or session_id or task_id or "default"
        try:
            if env.flags.host_local:
                outcome = await self._local(
                    code,
                    scope,
                    session_id,
                    env.cwd,
                    tools,
                    settings,
                    pipeline,
                    clock,
                    cancel,
                    reset,
                    owner,
                )
            else:
                outcome = await remote.run_remote(
                    remote.RemoteJob(
                        code=code,
                        scope=scope,
                        env=env,
                        env_key=str(env_key),
                        owner=owner,
                        mode=settings.mode,
                        tools=tools,
                        settings=settings,
                        reset=reset,
                        pipeline=pipeline,
                        clock=clock,
                        cancel=cancel,
                    )
                )
        except remote.RemoteSetupError as exc:
            return _error(str(exc), calls=pipeline.calls_made)
        result = runner.finish_result(outcome, scope, tools)
        if verdict.note:
            return _with_note(result, verdict.note)
        return result

    async def _local(
        self,
        code: str,
        scope: Scope,
        session_id: str | None,
        env_cwd: str,
        tools: tuple[str, ...],
        settings: CodeSettings,
        pipeline: RpcPipeline,
        clock: Clock,
        cancel: CancelSignal | None,
        reset: bool,
        owner: str,
    ) -> runner.RunOutcome:
        mode: Mode = settings.mode
        interpreter = await launch.choose_interpreter(scope, mode)
        cwd = _session_cwd(scope, session_id, env_cwd) if mode == "project" else None
        if settings.kernel == "off":
            return await runner.run_script(
                runner.ScriptJob(
                    code=code,
                    scope=scope,
                    session_id=session_id,
                    tools=tools,
                    settings=settings,
                    interpreter=interpreter,
                    cwd=cwd,
                    invoke=pipeline.invoke,
                    clock=clock,
                    cancel=cancel,
                    mode=mode,
                )
            )
        key = kernel.KernelKey(owner, "local", mode, interpreter, cwd or "", tools)

        async def make() -> kernel.LocalKernel:
            return await kernel.start_local_kernel(
                scope,
                session_id=session_id,
                interpreter=interpreter,
                cwd=cwd,
                tools=tools,
                idle_s=settings.kernel_idle_s,
                clock=clock,
            )

        job = kernel.CellJob(
            code=code,
            scope=scope,
            session_id=session_id,
            key=key,
            reset=reset,
            timeout_s=settings.timeout_s,
            idle_s=settings.kernel_idle_s,
            cap=settings.max_kernels,
            pipeline=pipeline,
            clock=clock,
            cancel=cancel,
        )
        return await kernel.run_cell(kernel.POOL, job, make)

    # -- registration ---------------------------------------------------------------------------

    def register(self, registry: ToolRegistry) -> None:
        registry.register(
            TOOL_NAME,
            TOOLSET,
            schema.build_schema(schema.ALLOWED),
            self,
            is_async=True,
            emoji="🐍",
            max_result_chars=MAX_RESULT_CHARS,
            schema_overrides=_overrides,
            rewrite=_rewrite,
            traits=ToolTraits(mutating=True),
        )


def _with_note(result: str, note: str) -> str:
    body = json.loads(result)
    body["approval"] = note
    return json.dumps(body, ensure_ascii=False)


def _overrides(scope: Scope | None) -> Mapping[str, Any]:
    settings = CodeSettings.from_scope(scope)
    return {SETTINGS_KEY: {"mode": settings.mode, "kernel": settings.kernel != "off"}}


def _rewrite(function: dict[str, Any], present: frozenset[str]) -> dict[str, Any]:
    carried = function.pop(SETTINGS_KEY, None)
    info: Mapping[str, Any] = (
        cast(Mapping[str, Any], carried) if isinstance(carried, Mapping) else {}
    )
    mode: Mode = "strict" if info.get("mode") == "strict" else "project"
    built = schema.build_schema(
        schema.allowed_for(present), mode, kernel=bool(info.get("kernel", True))
    )
    return {**function, "description": built["description"], "parameters": built["parameters"]}


DEFAULT_TOOL = ExecuteCodeTool()


def register_tools(registry: ToolRegistry) -> None:
    DEFAULT_TOOL.register(registry)


__all__ = ["DEFAULT_TOOL", "MISROUTED_COMMAND", "ExecuteCodeTool", "register_tools"]
