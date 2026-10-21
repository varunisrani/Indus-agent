"""``browser_exec`` (P24-39): a scripted step list run through an external browser-automation CLI.

The model sends a short list of steps from a fixed command set; each step becomes one call of the
external ``agent-browser`` CLI (a separate program found through ``indus pm``'s ``tool_path``,
then the scope's PATH; never imported or bundled) under one named session::

    agent-browser --session <name> --json <command> [args...]

Steps: ``open {url}``, ``snapshot``, ``screenshot``, ``eval {code}``, ``click {ref}``,
``type {ref, text}`` (the CLI's ``fill``), ``press {key}``, ``scroll {direction}``, ``back``. At
most ``MAX_STEPS`` steps; the call stops at the first failing step. Before anything runs, every
``open`` URL and every ``http(s)`` literal in ``eval`` code passes the P05 URL policy under the
call's scope. The CLI gets the scope's credential-free browser environment (R02.22), runs in its
own process group, and is killed with its group when the call's time is up (``timeout_s``, 300 s
by default, clamped to 5–1,800 s, shared by all steps). Screenshots are written under
``<home>/cache/browser/exec/``.

Output is budgeted like any tool's: each step's output and error text is cut to
``STEP_OUTPUT_CAP`` characters and redacted, and the tool is registered with
``MAX_RESULT_CHARS``, past which the tool round spills the whole result to a file.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import signal
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from indus.foundation.scope import Scope, scope_or_none
from indus.safety.redaction import redact_output
from indus.safety.url_policy import Blocked, Resolver, check_url_async, normalize_url
from indus.tools.builtin.browser.engines import locate_tool
from indus.tools.builtin.browser.session import browser_env
from indus.tools.builtin.browser.tools import as_json, fail, js_url_literals, parse_ref
from indus.tools.registry import ToolRegistry, ToolTraits

__all__ = [
    "CLI_NAME",
    "MAX_RESULT_CHARS",
    "SCHEMA",
    "BrowserExec",
    "RunOutcome",
    "SubprocessRunner",
    "clamp_timeout",
    "register_exec_tool",
]

log = logging.getLogger(__name__)

TOOL_NAME = "browser_exec"
TOOLSET = "browser"
CLI_NAME = "agent-browser"
MAX_STEPS = 25
STEP_OUTPUT_CAP = 4000
MAX_RESULT_CHARS = 20_000
DEFAULT_TIMEOUT_S = 300
MIN_TIMEOUT_S = 5
MAX_TIMEOUT_S = 1800
KILL_DRAIN_S = 10.0
INSTALL_HINT = (
    "browser_exec needs the agent-browser CLI. Install it with `indus pm install agent-browser` "
    "(or put `agent-browser` on PATH), or use the browser_* tools instead."
)
_SESSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SCROLLS = frozenset({"up", "down", "left", "right"})
_KEY = re.compile(r"^[A-Za-z0-9+_ -]{1,40}$")

SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Run a short scripted list of browser steps through the external agent-browser CLI, in "
        "one named session that persists between calls. Steps (in order): open {url}, snapshot, "
        "screenshot, eval {code}, click {ref}, type {ref, text}, press {key}, scroll "
        "{direction: up|down|left|right}, back. Refs come from a snapshot (e.g. @e3). The call "
        "stops at the first failing step. Use the browser_* tools for single actions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_STEPS,
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": [
                                "open",
                                "snapshot",
                                "screenshot",
                                "eval",
                                "click",
                                "type",
                                "press",
                                "scroll",
                                "back",
                            ],
                        },
                        "url": {"type": "string"},
                        "code": {"type": "string"},
                        "ref": {"type": "string"},
                        "text": {"type": "string"},
                        "key": {"type": "string"},
                        "direction": {"type": "string"},
                    },
                    "required": ["action"],
                },
            },
            "session": {
                "type": "string",
                "description": "Name of an isolated browser session to reuse across calls "
                "(1-64 letters, digits, dashes or underscores). Omit for this task's session.",
            },
            "timeout_s": {
                "type": "integer",
                "description": f"Seconds for the whole call (default {DEFAULT_TIMEOUT_S}, "
                f"max {MAX_TIMEOUT_S}).",
            },
        },
        "required": ["steps"],
    },
}


@dataclass(frozen=True)
class RunOutcome:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False


class CommandRunner(Protocol):
    async def run(
        self, argv: Sequence[str], env: Mapping[str, str], timeout_s: float
    ) -> RunOutcome: ...


class SubprocessRunner:
    """Runs the CLI in its own process group; the whole group is killed at the deadline."""

    async def run(
        self, argv: Sequence[str], env: Mapping[str, str], timeout_s: float
    ) -> RunOutcome:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=dict(env),
            start_new_session=sys.platform != "win32",
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout_s)
        except TimeoutError:
            self._kill(proc)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.communicate(), KILL_DRAIN_S)
            return RunOutcome(None, "", "", timed_out=True)
        return RunOutcome(
            proc.returncode,
            out.decode("utf-8", errors="replace"),
            err.decode("utf-8", errors="replace"),
        )

    @staticmethod
    def _kill(proc: asyncio.subprocess.Process) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            if sys.platform != "win32":
                os.killpg(proc.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            else:
                proc.kill()


def clamp_timeout(value: object) -> int:
    if isinstance(value, bool):
        return DEFAULT_TIMEOUT_S
    try:
        number = int(cast(Any, value))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_S
    return max(MIN_TIMEOUT_S, min(number, MAX_TIMEOUT_S))


def _cap(text: str, scope: Scope) -> str:
    clean = redact_output(text.strip(), force=True, scope=scope)
    if len(clean) <= STEP_OUTPUT_CAP:
        return clean
    return clean[:STEP_OUTPUT_CAP] + f"\n… ({len(clean) - STEP_OUTPUT_CAP:,} more characters cut)"


class StepError(ValueError):
    pass


@dataclass(frozen=True)
class Step:
    action: str
    argv: tuple[str, ...]
    urls: tuple[str, ...] = ()


def _text(raw: Mapping[str, Any], key: str, action: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise StepError(f"step '{action}' needs '{key}'")
    return value


def parse_step(raw: object, index: int, shots: Callable[[int], Path]) -> Step:
    """One model step → the CLI's argv tail (``StepError`` when it is not a valid step)."""
    if not isinstance(raw, Mapping):
        raise StepError(f"step {index + 1} must be an object")
    step = cast(Mapping[str, Any], raw)
    action = str(step.get("action") or "").strip().lower()
    if action == "open":
        url = normalize_url(_text(step, "url", action))
        return Step(action, ("open", url), (url,))
    if action == "snapshot":
        return Step(action, ("snapshot",))
    if action == "screenshot":
        return Step(action, ("screenshot", str(shots(index))))
    if action == "eval":
        code = _text(step, "code", action)
        return Step(action, ("eval", code), tuple(js_url_literals(code)))
    if action in ("click", "type"):
        ref = parse_ref(step.get("ref"))
        if ref is None:
            raise StepError(f"step '{action}' needs a ref from a snapshot, e.g. @e3")
        if action == "click":
            return Step(action, ("click", f"@{ref}"))
        text = step.get("text")
        if not isinstance(text, str):
            raise StepError("step 'type' needs 'text'")
        return Step(action, ("fill", f"@{ref}", text))
    if action == "press":
        key = _text(step, "key", action).strip()
        if not _KEY.match(key):
            raise StepError("step 'press' needs a key name such as Enter or Control+a")
        return Step(action, ("press", key))
    if action == "scroll":
        direction = str(step.get("direction") or "down").strip().lower()
        if direction not in _SCROLLS:
            raise StepError("step 'scroll' direction must be up, down, left or right")
        return Step(action, ("scroll", direction))
    if action == "back":
        return Step(action, ("back",))
    raise StepError(f"step {index + 1}: unknown action {action!r}")


