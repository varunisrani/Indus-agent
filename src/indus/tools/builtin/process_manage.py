# pyright: strict
"""``process_manage``: the model's handle on background jobs started by ``terminal`` (P05-14).

Actions: ``list`` (this conversation's jobs), ``poll`` (status and the last 1,000 characters; it
never uses up the completion message), ``log`` (lines without escape codes; no ``offset`` means
the last ``limit`` lines, an explicit ``offset: 0`` the first; reading through the end of an
ended job's log uses up its completion), ``wait`` (until it ends, the window passes or the turn is
interrupted; default and maximum ``terminal.timeout``), ``kill`` (ordered kill), ``write`` (raw
input), ``submit`` (input plus Enter), ``close`` (end of input) and ``handoff`` (reserved for
subagents, P10).

Two ``wait``/``poll`` calls in a row on a running job that printed nothing new switch on
notify-on-exit and tell the model to stop checking — only where this session can receive the
message, and while ``terminal.process_wait_auto_notify`` is on.

Every result that carries output, and the command echo, is masked for secrets. A job id that
does not name a job of this profile is "not found". The tool name follows the code-observed
reference name (R02.23).
"""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any

from indus.backends.contract import CancelSignal
from indus.backends.process_notify import COMPLETION_TAIL_CHARS
from indus.backends.processes import ProcessRecord, strip_ansi, uptime_s
from indus.foundation.config.schema import default_for
from indus.foundation.scope import Scope, scope_or_none
from indus.safety.redaction import redact_output
from indus.tools.builtin.terminal import TerminalRuntime, runtime
from indus.tools.registry import ToolRegistry

__all__ = ["ACTIONS", "PROCESS_SCHEMA", "ProcessManageTool", "register_tools"]

log = logging.getLogger(__name__)

TOOL_NAME = "process_manage"
TOOLSET = "terminal"
ACTIONS = ("list", "poll", "log", "wait", "kill", "write", "submit", "close", "handoff")
PREVIEW_CHARS = 1_000
LIST_PREVIEW_CHARS = 200
LIST_COMMAND_CHARS = 200
DEFAULT_LOG_LINES = 200
QUIET_CHECKS_TO_NOTIFY = 2
MAX_QUIET_TRACKED = 512

STOP_POLLING_NOTE = (
    "This process is still running and has printed nothing new since your last check. Notify is "
    "now on for it: when it ends, a message with its exit code and last output starts a new turn "
    "by itself, even after this turn is over. Do not wait on or poll {pid} again and do not loop "
    "or sleep for it. Do other work now, or end the turn with a short status."
)
HANDOFF_UNAVAILABLE = (
    "handoff is only for subagents passing a process to their parent, and subagents are not "
    "available in this version. Wait for the process, or kill it."
)


def _dumps(body: Mapping[str, Any]) -> str:
    return json.dumps(body, ensure_ascii=False)


def _config(scope: Scope, key: str, fallback: Any) -> Any:
    value = scope.config.get(key) if scope.config is not None else None
    return default_for(key, fallback) if value is None else value


def _error(message: str) -> str:
    return _dumps({"error": message})


