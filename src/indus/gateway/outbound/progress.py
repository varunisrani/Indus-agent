"""Tool progress, typing and the "still working" heartbeat in a chat (P15-16).

- ``ToolProgress`` turns ``tool.started`` events into short lines. Modes: ``off``; ``new`` (only
  when the tool changes); ``all``; ``verbose`` (plus the P05-redacted arguments, never the raw
  ones); ``log`` (no chat lines: a redacted, rotating audit file, 5 MB × 3). Grouping
  ``accumulate`` keeps one bubble per turn and edits it (a new bubble when it outgrows the
  platform limit); ``separate`` sends one message per line, as does any platform without edits.
  Repeats of the same line become ``(×N)``. Edits are spaced at least 1.5 s apart.
- ``TypingLoop`` refreshes the typing indicator every 2 s while a turn runs and pauses while an
  approval waits.
- ``Heartbeat`` posts "Still working… N min" every 180 s on long turns, edited in place.

Everything here is interim (never a turn's final) and leaves through ``ports``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import threading
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from indus.foundation.clock import Clock
from indus.foundation.home import secure_dir
from indus.foundation.scope import Scope
from indus.gateway.adapters.base import Adapter
from indus.gateway.outbound import ports
from indus.safety.redaction import redact_arguments, redact_output

__all__ = [
    "AUDIT_BACKUPS",
    "AUDIT_MAX_BYTES",
    "AuditLog",
    "Grouping",
    "Heartbeat",
    "ProgressDefaults",
    "ProgressMode",
    "ToolProgress",
    "TypingLoop",
    "progress_defaults",
    "progress_line",
]

type ProgressMode = Literal["off", "new", "all", "verbose", "log"]
type Grouping = Literal["accumulate", "separate"]

AUDIT_MAX_BYTES = 5 * 1024 * 1024
AUDIT_BACKUPS = 3
_VERBOSE_ARGS_CHARS = 400
_EPSILON = 1e-6


@dataclass(frozen=True)
class ProgressDefaults:
    mode: ProgressMode = "all"
    heartbeat: bool = True


_PLATFORM_DEFAULTS = {"telegram": ProgressDefaults(mode="off", heartbeat=True)}


def progress_defaults(platform: str) -> ProgressDefaults:
    """Per-platform defaults: phones (Telegram) get no tool lines, only the heartbeat."""
    return _PLATFORM_DEFAULTS.get(platform, ProgressDefaults())


_VERBS = {
    "terminal": "Running a command",
    "process": "Checking a background process",
    "read_file": "Reading a file",
    "write_file": "Writing a file",
    "patch": "Editing a file",
    "search_files": "Searching files",
    "web_search": "Searching the web",
    "web_extract": "Reading a web page",
    "execute_code": "Running code",
    "delegate_task": "Handing work to a helper",
    "memory": "Updating memory",
    "todo": "Updating the plan",
    "vision_analyze": "Looking at an image",
    "image_generate": "Making an image",
    "text_to_speech": "Making audio",
    "skill_view": "Opening a skill",
}


def progress_line(
    name: str,
    arguments: Mapping[str, Any] | None = None,
    *,
    verbose: bool = False,
    scope: Scope | None = None,
) -> str:
    """The chat line for a tool call. Built from the tool name; in verbose mode the arguments
    are added after P05 masking (raw arguments are never interpolated)."""
    label = _VERBS.get(name, name.replace("_", " "))
    line = f"⚙️ {label}"
    if verbose and arguments:
        masked = redact_arguments(dict(arguments), scope=scope)
        shown = json.dumps(masked, ensure_ascii=False, default=str, sort_keys=True)
        if len(shown) > _VERBOSE_ARGS_CHARS:
            shown = shown[: _VERBOSE_ARGS_CHARS - 1] + "…"
        line += f" ({name})\n```\n{shown}\n```"
    return line


# -- the audit log ---------------------------------------------------------------------------------


class AuditLog:
    """Append-only, redacted tool log with size rotation (``log`` mode)."""

    def __init__(
        self,
        path: Path,
        *,
        scope: Scope | None = None,
        max_bytes: int = AUDIT_MAX_BYTES,
        backups: int = AUDIT_BACKUPS,
    ) -> None:
        self.path = path
        self._scope = scope
        self._max = max_bytes
        self._backups = backups
        self._lock = threading.Lock()

    @classmethod
    def for_scope(cls, scope: Scope) -> AuditLog:
        return cls(scope.home / "logs" / "gateway-tools.log", scope=scope)

    def _rotate(self) -> None:
        for index in range(self._backups - 1, 0, -1):
            older = self.path.with_name(f"{self.path.name}.{index}")
            if older.exists():
                older.replace(self.path.with_name(f"{self.path.name}.{index + 1}"))
        self.path.replace(self.path.with_name(f"{self.path.name}.1"))

    def write(self, line: str, *, when: datetime | None = None) -> None:
        stamp = (when or datetime.now().astimezone()).strftime("%Y-%m-%d %H:%M:%S")
        masked = redact_output(" ".join(line.split()), force=True, scope=self._scope)
        data = f"{stamp}  {masked}\n".encode()
        with self._lock:
            env = self._scope.env if self._scope is not None else None
            secure_dir(self.path.parent, env=env)
            try:
                size = self.path.stat().st_size
            except OSError:
                size = 0
            if size and size + len(data) > self._max:
                self._rotate()
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "ab") as handle:
                handle.write(data)


# -- shared waiting --------------------------------------------------------------------------------


async def _sleep_or_event(clock: Clock, delay: float | None, event: asyncio.Event) -> None:
    """Wait until ``event`` is set or ``delay`` seconds pass (None: only the event)."""
    if delay is None:
        await event.wait()
        return
    waiter = asyncio.ensure_future(event.wait())
    sleeper = asyncio.ensure_future(clock.sleep(max(0.0, delay)))
    try:
        await asyncio.wait({waiter, sleeper}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (waiter, sleeper):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


# -- tool progress ---------------------------------------------------------------------------------


class ToolProgress:
    def __init__(
        self,
        port: Adapter,
        chat_id: str,
        *,
        clock: Clock,
        mode: ProgressMode = "all",
        grouping: Grouping = "accumulate",
        scope: Scope | None = None,
        audit: AuditLog | None = None,
        meta: Mapping[str, Any] | None = None,
        min_edit_interval: float = 1.5,
    ) -> None:
        self._port = port
        self._chat_id = chat_id
        self._clock = clock
        self.mode: ProgressMode = mode
        self._separate = grouping == "separate" or not ports.can_edit(port)
        self._scope = scope
        self._audit = audit
        self._meta = dict(meta or {})
        self._min_edit = min_edit_interval
        self._fmt = ports.formatter(port)
        self._len = ports.length_fn(port)
        self._limit = ports.max_length(port)
        self._inbox: deque[tuple[str, Mapping[str, Any]] | None] = deque()
        self._wake = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: int | None = None
        self._last_name: str | None = None
        self._last_line: str | None = None
        self._repeats = 0
        self._lines: list[str] = []
        self._unsent: list[str] = []
        self._bubble: str | None = None
        self._shown = ""
        self._last_edit = -math.inf
        self.message_ids: list[str] = []

    # -- input (any thread) ----------------------------------------------------------------------

    def _put(self, item: tuple[str, Mapping[str, Any]] | None) -> None:
        self._inbox.append(item)
        loop = self._loop
        if loop is None or threading.get_ident() == self._loop_thread:
            self._wake.set()
        else:
            loop.call_soon_threadsafe(self._wake.set)

    def tool_started(self, name: str, arguments: Mapping[str, Any] | None = None) -> None:
        if self.mode != "off" and name != "clarify":  # clarify renders its own prompt
            self._put((name, dict(arguments or {})))

    def feed(self, event: object) -> None:
        if getattr(event, "type", None) == "tool.started":
            arguments = getattr(event, "arguments", None)
            self.tool_started(str(getattr(event, "name", "tool")), arguments)

    def close(self) -> None:
        """No more tools this turn: flush and let ``run`` return."""
        self._put(None)

    # -- the task --------------------------------------------------------------------------------

    async def run(self) -> list[str]:
        self._loop = asyncio.get_running_loop()
        self._loop_thread = threading.get_ident()
        closing = False
        while True:
            while self._inbox:
                item = self._inbox.popleft()
                if item is None:
                    closing = True
                else:
                    self._absorb(*item)
            await self._flush(force=closing)
            if closing:
                return self.message_ids
            self._wake.clear()
            if self._inbox:
                continue
            due = None
            if self._pending():
                due = self._last_edit + self._min_edit - self._clock.monotonic() - _EPSILON
            await _sleep_or_event(self._clock, due, self._wake)

    def _absorb(self, name: str, arguments: Mapping[str, Any]) -> None:
        if self.mode == "log":
            if self._audit is not None:
                with contextlib.suppress(OSError):
                    self._audit.write(
                        progress_line(name, arguments, verbose=True, scope=self._scope)
                    )
            return
        if self.mode == "new" and name == self._last_name:
            return
        self._last_name = name
        line = progress_line(name, arguments, verbose=self.mode == "verbose", scope=self._scope)
        if line == self._last_line:
            self._repeats += 1
            # a counter after a closing fence would break the fence: give it its own line
            joiner = "\n" if line.endswith("```") else " "
            counted = f"{line}{joiner}(×{self._repeats + 1})"
            if self._separate:
                self._unsent.append(counted)
            elif self._lines:
                self._lines[-1] = counted
            return
        self._last_line, self._repeats = line, 0
        if self._separate:
            self._unsent.append(line)
        else:
            self._lines.append(line)

    def _pending(self) -> bool:
        if self._separate:
            return bool(self._unsent)
        return "\n".join(self._lines) != self._shown

    def _render(self, text: str) -> str:
        return self._fmt.format(text)

    async def _send(self, text: str) -> str | None:
        result = await ports.send_text(
            self._port, self._chat_id, self._render(text), final=False, meta=self._meta
        )
        if result.ok and result.message_id:
            self.message_ids.append(result.message_id)
            return result.message_id
        return None

    async def _flush(self, *, force: bool) -> None:
        if self._separate:
            while self._unsent:
                await self._send(self._unsent.pop(0))
            return
        if not self._pending():
            return
        now = self._clock.monotonic()
        if not force and now - self._last_edit < self._min_edit - _EPSILON:
            return
        self._last_edit = now
        groups = self._groups()
        if len(groups) > 1:  # the bubble is full: finish it, continue in new ones
            rest = groups
            if self._bubble is not None:
                first = "\n".join(groups[0])
                if first != self._shown:
                    await self._edit(first)
                rest = groups[1:]
            for group in rest:
                self._bubble = await self._send("\n".join(group))
            self._lines = list(groups[-1])
            self._shown = "\n".join(self._lines) if self._bubble is not None else ""
            return
        text = "\n".join(self._lines)
        if self._bubble is None:
            self._bubble = await self._send(text)
            if self._bubble is not None:
                self._shown = text
            else:
                self._separate = True
            return
        await self._edit(text)

    def _groups(self) -> list[list[str]]:
        groups: list[list[str]] = []
        current: list[str] = []
        for line in self._lines:
            candidate = [*current, line]
            if current and self._len(self._render("\n".join(candidate))) > self._limit:
                groups.append(current)
                candidate = [line]
            current = candidate
        return [*groups, current] if current else groups

    async def _edit(self, text: str) -> None:
        assert self._bubble is not None
        result = await ports.edit_text(
            self._port,
            self._chat_id,
            self._bubble,
            self._render(text),
            final=False,
            meta=self._meta,
        )
        if result.ok:
            self._shown = text
        elif not ports.is_flood(result) and not result.retryable:
            # this bubble cannot be edited: what it does not show yet goes out line by line
            seen = len(self._shown.split("\n")) if self._shown else 0
            self._separate = True
            self._unsent = self._lines[seen:]


# -- typing ----------------------------------------------------------------------------------------


class TypingLoop:
    """Keeps the typing indicator alive while a turn runs; paused while an approval waits."""

    def __init__(self, port: Adapter, chat_id: str, *, clock: Clock, interval: float = 2.0) -> None:
        self._port = port
        self._chat_id = chat_id
        self._clock = clock
        self._interval = interval
        self._changed = asyncio.Event()
        self._paused = False
        self._stopped = False

    def pause(self) -> None:
        self._paused = True
        self._changed.set()

    def resume(self) -> None:
        self._paused = False
        self._changed.set()

    def stop(self) -> None:
        self._stopped = True
        self._changed.set()

    async def run(self) -> None:
        try:
            while not self._stopped:
                self._changed.clear()
                if self._paused:
                    await ports.typing(self._port, self._chat_id, on=False)
                    await self._changed.wait()
                    continue
                await ports.typing(self._port, self._chat_id, on=True)
                await _sleep_or_event(self._clock, self._interval, self._changed)
        finally:
            with contextlib.suppress(Exception):
                await ports.typing(self._port, self._chat_id, on=False)


# -- the long-turn heartbeat -----------------------------------------------------------------------


def heartbeat_text(minutes: int) -> str:
    return f"Still working… {minutes} min" if minutes else "Still working…"


class Heartbeat:
    """Posts a "still working" line every ``interval`` seconds, edited in place."""

    def __init__(
        self,
        port: Adapter,
        chat_id: str,
        *,
        clock: Clock,
        interval: float = 180.0,
        meta: Mapping[str, Any] | None = None,
        text: Callable[[int], str] = heartbeat_text,
    ) -> None:
        self._port = port
        self._chat_id = chat_id
        self._clock = clock
        self._interval = interval
        self._meta = dict(meta or {})
        self._text = text
        self._stop = asyncio.Event()
        self.message_id: str | None = None

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        if self._interval <= 0:
            return
        started = self._clock.monotonic()
        while True:
            await _sleep_or_event(self._clock, self._interval, self._stop)
            if self._stop.is_set():
                return
            minutes = int((self._clock.monotonic() - started) // 60)
            text = self._text(minutes)
            if self.message_id is not None and ports.can_edit(self._port):
                result = await ports.edit_text(
                    self._port, self._chat_id, self.message_id, text, final=False, meta=self._meta
                )
                if result.ok:
                    continue
            result = await ports.send_text(
                self._port, self._chat_id, text, final=False, meta=self._meta
            )
            if result.ok and result.message_id:
                self.message_id = result.message_id
