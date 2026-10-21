# pyright: strict
"""Running a recipe (P24-17): ``bootstrap`` → ``build`` → ``test``, then optionally boot and poll.

Every command runs through an ``ExecHost`` (the session's terminal backend: its environment
builder, its sandbox) in the workspace folder, with ``PHASE_TIMEOUT_S`` (600 s) each, and through
an optional approval ``gate`` first: a refused command is a failed phase whose text says why, and
nothing ran. The first failure stops the run (unless ``stop_on_failure`` is off). Each result keeps
the last ``TAIL_CHARS`` (2,000) characters of the command's output.

After the phases pass, a recipe's ``start`` command may be booted (only when a ``ServiceStarter``
is given, as the CLI does) and ``http://127.0.0.1:<port><ready_path>`` polled every second for up
to ``READY_TIMEOUT_S`` (60 s); any HTTP answer counts as ready. The service is stopped either way.

A compose recipe refuses to build or start while that compose project has running containers
(``docker compose ps``): rebuilding would replace them and lose what lives inside. When docker
itself is missing the run goes on (the build then fails on its own); when the probe fails or
hangs the run is refused, since running containers cannot be ruled out.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from indus.foundation.clock import Clock, SystemClock
from indus.tools.builtin.exec_host import ExecHost
from indus.tools.verify.recipes import Recipe

__all__ = [
    "PHASES",
    "PHASE_TIMEOUT_S",
    "READY_TIMEOUT_S",
    "TAIL_CHARS",
    "CommandGate",
    "LocalStarter",
    "PhaseResult",
    "Readiness",
    "ServiceStarter",
    "VerifyResult",
    "http_probe",
    "run_verify",
    "tail",
]

log = logging.getLogger(__name__)

PHASES = ("bootstrap", "build", "test")
PHASE_TIMEOUT_S = 600.0
READY_TIMEOUT_S = 60.0
READY_POLL_S = 1.0
PROBE_TIMEOUT_S = 5.0
TAIL_CHARS = 2000
COMPOSE_PROBE_S = 15.0
STOP_GRACE_S = 10.0
MISSING_RC = 127

type CommandGate = Callable[[str, str], Awaitable[str | None]]
"""``(command, cwd)`` → why it may not run, or None to let it run."""
type Probe = Callable[[str], Awaitable[int | None]]
"""``url`` → the HTTP status it answered with, or None when nothing answered."""


def tail(text: str, limit: int = TAIL_CHARS) -> str:
    return text[-limit:] if len(text) > limit else text


@dataclass(slots=True)
class PhaseResult:
    phase: str
    command: str
    exit_code: int | None
    duration: float
    tail: str
    timed_out: bool = False
    refused: str | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and self.refused is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "command": self.command,
            "exit_code": self.exit_code,
            "duration": round(self.duration, 3),
            "ok": self.ok,
            "timed_out": self.timed_out,
            "refused": self.refused,
            "tail": self.tail,
        }


@dataclass(slots=True)
class Readiness:
    url: str
    ready: bool
    status: int | None
    duration: float
    error: str | None = None
    tail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "ready": self.ready,
            "status": self.status,
            "duration": round(self.duration, 3),
            "error": self.error,
            "tail": self.tail,
        }


@dataclass(slots=True)
class VerifyResult:
    recipe: str
    phases: list[PhaseResult] = field(default_factory=lambda: list[PhaseResult]())
    readiness: Readiness | None = None
    refused: str | None = None

    @property
    def ok(self) -> bool:
        return (
            self.refused is None
            and all(p.ok for p in self.phases)
            and (self.readiness is None or self.readiness.ready)
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "recipe": self.recipe,
            "ok": self.ok,
            "refused": self.refused,
            "phases": [p.as_dict() for p in self.phases],
            "readiness": self.readiness.as_dict() if self.readiness else None,
        }


class ServiceStarter(Protocol):
    """Boots a recipe's start command in the background and stops it again."""

    async def start(self, command: str, cwd: str) -> object: ...

    async def stop(self, handle: object) -> str:
        """Stop it; the output it produced."""
        ...


# -- phases ---------------------------------------------------------------------------------------


async def _run_command(
    host: ExecHost,
    phase: str,
    command: str,
    root: str,
    *,
    timeout: float,
    clock: Clock,
    gate: CommandGate | None,
    cancel: object,
) -> PhaseResult:
    if gate is not None:
        refusal = await gate(command, root)
        if refusal:
            return PhaseResult(phase, command, None, 0.0, "", refused=refusal)
    started = clock.monotonic()
    try:
        result = await host.execute(
            command, timeout=timeout, cwd=root, bounded=False, cancel=cancel
        )
    except Exception as exc:  # the backend could not run it at all
        return PhaseResult(
            phase,
            command,
            None,
            clock.monotonic() - started,
            f"{type(exc).__name__}: {exc}",
            refused=f"the terminal backend could not run it ({type(exc).__name__})",
        )
    duration = clock.monotonic() - started
    if result.timed_out:
        return PhaseResult(phase, command, None, duration, tail(result.output), timed_out=True)
    return PhaseResult(phase, command, result.returncode, duration, tail(result.output))


async def _compose_refusal(host: ExecHost, root: str) -> str | None:
    probe = "docker compose ps --status running --format '{{.Name}}'"
    try:
        result = await host.execute(probe, timeout=COMPOSE_PROBE_S, cwd=root, bounded=False)
    except Exception as exc:
        return f"the docker compose probe could not run ({type(exc).__name__})"
    if result.timed_out:
        return f"docker compose ps did not answer within {COMPOSE_PROBE_S:g} s"
    if result.returncode == MISSING_RC:
        return None
    if result.returncode != 0:
        last = [line for line in result.output.splitlines() if line.strip()]
        return f"docker compose ps failed (exit {result.returncode}): {last[-1] if last else ''}"
    names = [line.strip() for line in result.output.splitlines() if line.strip()]
    if names:
        return "this compose project already has running containers: " + ", ".join(names)
    return None