class ProcessManageTool:
    """The ``process_manage`` handler (see the module notes)."""

    def __init__(self, shared: TerminalRuntime | None = None) -> None:
        self._shared = shared
        self._quiet: OrderedDict[str, tuple[int, int]] = OrderedDict()

    @property
    def shared(self) -> TerminalRuntime:
        return self._shared if self._shared is not None else runtime()

    async def __call__(
        self,
        args: Mapping[str, Any],
        *,
        scope: Scope | None = None,
        session_id: str | None = None,
        cancel: CancelSignal | None = None,
    ) -> str:
        scope = scope if scope is not None else scope_or_none()
        if scope is None:
            return _error("No profile is active for this call, so its processes cannot be found.")
        action = str(args.get("action") or "").strip().lower()
        if action == "list":
            return self._list(scope, session_id)
        if action not in ACTIONS:
            return _error(f"Unknown action {action!r}. Use one of: {', '.join(ACTIONS)}.")
        raw_id = args.get("session_id")
        ident = str(raw_id).strip() if raw_id is not None else ""
        if not ident:
            return _error(f"session_id (the process id from terminal) is required for {action}.")
        if action == "handoff":
            return _error(HANDOFF_UNAVAILABLE)
        record = self.shared.processes.get(ident, str(scope.home))
        if record is None:
            return _dumps(
                {"status": "not_found", "error": f"No background process with id {ident}."}
            )
        if action == "poll":
            return self._finish(scope, session_id, record, "poll", self._poll(record))
        if action == "log":
            return self._log(scope, record, args)
        if action == "wait":
            body = await self._wait(scope, record, args.get("timeout"), cancel)
            return self._finish(scope, session_id, record, "wait", body)
        self._forget(record.id)
        if action == "kill":
            return await self._kill(scope, record)
        if action == "close":
            problem = self.shared.processes.close_input(record)
            return _dumps(_status(problem, {"status": "ok", "message": "input closed"}))
        data = args.get("data")
        text = "" if data is None else str(data)
        if action == "submit":
            text += "\n"
        problem = await self.shared.processes.write(record, text)
        return _dumps(_status(problem, {"status": "ok", "bytes_written": len(text.encode())}))

    # -- actions --------------------------------------------------------------------------------

    def _redact(self, scope: Scope, record: ProcessRecord, text: str) -> str:
        return redact_output(text, command=record.command, scope=scope) if text else text

    def _command(self, scope: Scope, record: ProcessRecord) -> str:
        return redact_output(record.command, scope=scope)

    def _head(self, scope: Scope, record: ProcessRecord) -> dict[str, Any]:
        return {
            "session_id": record.id,
            "command": self._command(scope, record),
            "status": record.status,
        }

    @staticmethod
    def _exit_fields(record: ProcessRecord) -> dict[str, Any]:
        body: dict[str, Any] = {"exit_code": record.exit_code, "completion_reason": record.reason}
        if record.source:
            body["termination_source"] = record.source
        return body

    def _completion_output(self, scope: Scope, record: ProcessRecord) -> dict[str, Any]:
        body: dict[str, Any] = {
            "output": self._redact(scope, record, record.tail(COMPLETION_TAIL_CHARS))
        }
        cut = record.total_chars - COMPLETION_TAIL_CHARS
        if cut > 0:
            body["output_cut"] = cut
        return body

    def _list(self, scope: Scope, session_id: str | None) -> str:
        clock = self.shared.processes.clock
        entries: list[dict[str, Any]] = []
        for record in self.shared.processes.list(str(scope.home), session_id):
            entry: dict[str, Any] = {
                "session_id": record.id,
                "command": self._command(scope, record)[:LIST_COMMAND_CHARS],
                "cwd": record.cwd,
                "pid": record.pid,
                "started_at": record.started_at.isoformat(timespec="seconds"),
                "uptime_seconds": uptime_s(record, clock),
                "status": record.status,
                "output_preview": self._redact(scope, record, record.tail(LIST_PREVIEW_CHARS)),
            }
            if record.notify_on_exit:
                entry["notify_on_complete"] = True
            if record.watch is not None and not record.watch.off:
                entry["watch_patterns"] = list(record.watch.patterns)
            if record.persist_on_release:
                entry["persist_on_release"] = True
            if record.exited:
                entry.update(self._exit_fields(record))
            entries.append(entry)
        return _dumps({"processes": entries})

    def _poll(self, record: ProcessRecord) -> dict[str, Any]:
        scope = record.scope
        body: dict[str, Any] = {
            **self._head(scope, record),
            "pid": record.pid,
            "uptime_seconds": uptime_s(record, self.shared.processes.clock),
            "output_preview": self._redact(scope, record, record.tail(PREVIEW_CHARS)),
        }
        if record.exited:
            body.update(self._exit_fields(record))
        return body

    def _log(self, scope: Scope, record: ProcessRecord, args: Mapping[str, Any]) -> str:
        raw_limit = args.get("limit")
        raw_offset = args.get("offset")
        try:
            limit = DEFAULT_LOG_LINES if raw_limit is None else int(raw_limit)
            offset = None if raw_offset is None else int(raw_offset)
        except (TypeError, ValueError):
            return _error("offset and limit are whole numbers of lines.")
        if limit <= 0:
            return _error("limit must be at least 1.")
        if offset is not None and offset < 0:
            return _error("offset counts lines from the start and cannot be negative.")
        lines = strip_ansi(record.buffer).splitlines()
        total = len(lines)
        if offset is None:
            chosen = lines[-limit:]
            reached_end = True
        else:
            chosen = lines[offset : offset + limit]
            reached_end = offset + limit >= total
        if record.exited and reached_end:
            record.consumed = True  # the model has the end of the output in hand
        self._forget(record.id)
        body = {
            **self._head(scope, record),
            "output": self._redact(scope, record, "\n".join(chosen)),
            "total_lines": total,
            "showing": f"{len(chosen)} lines",
        }
        return _dumps(body)

    async def _wait(
        self, scope: Scope, record: ProcessRecord, raw: object, cancel: CancelSignal | None
    ) -> dict[str, Any]:
        limit = float(_config(scope, "terminal.timeout", 180))
        note = ""
        if raw is None:
            seconds = limit
        else:
            try:
                seconds = float(raw)  # pyright: ignore[reportArgumentType]
            except (TypeError, ValueError):
                return {"status": "error", "error": "timeout is a whole number of seconds."}
            if seconds <= 0:
                return {"status": "error", "error": f"timeout must be positive (got {seconds:g})."}
            if seconds > limit:
                note = f"The wait of {seconds:g}s was shortened to the {limit:g}s limit."
                seconds = limit
        outcome = await self.shared.processes.wait(record, seconds, cancel)
        if outcome == "exited":
            record.consumed = True
            body = {
                **self._head(scope, record),
                **self._exit_fields(record),
                **self._completion_output(scope, record),
            }
        elif outcome == "interrupted":
            body = {
                **self._head(scope, record),
                "status": "interrupted",
                "output": self._redact(scope, record, record.tail(PREVIEW_CHARS)),
                "process_running": True,
                "note": "The wait was interrupted; the process keeps running.",
            }
        else:
            told = (
                "A message arrives when it ends; do other work instead of waiting again."
                if record.notify_on_exit
                else "Check again later, or start such jobs with notify=true to get a message."
            )
            body = {
                **self._head(scope, record),
                "status": "timeout",
                "output": self._redact(scope, record, record.tail(PREVIEW_CHARS)),
                "process_running": True,
                "timeout_note": (
                    f"{note + ' ' if note else ''}The {seconds:g}s wait ended and the process is "
                    f"still running (up {uptime_s(record, self.shared.processes.clock)}s). This "
                    f"is not an error. {told}"
                ),
            }
            return body
        if note:
            body["timeout_note"] = note
        return body

    async def _kill(self, scope: Scope, record: ProcessRecord) -> str:
        if record.exited:
            record.consumed = True
            body = {
                **self._head(scope, record),
                "status": "already_exited",
                **self._exit_fields(record),
                **self._completion_output(scope, record),
            }
            return _dumps(body)
        await self.shared.processes.kill(record)
        body = {
            **self._head(scope, record),
            "status": "killed",
            **self._exit_fields(record),
            **self._completion_output(scope, record),
        }
        return _dumps(body)

    # -- the stop-polling policy -----------------------------------------------------------------

    def _forget(self, process_id: str) -> None:
        self._quiet.pop(process_id, None)

    def _finish(
        self,
        scope: Scope,
        session_id: str | None,
        record: ProcessRecord,
        action: str,
        body: dict[str, Any],
    ) -> str:
        running = body.get("status") in ("running", "timeout")
        if not running:
            if body.get("status") != "interrupted":
                self._forget(record.id)
            return _dumps(body)
        last_total, count = self._quiet.pop(record.id, (0, 0))
        count = count + 1 if record.total_chars == last_total else 0
        self._quiet[record.id] = (record.total_chars, count)
        while len(self._quiet) > MAX_QUIET_TRACKED:
            self._quiet.popitem(last=False)
        if count < QUIET_CHECKS_TO_NOTIFY or record.exited:
            return _dumps(body)
        if not bool(_config(scope, "terminal.process_wait_auto_notify", True)):
            return _dumps(body)
        if record.session_id != session_id or not self.shared.deliveries.deliverable(
            record.home, record.session_id
        ):
            return _dumps(body)  # nothing would reach this caller: keep plain polling
        record.notify_on_exit = True
        body["notify_on_complete"] = True
        stop = STOP_POLLING_NOTE.format(pid=record.id)
        if action == "wait":
            body["timeout_note"] = stop
        else:
            body["note"] = stop
        log.info("%s: checked twice with no new output; notify on exit switched on", record.id)
        return _dumps(body)

    def register(self, registry: ToolRegistry) -> None:
        registry.register(TOOL_NAME, TOOLSET, PROCESS_SCHEMA, self, is_async=True)


