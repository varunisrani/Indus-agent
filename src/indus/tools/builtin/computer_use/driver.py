"""The desktop driver contract and the built-in driver that runs an external program over stdio.

``DesktopDriver`` is what the ``computer_use`` tool talks to: one coroutine per tool action
(``capture``, ``click``, ``double_click``, ``drag``, ``scroll``, ``type_text``, ``key``,
``set_value``, ``wait``, ``list_apps``, ``focus_app``) plus ``start`` / ``close``. A driver
delivers input to the target window in the background: it never moves the person's pointer and
never brings a window to the front.

Drivers are offered by ``DriverProvider`` entries in a small registry: ``create`` builds an
unstarted driver for one (profile, session), ``ready`` answers whether it can run here (the tool's
probe and the ``indus doctor`` rows). ``computer_use.driver`` picks the provider; the built-in one
is ``stdio``. Plugins add theirs with ``register_driver`` (P13).

The ``stdio`` driver starts the program named by ``INDUS_CUA_DRIVER_CMD`` in the scope's env, else
``computer_use.driver_cmd`` (split like a shell would), with the environment from
``safety.child_env``. It writes one JSON object per line, ``{"id", "op", "args"}``, and reads one
answer line per request, ``{"id", "ok": true, "result": {...}}`` or ``{"id", "ok": false,
"error": "..."}``. The first request is ``hello``: its result says ``ready`` (and ``name``,
``version``, ``permissions``, ``reason``). The program must exit when its stdin closes. A program
that exits, closes its output, answers with something that is not JSON, or does not answer within
the action timeout is killed and reported as ``DriverCrashed`` (with a bounded tail of its stderr);
the caller starts a new one next time.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import os
import shlex
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from indus.foundation.clock import Clock, SystemClock
from indus.foundation.config.schema import default_for
from indus.foundation.scope import Scope
from indus.platform.locate import which
from indus.safety.child_env import child_env
from indus.tools.registry import scope_key

__all__ = [
    "CMD_ENV",
    "CMD_KEY",
    "DRIVER_KEY",
    "STDIO",
    "Capture",
    "CaptureMode",
    "DesktopDriver",
    "DoctorRow",
    "DriverCrashed",
    "DriverError",
    "DriverProvider",
    "Element",
    "Outcome",
    "Readiness",
    "StdioDriver",
    "Target",
    "doctor_rows",
    "driver_command",
    "provider_for",
    "providers",
    "register_driver",
    "setting",
    "stdio_readiness",
    "unregister_driver",
]

log = logging.getLogger(__name__)

CMD_ENV = "INDUS_CUA_DRIVER_CMD"
CMD_KEY = "computer_use.driver_cmd"
DRIVER_KEY = "computer_use.driver"
TIMEOUT_KEY = "computer_use.action_timeout_s"
STDIO = "stdio"

DEFAULT_ACTION_TIMEOUT_S = 30.0
HELLO_TIMEOUT_S = 10.0
PROBE_TIMEOUT_S = 5.0
READY_CACHE_S = 300.0
CLOSE_GRACE_S = 2.0
MAX_LINE_BYTES = 64 * 1024 * 1024  # a screenshot travels inside one answer line
STDERR_TAIL_LINES = 20
STDERR_TAIL_CHARS = 1_000
MAX_WAIT_MS = 30_000

CaptureMode = Literal["som", "vision", "ax"]
CAPTURE_MODES: tuple[str, ...] = ("som", "vision", "ax")


# -- errors ---------------------------------------------------------------------------------------


class DriverError(Exception):
    """The driver refused or failed one request; the driver itself is still usable."""


class DriverCrashed(DriverError):
    """The driver is gone (exited, hung, or spoke garbage); it must be started again."""


# -- shapes ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Element:
    """One control on screen. ``index`` is its mark number in a ``som`` capture; ``bounds`` is
    ``(x, y, width, height)`` in screenshot pixels, ``None`` when the driver does not know it."""

    index: int
    role: str = ""
    label: str = ""
    bounds: tuple[int, int, int, int] | None = None
    app: str = ""


@dataclass(frozen=True)
class Capture:
    mode: str
    width: int = 0
    height: int = 0
    image_b64: str | None = None
    mime: str = "image/png"
    app: str = ""
    window: str = ""
    elements: tuple[Element, ...] = ()
    tree: str = ""
    note: str = ""


@dataclass(frozen=True)
class Outcome:
    """What an action did. ``data`` holds any further fields the driver reported."""

    ok: bool
    action: str
    message: str = ""
    data: Mapping[str, Any] = field(default_factory=lambda: {})


@dataclass(frozen=True)
class Target:
    """Where an action lands: a mark number from the last capture, or a pixel position."""

    element: int | None = None
    x: int | None = None
    y: int | None = None

    def as_args(self, prefix: str = "") -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.element is not None:
            out[f"{prefix}element"] = self.element
        if self.x is not None and self.y is not None:
            out[f"{prefix}x"] = self.x
            out[f"{prefix}y"] = self.y
        return out


@dataclass(frozen=True)
class Readiness:
    """Whether a driver can run here, and what it said about itself."""

    ready: bool
    reason: str = ""
    configured: bool = True
    found: bool = True
    command: str = ""
    name: str = ""
    version: str = ""
    permissions: Mapping[str, bool] = field(default_factory=lambda: {})


class DesktopDriver(ABC):
    """The contract every desktop driver meets (see the module notes)."""

    name: str = "driver"
    clock: Clock = SystemClock()

    async def start(self) -> Readiness:
        """Get ready for the first request; raise ``DriverError`` when that is impossible."""
        return Readiness(True, name=self.name)

    async def close(self) -> None:
        return None

    @abstractmethod
    async def capture(self, mode: CaptureMode, *, app: str | None = None) -> Capture: ...

    @abstractmethod
    async def click(self, target: Target, *, button: str = "left") -> Outcome: ...

    @abstractmethod
    async def double_click(self, target: Target, *, button: str = "left") -> Outcome: ...

    @abstractmethod
    async def drag(self, start: Target, end: Target, *, button: str = "left") -> Outcome: ...

    @abstractmethod
    async def scroll(self, target: Target, *, direction: str, amount: int) -> Outcome: ...

    @abstractmethod
    async def type_text(self, text: str) -> Outcome: ...

    @abstractmethod
    async def key(self, combo: str) -> Outcome: ...

    @abstractmethod
    async def set_value(self, element: int, value: str) -> Outcome: ...

    async def wait(self, ms: int) -> Outcome:
        ms = max(0, min(int(ms), MAX_WAIT_MS))
        await self.clock.sleep(ms / 1000)
        return Outcome(True, "wait", f"waited {ms} ms")

    @abstractmethod
    async def list_apps(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def focus_app(self, name: str) -> Outcome: ...


# -- the provider registry ------------------------------------------------------------------------

DriverFactory = Callable[[Scope, "str | None"], DesktopDriver]
ReadyCheck = Callable[[Scope], Readiness]


@dataclass(frozen=True)
class DriverProvider:
    name: str
    create: DriverFactory
    ready: ReadyCheck
    doctor: Callable[[Scope], list[DoctorRow]] | None = None


_providers: dict[str, DriverProvider] = {}
_providers_lock = threading.Lock()


def register_driver(provider: DriverProvider, *, replace: bool = False) -> None:
    """Offer a driver under ``provider.name`` (the value ``computer_use.driver`` selects)."""
    with _providers_lock:
        if provider.name in _providers and not replace:
            raise ValueError(f"a desktop driver named {provider.name!r} is already registered")
        _providers[provider.name] = provider


def unregister_driver(name: str) -> bool:
    if name == STDIO:
        return False  # the built-in driver stays
    with _providers_lock:
        return _providers.pop(name, None) is not None


def providers() -> list[str]:
    with _providers_lock:
        return sorted(_providers)


def setting(scope: Scope | None, key: str) -> Any:
    value: Any = None
    if scope is not None and scope.config is not None:
        try:
            value = scope.config.get(key)
        except Exception:
            log.debug("config %s unreadable; using the default", key, exc_info=True)
    return default_for(key) if value is None else value


def selected_name(scope: Scope | None) -> str:
    return str(setting(scope, DRIVER_KEY) or STDIO).strip() or STDIO


def provider_for(scope: Scope | None) -> DriverProvider | None:
    with _providers_lock:
        return _providers.get(selected_name(scope))


def action_timeout(scope: Scope | None) -> float:
    raw = setting(scope, TIMEOUT_KEY)
    if isinstance(raw, int | float) and not isinstance(raw, bool) and raw > 0:
        return float(raw)
    return DEFAULT_ACTION_TIMEOUT_S


# -- the stdio driver: command, readiness ---------------------------------------------------------


@dataclass(frozen=True)
class Command:
    argv: tuple[str, ...]  # argv[0] resolved to a full path when found
    text: str  # as configured, for messages
    source: str  # "env" or "config"
    found: bool


def driver_command(scope: Scope) -> Command | None:
    """The configured driver program (env first, then config), or None when neither sets one."""
    text, source = (scope.env.get(CMD_ENV) or "").strip(), "env"
    if not text:
        configured = setting(scope, CMD_KEY)
        text, source = (configured.strip() if isinstance(configured, str) else ""), "config"
    if not text:
        return None
    try:
        argv = shlex.split(text, posix=os.name != "nt")
    except ValueError:
        return Command((), text, source, False)
    if not argv:
        return None
    program = argv[0]
    if os.sep in program or (os.altsep is not None and os.altsep in program):
        path = Path(program).expanduser()
        found = path.is_file() and os.access(path, os.X_OK)
        resolved = str(path)
    else:
        located = which(program, path=scope.env.get("PATH"))
        found, resolved = located is not None, located or program
    return Command((resolved, *argv[1:]), text, source, found)


def _hello_line() -> bytes:
    return (json.dumps({"id": 0, "op": "hello", "args": {}}) + "\n").encode()


def _readiness_from(answer: Mapping[str, Any], command: Command) -> Readiness:
    if answer.get("ok") is not True:
        reason = str(answer.get("error") or "the driver refused the hello request")
        return Readiness(False, reason, command=command.text)
    result_raw = answer.get("result")
    result = cast(Mapping[str, Any], result_raw) if isinstance(result_raw, Mapping) else {}
    perms_raw = result.get("permissions")
    perms: dict[str, bool] = {}
    if isinstance(perms_raw, Mapping):
        for k, v in cast(Mapping[str, Any], perms_raw).items():
            if isinstance(v, bool):
                perms[str(k)] = v
    ready = result.get("ready") is True
    reason = str(result.get("reason") or ("" if ready else "the driver says it is not ready"))
    return Readiness(
        ready,
        reason,
        command=command.text,
        name=str(result.get("name") or ""),
        version=str(result.get("version") or ""),
        permissions=perms,
    )


def _first_answer(stdout: bytes) -> Mapping[str, Any] | None:
    for raw in stdout.decode("utf-8", "replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            return None
        return cast(Mapping[str, Any], parsed) if isinstance(parsed, Mapping) else None
    return None


_ready_cache: dict[tuple[str, tuple[str, ...]], tuple[float, Readiness]] = {}
_ready_lock = threading.Lock()


def clear_readiness_cache() -> None:
    with _ready_lock:
        _ready_cache.clear()


def stdio_readiness(scope: Scope, *, cached: bool = True) -> Readiness:
    """Run the driver once with only a ``hello`` on its stdin and read the answer.

    A "ready" verdict is remembered per (profile, command) for ``READY_CACHE_S``, so building
    tool arrays does not start the program every turn; a "not ready" one is not remembered.
    """
    command = driver_command(scope)
    if command is None:
        return Readiness(False, "no driver command is configured", configured=False, found=False)
    if not command.found:
        return Readiness(
            False, f"the program {command.text!r} was not found", found=False, command=command.text
        )
    key = (scope_key(scope), command.argv)
    now = time.monotonic()
    if cached:
        with _ready_lock:
            hit = _ready_cache.get(key)
        if hit is not None and now - hit[0] < READY_CACHE_S:
            return hit[1]
    try:
        done = subprocess.run(
            list(command.argv),
            input=_hello_line(),
            capture_output=True,
            timeout=PROBE_TIMEOUT_S,
            env=child_env(scope),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return Readiness(
            False, f"no answer to hello within {PROBE_TIMEOUT_S:g} s", command=command.text
        )
    except OSError as exc:
        return Readiness(False, f"could not start: {exc}", command=command.text)
    answer = _first_answer(done.stdout)
    if answer is None:
        tail = _tail_text(done.stderr.decode("utf-8", "replace").splitlines())
        why = f"no answer to hello (exit code {done.returncode})"
        return Readiness(False, why + (f": {tail}" if tail else ""), command=command.text)
    verdict = _readiness_from(answer, command)
    if verdict.ready:
        with _ready_lock:
            _ready_cache[key] = (now, verdict)
    return verdict


def _tail_text(lines: Sequence[str]) -> str:
    text = " | ".join(line.strip() for line in lines[-STDERR_TAIL_LINES:] if line.strip())
    if len(text) > STDERR_TAIL_CHARS:
        text = "…" + text[-STDERR_TAIL_CHARS:]
    return text


# -- the stdio driver: the running program --------------------------------------------------------


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return default


def _bounds(value: Any) -> tuple[int, int, int, int] | None:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return None
    items = list(cast(Sequence[Any], value))
    if len(items) != 4:
        return None
    numbers = tuple(_int(v) for v in items)
    if numbers[2] <= 0 and numbers[3] <= 0:
        return None  # a zero rect means "unknown", never "top-left corner"
    return cast(tuple[int, int, int, int], numbers)


def parse_capture(result: Mapping[str, Any], mode: str) -> Capture:
    elements: list[Element] = []
    raw_elements = result.get("elements")
    if isinstance(raw_elements, list):
        for position, raw in enumerate(cast(list[Any], raw_elements), start=1):
            if not isinstance(raw, Mapping):
                continue
            item = cast(Mapping[str, Any], raw)
            elements.append(
                Element(
                    index=_int(item.get("index"), position),
                    role=str(item.get("role") or ""),
                    label=str(item.get("label") or ""),
                    bounds=_bounds(item.get("bounds")),
                    app=str(item.get("app") or ""),
                )
            )
    image = result.get("image")
    return Capture(
        mode=str(result.get("mode") or mode),
        width=_int(result.get("width")),
        height=_int(result.get("height")),
        image_b64=image if isinstance(image, str) and image else None,
        mime=str(result.get("mime") or "image/png"),
        app=str(result.get("app") or ""),
        window=str(result.get("window") or ""),
        elements=tuple(elements),
        tree=str(result.get("tree") or ""),
        note=str(result.get("note") or ""),
    )


def parse_outcome(action: str, result: Mapping[str, Any]) -> Outcome:
    ok = result.get("ok")
    message = str(result.get("message") or "")
    rest = {k: v for k, v in result.items() if k not in ("ok", "message", "action")}
    return Outcome(ok is not False, action, message, rest)


class StdioDriver(DesktopDriver):
    """An external driver program spoken to over stdin/stdout (see the module notes)."""

    name = STDIO

    def __init__(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        *,
        timeout_s: float = DEFAULT_ACTION_TIMEOUT_S,
        label: str = "",
    ) -> None:
        self.argv = tuple(argv)
        self.env = dict(env)
        self.timeout_s = timeout_s
        self.label = label or (self.argv[0] if self.argv else "driver")
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr: collections.deque[str] = collections.deque(maxlen=STDERR_TAIL_LINES)
        self._drain: asyncio.Task[None] | None = None
        self._ids = 0
        self._lock = asyncio.Lock()
        self.info = Readiness(False, "not started")

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self) -> Readiness:
        if self.running:
            return self.info
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self.argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.env,
                limit=MAX_LINE_BYTES,
                start_new_session=sys.platform != "win32",
            )
        except OSError as exc:
            raise DriverCrashed(f"could not start {self.label}: {exc}") from exc
        self._stderr.clear()
        self._drain = asyncio.ensure_future(self._read_stderr(self._proc))
        answer = await self._exchange("hello", {}, HELLO_TIMEOUT_S, raw=True)
        verdict = _readiness_from(answer, Command(self.argv, self.label, "", True))
        if not verdict.ready:
            await self.close()
            raise DriverError(f"the desktop driver is not ready: {verdict.reason}")
        self.info = verdict
        return verdict

    async def _read_stderr(self, proc: asyncio.subprocess.Process) -> None:
        stream = proc.stderr
        if stream is None:
            return
        with contextlib.suppress(Exception):
            while line := await stream.readline():
                self._stderr.append(line.decode("utf-8", "replace").rstrip())

    def _stderr_tail(self) -> str:
        return _tail_text(list(self._stderr))

    async def _crash(self, why: str) -> DriverCrashed:
        proc = self._proc
        await self.close()
        code = proc.returncode if proc is not None else None
        detail = why + (f" (exit code {code})" if code is not None else "")
        tail = self._stderr_tail()
        return DriverCrashed(detail + (f"; its last output: {tail}" if tail else ""))

    async def _exchange(
        self, op: str, args: Mapping[str, Any], timeout_s: float, *, raw: bool = False
    ) -> Mapping[str, Any]:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.stdout is None or proc.returncode is not None:
            raise await self._crash("the desktop driver is not running")
        self._ids += 1
        request_id = self._ids
        line = json.dumps({"id": request_id, "op": op, "args": dict(args)}) + "\n"
        try:
            proc.stdin.write(line.encode())
            await asyncio.wait_for(proc.stdin.drain(), timeout_s)
        except (OSError, TimeoutError) as exc:
            raise await self._crash(f"the desktop driver stopped taking requests ({exc})") from exc
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            left = deadline - asyncio.get_running_loop().time()
            try:
                if left <= 0:
                    raise TimeoutError
                data = await asyncio.wait_for(proc.stdout.readline(), left)
            except TimeoutError as exc:
                raise await self._crash(
                    f"the desktop driver did not answer {op} within {timeout_s:g} s"
                ) from exc
            except (OSError, ValueError, asyncio.LimitOverrunError) as exc:
                raise await self._crash(
                    f"the desktop driver's answer was unreadable ({exc})"
                ) from exc
            if not data:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(proc.wait(), CLOSE_GRACE_S)
                raise await self._crash(f"the desktop driver exited during {op}")
            text = data.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                answer = json.loads(text)
            except ValueError:
                raise await self._crash(
                    f"the desktop driver answered {op} with something that is not JSON"
                ) from None
            if not isinstance(answer, Mapping):
                raise await self._crash(f"the desktop driver answered {op} with a non-object")
            body = cast(Mapping[str, Any], answer)
            if body.get("id") != request_id:
                continue  # an answer to an earlier request; ours is still coming
            if raw:
                return body
            if body.get("ok") is not True:
                raise DriverError(str(body.get("error") or f"the desktop driver refused {op}"))
            result = body.get("result")
            return cast(Mapping[str, Any], result) if isinstance(result, Mapping) else {}

    async def request(
        self, op: str, args: Mapping[str, Any], *, extra_s: float = 0.0
    ) -> Mapping[str, Any]:
        """Send one request and return its result; one request at a time per driver."""
        async with self._lock:
            if not self.running:
                await self.start()
            return await self._exchange(op, args, self.timeout_s + extra_s)

    async def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            if proc.stdin is not None:
                with contextlib.suppress(Exception):
                    proc.stdin.close()
            if proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), CLOSE_GRACE_S)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(proc.wait(), CLOSE_GRACE_S)
        drain, self._drain = self._drain, None
        if drain is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(drain, CLOSE_GRACE_S)

    def kill_now(self) -> None:
        """Kill without awaiting (the loop that started the program is gone)."""
        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()

    # -- the actions ------------------------------------------------------------------------------

    async def capture(self, mode: CaptureMode, *, app: str | None = None) -> Capture:
        args: dict[str, Any] = {"mode": mode}
        if app:
            args["app"] = app
        return parse_capture(await self.request("capture", args), mode)

    async def _act(self, op: str, args: Mapping[str, Any], *, extra_s: float = 0.0) -> Outcome:
        return parse_outcome(op, await self.request(op, args, extra_s=extra_s))

    async def click(self, target: Target, *, button: str = "left") -> Outcome:
        return await self._act("click", {**target.as_args(), "button": button})

    async def double_click(self, target: Target, *, button: str = "left") -> Outcome:
        return await self._act("double_click", {**target.as_args(), "button": button})

    async def drag(self, start: Target, end: Target, *, button: str = "left") -> Outcome:
        args = {**start.as_args("from_"), **end.as_args("to_"), "button": button}
        return await self._act("drag", args)

    async def scroll(self, target: Target, *, direction: str, amount: int) -> Outcome:
        args = {**target.as_args(), "direction": direction, "amount": amount}
        return await self._act("scroll", args)

    async def type_text(self, text: str) -> Outcome:
        return await self._act("type", {"text": text})

    async def key(self, combo: str) -> Outcome:
        return await self._act("key", {"keys": combo})

    async def set_value(self, element: int, value: str) -> Outcome:
        return await self._act("set_value", {"element": element, "value": value})

    async def wait(self, ms: int) -> Outcome:
        ms = max(0, min(int(ms), MAX_WAIT_MS))
        return await self._act("wait", {"ms": ms}, extra_s=ms / 1000)

    async def list_apps(self) -> list[dict[str, Any]]:
        result = await self.request("list_apps", {})
        apps = result.get("apps")
        if not isinstance(apps, list):
            return []
        return [
            dict(cast(Mapping[str, Any], a))
            for a in cast(list[Any], apps)
            if isinstance(a, Mapping)
        ]

    async def focus_app(self, name: str) -> Outcome:
        return await self._act("focus_app", {"name": name})


def _create_stdio(scope: Scope, session_id: str | None) -> DesktopDriver:
    command = driver_command(scope)
    if command is None or not command.found:
        raise DriverError(stdio_readiness(scope, cached=False).reason or "no desktop driver")
    env = child_env(scope, session_id=session_id)
    return StdioDriver(command.argv, env, timeout_s=action_timeout(scope), label=command.text)


# -- doctor rows ----------------------------------------------------------------------------------

Severity = Literal["ok", "info", "warn", "error"]


@dataclass(frozen=True)
class DoctorRow:
    id: str
    severity: Severity
    message: str
    fix: str | None = None


_PERMISSIONS: tuple[tuple[str, str, str], ...] = (
    ("accessibility", "Accessibility", "to read and operate controls"),
    ("screen_recording", "Screen Recording", "to take screenshots"),
)
_SETTINGS_PATH = "System Settings > Privacy & Security > {label}"


def _permission_rows(verdict: Readiness, platform: str) -> list[DoctorRow]:
    rows: list[DoctorRow] = []
    program = verdict.command or "the driver program"
    for key, label, why in _PERMISSIONS:
        granted = verdict.permissions.get(key)
        row_id = f"computer_use.{key}"
        where = _SETTINGS_PATH.format(label=label)
        if granted is True:
            rows.append(DoctorRow(row_id, "ok", f"{label} permission granted"))
        elif granted is False:
            rows.append(
                DoctorRow(
                    row_id,
                    "warn",
                    f"the desktop driver lacks the {label} permission ({why})",
                    fix=f"turn on {program} in {where}, then start Indus again",
                )
            )
        elif platform == "darwin":
            rows.append(
                DoctorRow(
                    row_id,
                    "info",
                    f"macOS asks for the {label} permission {why}; the driver did not say "
                    "whether it has it",
                    fix=f"check {program} in {where}",
                )
            )
    return rows


def _stdio_doctor(scope: Scope, platform: str) -> list[DoctorRow]:
    verdict = stdio_readiness(scope, cached=False)
    if not verdict.configured:
        return [
            DoctorRow(
                "computer_use",
                "info",
                "desktop control (computer_use) is off: no driver command is set",
                fix=f"set {CMD_KEY} (or {CMD_ENV}) to a desktop driver program that speaks the "
                "Indus stdio protocol",
            )
        ]
    if not verdict.found:
        return [
            DoctorRow(
                "computer_use",
                "warn",
                f"desktop control (computer_use) is off: {verdict.reason}",
                fix=f"install the driver, or give its full path in {CMD_KEY}",
            )
        ]
    if not verdict.ready:
        rows = [
            DoctorRow(
                "computer_use",
                "warn",
                f"desktop control (computer_use) is off: the driver {verdict.command!r} is not "
                f"ready ({verdict.reason})",
                fix="run the driver program by hand to see what it needs",
            )
        ]
        return rows + _permission_rows(verdict, platform)
    who = " ".join(p for p in (verdict.name, verdict.version) if p) or verdict.command
    rows = [DoctorRow("computer_use", "ok", f"desktop driver ready: {who}")]
    return rows + _permission_rows(verdict, platform)


def doctor_rows(scope: Scope, *, platform: str | None = None) -> list[DoctorRow]:
    """The ``indus doctor`` rows for desktop control; never raises."""
    platform = platform if platform is not None else sys.platform
    provider = provider_for(scope)
    if provider is None:
        name = selected_name(scope)
        return [
            DoctorRow(
                "computer_use",
                "warn",
                f"{DRIVER_KEY} is {name!r}, but no desktop driver of that name is installed "
                f"(known: {', '.join(providers())})",
                fix=f"set {DRIVER_KEY} to {STDIO}, or install the plugin that provides {name!r}",
            )
        ]
    try:
        if provider.name == STDIO:
            return _stdio_doctor(scope, platform)
        if provider.doctor is not None:
            return provider.doctor(scope)
        verdict = provider.ready(scope)
    except Exception as exc:
        return [DoctorRow("computer_use", "warn", f"the desktop driver check failed: {exc}")]
    if verdict.ready:
        return [DoctorRow("computer_use", "ok", f"desktop driver {provider.name!r} ready")]
    return [
        DoctorRow(
            "computer_use",
            "warn",
            f"desktop control (computer_use) is off: driver {provider.name!r} is not ready "
            f"({verdict.reason})",
        )
    ]


register_driver(DriverProvider(STDIO, _create_stdio, stdio_readiness))