async def run_verify(
    host: ExecHost,
    root: str,
    recipe: Recipe,
    *,
    phases: Sequence[str] | None = None,
    phase_timeout: float = PHASE_TIMEOUT_S,
    ready_timeout: float = READY_TIMEOUT_S,
    clock: Clock | None = None,
    gate: CommandGate | None = None,
    starter: ServiceStarter | None = None,
    probe: Probe | None = None,
    port: int | None = None,
    stop_on_failure: bool = True,
    cancel: object = None,
) -> VerifyResult:
    """Run ``phases`` (default: bootstrap, build, test and start) of ``recipe`` in ``root``."""
    clock = clock or SystemClock()
    selected = tuple(phases) if phases else (*PHASES, "start")
    result = VerifyResult(recipe.name)
    boots = "start" in selected and recipe.start is not None and starter is not None
    if recipe.kind == "compose" and ("build" in selected or boots):
        refusal = await _compose_refusal(host, root)
        if refusal:
            result.refused = (
                f"{refusal}. Rebuilding would replace those containers and lose what lives "
                "inside them; run `docker compose build` / `up` yourself if that is intended."
            )
            return result
    for phase in PHASES:
        if phase not in selected:
            continue
        for command in recipe.commands(phase):
            outcome = await _run_command(
                host,
                phase,
                command,
                root,
                timeout=phase_timeout,
                clock=clock,
                gate=gate,
                cancel=cancel,
            )
            result.phases.append(outcome)
            if not outcome.ok and stop_on_failure:
                return result
    if boots and all(p.ok for p in result.phases):
        assert recipe.start is not None and starter is not None
        if gate is not None:
            refusal = await gate(recipe.start, root)
            if refusal:
                result.phases.append(
                    PhaseResult("start", recipe.start, None, 0.0, "", refused=refusal)
                )
                return result
        result.readiness = await _boot(
            starter, recipe, root, port or recipe.port or 8000, ready_timeout, clock, probe
        )
    return result


async def _boot(
    starter: ServiceStarter,
    recipe: Recipe,
    root: str,
    port: int,
    ready_timeout: float,
    clock: Clock,
    probe: Probe | None,
) -> Readiness:
    assert recipe.start is not None
    url = f"http://127.0.0.1:{port}{recipe.ready_path}"
    ask = probe or http_probe
    started = clock.monotonic()
    handle = await starter.start(recipe.start, root)
    ready, status, error = False, None, "no answer"
    output = ""
    try:
        deadline = started + ready_timeout
        while True:
            try:
                status = await ask(url)
            except Exception as exc:
                status, error = None, f"{type(exc).__name__}: {exc}"
            if status is not None:
                ready, error = True, None
                break
            if clock.monotonic() + READY_POLL_S > deadline:
                break
            await clock.sleep(READY_POLL_S)
    finally:
        output = await starter.stop(handle)
    return Readiness(url, ready, status, clock.monotonic() - started, error, tail(output))


# -- local boot and probe (the CLI) ---------------------------------------------------------------


async def http_probe(url: str) -> int | None:
    """The status line of a plain GET to a loopback URL, or None when nothing answered."""
    rest = url.removeprefix("http://")
    hostport, _, path = rest.partition("/")
    host, _, port_text = hostport.partition(":")
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError("readiness is only probed on the loopback interface")
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, int(port_text or "80")), PROBE_TIMEOUT_S
        )
    except (OSError, TimeoutError):
        return None
    try:
        request = f"GET /{path} HTTP/1.0\r\nHost: {hostport}\r\nConnection: close\r\n\r\n"
        writer.write(request.encode("ascii", errors="replace"))
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), PROBE_TIMEOUT_S)
    except (OSError, TimeoutError):
        return None
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
    parts = line.decode("latin-1").split()
    if len(parts) >= 2 and parts[0].startswith("HTTP/") and parts[1].isdigit():
        return int(parts[1])
    return None


class LocalStarter:
    """Boots a start command with bash on this machine in its own process group; stopping sends
    SIGTERM to the group, then SIGKILL after ``STOP_GRACE_S``."""

    def __init__(self, env: Mapping[str, str], *, clock: Clock | None = None) -> None:
        self.env = dict(env)
        self.clock: Clock = clock or SystemClock()

    async def start(self, command: str, cwd: str) -> object:
        return await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            command,
            cwd=cwd,
            env=self.env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )

    async def stop(self, handle: object) -> str:
        if not isinstance(handle, asyncio.subprocess.Process):
            return ""
        proc = handle
        reading = asyncio.ensure_future(proc.stdout.read()) if proc.stdout is not None else None
        for sig in (signal.SIGTERM, signal.SIGKILL):
            if proc.returncode is not None:
                break
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, sig)
            waiter = asyncio.ensure_future(proc.wait())
            timer = asyncio.ensure_future(self.clock.sleep(STOP_GRACE_S))
            await asyncio.wait({waiter, timer}, return_when=asyncio.FIRST_COMPLETED)
            timer.cancel()
            if not waiter.done():
                waiter.cancel()
        if reading is None:
            return ""
        try:
            data = await asyncio.wait_for(reading, 2.0)
        except TimeoutError:
            return ""
        return data.decode("utf-8", errors="replace")