def _status(problem: str | None, ok: dict[str, Any]) -> dict[str, Any]:
    if problem is None:
        return ok
    if "already ended" in problem:
        return {"status": "already_exited", "error": problem}
    return {"status": "error", "error": problem}


PROCESS_SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Check on, wait for or stop background processes started with terminal(background=true). "
        "list: this conversation's processes. poll: status and recent output. log: the output in "
        "pages of lines. wait: block until it ends or the timeout passes (a timeout is not an "
        "error). kill: stop it. write sends text as it is; submit sends text plus Enter (use it "
        "to answer prompts); close ends its input. handoff is for subagents only."
    ),
    "parameters": {
        "type": "object",
        "required": ["action"],
        "properties": {
            "action": {"type": "string", "enum": list(ACTIONS)},
            "session_id": {
                "type": "string",
                "description": "The process id from terminal's result (any unique start of it, "
                "e.g. '4dae' for proc_4dae56ca81f6). Needed by every action but list.",
            },
            "data": {"type": "string", "description": "Text for write or submit."},
            "timeout": {
                "type": "integer",
                "minimum": 1,
                "description": "Longest wait in seconds for wait.",
            },
            "offset": {
                "type": "integer",
                "minimum": 0,
                "description": "First log line to show (0 = the start); leave out for the last "
                "lines.",
            },
            "limit": {"type": "integer", "minimum": 1, "description": "How many log lines."},
        },
    },
}


DEFAULT_TOOL = ProcessManageTool()


def register_tools(registry: ToolRegistry) -> None:
    DEFAULT_TOOL.register(registry)