class BrowserExec:
    """The ``browser_exec`` handler (see the module notes)."""

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        locate: Callable[[Scope, str], Path | None] = locate_tool,
        resolver: Resolver | None = None,
    ) -> None:
        self.runner: CommandRunner = runner if runner is not None else SubprocessRunner()
        self.locate = locate
        self.resolver = resolver

    def ready(self, scope: Scope) -> bool:
        return self.locate(scope, CLI_NAME) is not None

    @staticmethod
    def session_name(task_id: str | None, session: object) -> str | None:
        if session is None or session == "":
            base = re.sub(r"[^A-Za-z0-9_-]+", "_", task_id or "default")[:48] or "default"
            return f"indus-{base}"
        if not isinstance(session, str) or not _SESSION.match(session):
            return None
        return f"indus-{session}"

    async def _refusal(self, urls: Sequence[str], scope: Scope) -> str | None:
        for url in urls:
            verdict = await check_url_async(url, scope, resolver=self.resolver)
            if isinstance(verdict, Blocked):
                return f"Blocked: {verdict.reason}"
        return None

    async def __call__(
        self, args: Mapping[str, Any], *, task_id: str | None = None, scope: Scope | None = None
    ) -> str:
        resolved = scope if scope is not None else scope_or_none()
        if resolved is None:
            return fail("browser_exec needs a profile scope; none is active here.")
        raw_steps = args.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            return fail('steps must be a non-empty list, e.g. [{"action": "open", "url": …}]')
        steps_in = cast(list[object], raw_steps)
        if len(steps_in) > MAX_STEPS:
            return fail(f"at most {MAX_STEPS} steps per call; split the work")
        name = self.session_name(task_id, args.get("session"))
        if name is None:
            return fail("session must be 1-64 letters, digits, dashes or underscores (e.g. 'r7k2')")
        shots_dir = resolved.home / "cache" / "browser" / "exec"
        stamp = secrets.token_hex(3)

        def shot(index: int) -> Path:
            return shots_dir / f"{name}-{stamp}-{index + 1}.png"

        try:
            steps = [parse_step(raw, i, shot) for i, raw in enumerate(steps_in)]
        except StepError as exc:
            return fail(str(exc))
        refusal = await self._refusal([u for s in steps for u in s.urls], resolved)
        if refusal is not None:
            return fail(refusal)
        cli = self.locate(resolved, CLI_NAME)
        if cli is None:
            return fail(INSTALL_HINT)
        if any(s.action == "screenshot" for s in steps):
            shots_dir.mkdir(parents=True, exist_ok=True)
        env = browser_env(resolved)
        budget = float(clamp_timeout(args.get("timeout_s", DEFAULT_TIMEOUT_S)))
        clock = resolved.clock
        deadline = clock.monotonic() + budget
        results: list[dict[str, Any]] = []
        ok = True
        for index, step in enumerate(steps):
            left = deadline - clock.monotonic()
            if left <= 0:
                results.append({"action": step.action, "ok": False, "error": "out of time"})
                ok = False
                break
            argv = [str(cli), "--session", name, "--json", *step.argv]
            try:
                outcome = await self.runner.run(argv, env, left)
            except OSError as exc:
                return fail(f"The agent-browser CLI could not be started: {type(exc).__name__}")
            row = self._row(step, outcome, resolved, budget)
            if step.action == "screenshot" and row["ok"]:
                row["path"] = str(shot(index))
            results.append(row)
            if not row["ok"]:
                ok = False
                break
        skipped = len(steps) - len(results)
        body: dict[str, Any] = {"success": ok, "session": name, "steps": results}
        if skipped:
            body["skipped_steps"] = skipped
        return as_json(body)

    @staticmethod
    def _row(step: Step, outcome: RunOutcome, scope: Scope, budget: float) -> dict[str, Any]:
        if outcome.timed_out:
            return {
                "action": step.action,
                "ok": False,
                "error": f"the call ran out of its {budget:g} s; the CLI was stopped",
            }
        row: dict[str, Any] = {"action": step.action, "ok": outcome.returncode == 0}
        output = outcome.stdout.strip()
        with contextlib.suppress(ValueError):
            parsed = json.loads(output.splitlines()[-1]) if output else None
            if isinstance(parsed, dict):
                answer = cast(dict[str, Any], parsed)
                if answer.get("success") is False:
                    row["ok"] = False
                data = answer.get("data", answer.get("error"))
                output = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        if output:
            row["output" if row["ok"] else "error"] = _cap(output, scope)
        if outcome.stderr.strip() and not row["ok"]:
            row["stderr"] = _cap(outcome.stderr, scope)
        if not row["ok"] and "error" not in row:
            row["error"] = f"the CLI exited with code {outcome.returncode}"
        return row


ACTS = ToolTraits(mutating=True)


def register_exec_tool(registry: ToolRegistry, handler: BrowserExec | None = None) -> BrowserExec:
    """Register ``browser_exec`` (offered only when the CLI is found)."""
    made = handler if handler is not None else BrowserExec()
    registry.register(
        TOOL_NAME,
        TOOLSET,
        SCHEMA,
        made.__call__,
        probe=made.ready,
        is_async=True,
        max_result_chars=MAX_RESULT_CHARS,
        traits=ACTS,
    )
    return made
