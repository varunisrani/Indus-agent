"""The interactive trajectory saver (P22-10): finished chat turns → training samples on disk.

With ``indus chat --save-trajectories`` (or config ``research.save_trajectories: true``) every
finished turn of a CLI session appends the conversation so far as one sample to a JSON Lines
file in the session's working directory: ``trajectory_samples.jsonl`` when the turn completed,
``failed_trajectories.jsonl`` when it did not. A line is ``{conversations, timestamp, model,
completed}``; ``conversations`` comes from the shared converter (``trajectory.convert``), so it
has exactly the shape of a batch row's (R10.7).

Nothing secret reaches the disk: every turn value goes through the P05 output redactor in force
mode before it is written (whatever ``security.redact_secrets`` says).

Several writers may append to one file at the same time (two chats, a gateway, batch workers), so
``append_locked`` holds an exclusive lock on the whole file around write + flush: ``flock`` on
POSIX, a one-byte ``msvcrt.locking`` range at offset 0 on Windows. The line is serialised before
the lock is taken. The SWE runner appends its lines through the same helper.

The listener runs in the service's end-of-turn bookkeeping (so even a one-shot run that closes
right after its turn keeps the sample) and never raises: a failed save is a warning in the log.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC
from pathlib import Path
from typing import IO, Any, Protocol

from indus.foundation.scope import Scope
from indus.research.trajectory import content_text, convert
from indus.safety.redaction import redact_output

__all__ = [
    "COMPLETED_FILE",
    "CONFIG_KEY",
    "FAILED_FILE",
    "TrajectorySaver",
    "append_locked",
    "attach",
    "enabled",
    "redact_turns",
    "sample_record",
    "stored_messages",
    "target_file",
]

log = logging.getLogger(__name__)

COMPLETED_FILE = "trajectory_samples.jsonl"
FAILED_FILE = "failed_trajectories.jsonl"
CONFIG_KEY = "research.save_trajectories"


# -- the file lock ---------------------------------------------------------------------------------

if sys.platform == "win32":
    import msvcrt

    def _lock(handle: IO[bytes], acquire: bool) -> None:
        """One byte at offset 0 stands for the whole file; the append position is restored."""
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK if acquire else msvcrt.LK_UNLCK, 1)
        handle.seek(0, os.SEEK_END)

else:
    import fcntl

    def _lock(handle: IO[bytes], acquire: bool) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX if acquire else fcntl.LOCK_UN)


def append_locked(path: Path, record: Mapping[str, Any]) -> None:
    """Append ``record`` as one JSON line while holding the file's exclusive lock."""
    data = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    with open(path, "ab") as handle:
        _lock(handle, True)
        try:
            handle.write(data)
            handle.flush()
        finally:
            _lock(handle, False)


# -- the record ------------------------------------------------------------------------------------


def target_file(directory: Path, completed: bool) -> Path:
    return directory / (COMPLETED_FILE if completed else FAILED_FILE)


def sample_record(
    turns: Sequence[Mapping[str, str]], *, model: str, completed: bool, timestamp: str
) -> dict[str, Any]:
    return {
        "conversations": [dict(turn) for turn in turns],
        "timestamp": timestamp,
        "model": model,
        "completed": completed,
    }


def redact_turns(turns: Sequence[Mapping[str, str]], scope: Scope | None) -> list[dict[str, str]]:
    """Every value masked by the P05 redactor (force mode: the switch cannot turn it off)."""
    return [
        {**turn, "value": redact_output(turn.get("value", ""), force=True, scope=scope)}
        for turn in turns
    ]


def first_prompt(messages: Sequence[Mapping[str, Any]]) -> str | None:
    for message in messages:
        if message.get("role") == "user":
            return content_text(message.get("content"))
    return None


def enabled(scope: Scope, flag: bool = False) -> bool:
    """The ``--save-trajectories`` flag, else config ``research.save_trajectories``."""
    if flag:
        return True
    config: Any = scope.config
    if config is None:
        from indus.foundation import config as config_mod

        try:
            config = config_mod.load(scope)
        except Exception:
            log.warning("the config could not be read; trajectories are not saved", exc_info=True)
            return False
    return bool(config.get(CONFIG_KEY))


# -- the session listener --------------------------------------------------------------------------


class _Ended(Protocol):
    @property
    def session_id(self) -> str: ...

    @property
    def completed(self) -> bool: ...


class _Service(Protocol):
    store: Any

    def current_id(self, session_id: str) -> str: ...

    def agent(self, session_id: str) -> Any: ...

    def on_turn_end(self, listener: Any) -> Callable[[], None]: ...


def stored_messages(store: Any, session_id: str) -> list[dict[str, Any]]:
    """The session's live transcript as provider-shaped messages, read right now (a P02 state
    store through its handle, or an in-process ``MemoryStore``)."""
    handle = getattr(store, "handle", None)
    if handle is not None:
        from indus.core.persist import rows_from_store
        from indus.state import messages

        rows = rows_from_store(messages.get(handle, session_id))
    else:
        rows = list(getattr(store, "rows", {}).get(session_id, []))
    return [row.to_message() for row in rows if not getattr(row, "scaffold", False)]


class TrajectorySaver:
    """Saves each finished turn of the sessions of one service (see the module notes).

    The listener is synchronous on purpose: it runs inside the service's end-of-turn bookkeeping,
    before ``wait`` returns, so a one-shot run that closes the service right after its turn
    cannot lose the sample. Reading the committed rows and appending one line takes
    milliseconds."""

    def __init__(self, scope: Scope, directory: Path) -> None:
        self.scope = scope
        self.directory = directory
        self.saved: list[Path] = []

    def attach(self, service: _Service) -> Callable[[], None]:
        def listener(ended: _Ended) -> None:
            self.save_turn(service, ended)

        return service.on_turn_end(listener)

    def save_turn(self, service: _Service, ended: _Ended) -> Path | None:
        try:
            session_id = service.current_id(ended.session_id)
            agent = service.agent(session_id)
            messages = stored_messages(service.store, session_id)
            prompt = first_prompt(messages)
            if prompt is None:
                return None
            tools = [dict(t) for t in agent.runtime.tools]
            model = str(agent.binding.state.model)
            return self.write(messages, prompt, tools, model, ended.completed)
        except Exception:
            log.warning("the trajectory of this turn was not saved", exc_info=True)
            return None

    def write(
        self,
        messages: Sequence[Mapping[str, Any]],
        prompt: str,
        tools: Sequence[Mapping[str, Any]],
        model: str,
        completed: bool,
    ) -> Path:
        turns = convert(messages, prompt=prompt, tools=tools).turns
        record = sample_record(
            redact_turns(turns, self.scope),
            model=model,
            completed=completed,
            timestamp=self.scope.clock.now().astimezone(UTC).isoformat(),
        )
        path = target_file(self.directory, completed)
        append_locked(path, record)
        self.saved.append(path)
        return path


def attach(service: _Service, scope: Scope, directory: Path) -> TrajectorySaver:
    """A saver listening to ``service``'s finished turns, writing into ``directory``."""
    saver = TrajectorySaver(scope, directory)
    saver.attach(service)
    return saver
