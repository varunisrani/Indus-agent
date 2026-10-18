"""Taking a checkpoint before the first file change of a turn (P11-16).

``TRIGGER`` (one per process) is asked by the file tools and the terminal right before they
change files. When checkpoints are on for the session it snapshots the session's working folder
once per turn; a later turn snapshots again. It never blocks the tool: when no checkpoint could be
taken it returns one warning per turn, which the tool adds to its result.

On or off, first match wins: ``/checkpoints on|off`` for this session (``set_enabled``), the
``--checkpoints`` flag of this run (``set_default``), the ``checkpoints.enabled`` setting.

``is_destructive(command)`` decides whether a terminal command changes files: deleting, moving,
truncating or in-place editing programs, a truncating ``>`` redirect to a file, git commands that
rewrite the work tree, ``find -delete``, anything the approval detector calls dangerous, and any
command too odd to parse (fail toward a snapshot).

After its first snapshot the trigger starts the automatic prune once per process and profile
(``checkpoints.auto_prune``; the prune itself throttles to ``min_interval_hours``).
"""

from __future__ import annotations

import functools
import logging
import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from indus.backends.checkpoints.store import ShadowStore, Skipped, config_value
from indus.foundation.blocking import run_blocking
from indus.foundation.scope import Scope
from indus.safety.approvals import shlex_lite
from indus.safety.approvals.detect import detect

__all__ = [
    "CONTAINER_NOTE",
    "TRIGGER",
    "CheckpointTrigger",
    "is_destructive",
]

log = logging.getLogger(__name__)

SNAPSHOT_DEADLINE_S = 60.0
CONTAINER_NOTE = (
    "No checkpoint was taken before this change: the terminal backend's files are not on this "
    "computer, so /rollback cannot restore them."
)

_CHANGING_PROGRAMS = frozenset({"rm", "rmdir", "unlink", "shred", "mv", "truncate", "dd"})
_GIT_CHANGING = frozenset(
    {
        "reset",
        "checkout",
        "clean",
        "restore",
        "stash",
        "switch",
        "rebase",
        "merge",
        "revert",
        "apply",
        "am",
        "rm",
        "mv",
        "pull",
        "cherry-pick",
    }
)
_INPLACE_PROGRAMS = frozenset({"sed", "gsed", "perl", "ruby"})
_INPLACE_FLAG = re.compile(r"^(?:--in-place(?:=.*)?|-[A-Za-z]*i.*)$")
_WRAPPERS = frozenset({"sudo", "doas", "env", "nohup", "time", "command", "nice", "exec"})
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_TRUNCATING = frozenset({">", ">|", "&>"})
_HARMLESS_TARGETS = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"})


def _program(words: list[str]) -> tuple[str, list[str]]:
    """The program name (basename) and its arguments, past assignments and simple wrappers."""
    rest = list(words)
    while rest and (_ASSIGNMENT.match(rest[0]) or os.path.basename(rest[0]) in _WRAPPERS):
        head = os.path.basename(rest.pop(0))
        if head in ("env", "sudo", "doas", "nice"):
            while rest and rest[0].startswith("-"):
                rest.pop(0)
    if not rest:
        return "", []
    return os.path.basename(rest[0]), rest[1:]


def _git_subcommand(args: list[str]) -> str:
    skip_next = False
    for word in args:
        if skip_next:
            skip_next = False
            continue
        if word in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
            skip_next = True
            continue
        if word.startswith("-"):
            continue
        return word
    return ""


def _command_changes_files(program: str, args: list[str]) -> bool:
    if program in _CHANGING_PROGRAMS:
        return True
    if program == "git":
        return _git_subcommand(args) in _GIT_CHANGING
    if program in _INPLACE_PROGRAMS:
        return any(_INPLACE_FLAG.match(word) for word in args)
    if program == "find":
        return "-delete" in args
    return False


def is_destructive(command: str) -> bool:
    """Does ``command`` change files on disk? (see the module notes)"""
    try:
        commands = shlex_lite.parse(command)
    except (shlex_lite.ShellSyntaxError, shlex_lite.ShellLimitError):
        return True
    for simple in commands:
        for redirect in simple.redirects:
            target = redirect.target.text
            if redirect.op in _TRUNCATING and target not in _HARMLESS_TARGETS:
                return True
        program, args = _program([word.text for word in simple.words])
        if program and _command_changes_files(program, args):
            return True
        for word in simple.words:
            if any(is_destructive(sub.text) for sub in word.subs):
                return True
    try:
        return detect(command).dangerous
    except Exception:  # the detector is a second opinion only
        log.debug("approval detector failed on a checkpoint check", exc_info=True)
        return False


@dataclass
class _Session:
    enabled: bool | None = None
    turn: str | None = None
    warned: str | None = None


def _start_thread(work: Callable[[], None]) -> None:
    threading.Thread(target=work, name="indus-checkpoint-prune", daemon=True).start()


class CheckpointTrigger:
    """Once-per-turn snapshots (see the module notes)."""

    def __init__(
        self,
        *,
        store_for: Callable[[Scope], ShadowStore] = ShadowStore.for_scope,
        run_prune: Callable[[Callable[[], None]], None] = _start_thread,
    ) -> None:
        self._store_for = store_for
        self._run_prune = run_prune
        self._lock = threading.Lock()
        self._sessions: dict[tuple[str, str], _Session] = {}
        self._default: bool | None = None
        self._pruned: set[str] = set()

    @staticmethod
    def _key(scope: Scope, session_id: str | None) -> tuple[str, str]:
        return str(scope.home), session_id or "default"

    def _session(self, scope: Scope, session_id: str | None) -> _Session:
        key = self._key(scope, session_id)
        found = self._sessions.get(key)
        if found is None:
            found = self._sessions[key] = _Session()
        return found

    def set_default(self, value: bool | None) -> None:
        """``--checkpoints`` for this run (None: back to the setting)."""
        with self._lock:
            self._default = value

    def set_enabled(self, scope: Scope, session_id: str | None, value: bool | None) -> None:
        """``/checkpoints on|off`` for one session (None: back to the run default)."""
        with self._lock:
            self._session(scope, session_id).enabled = value

    def enabled(self, scope: Scope, session_id: str | None) -> bool:
        with self._lock:
            found = self._sessions.get(self._key(scope, session_id))
            if found is not None and found.enabled is not None:
                return found.enabled
            if self._default is not None:
                return self._default
        return bool(config_value(scope, "checkpoints.enabled", False))

    def forget(self, scope: Scope, session_id: str | None) -> None:
        with self._lock:
            self._sessions.pop(self._key(scope, session_id), None)

    def before_mutation(
        self,
        scope: Scope,
        *,
        session_id: str | None,
        turn_id: str | None,
        workdir: str | Path,
        on_host: bool = True,
        reason: str = "before file changes",
    ) -> str | None:
        """Snapshot ``workdir`` if this is the session's first change in ``turn_id``; returns a
        warning for the tool result (at most once per turn), else None. Never raises."""
        if not self.enabled(scope, session_id):
            return None
        with self._lock:
            state = self._session(scope, session_id)
            if turn_id is not None and state.turn == turn_id:
                return None
            state.turn = turn_id
        warning = self._take(scope, session_id, turn_id, workdir, on_host, reason)
        if warning is None:
            return None
        with self._lock:
            if turn_id is not None and state.warned == turn_id:
                return None
            state.warned = turn_id
        return warning

    def _take(
        self,
        scope: Scope,
        session_id: str | None,
        turn_id: str | None,
        workdir: str | Path,
        on_host: bool,
        reason: str,
    ) -> str | None:
        if not on_host:
            return CONTAINER_NOTE
        try:
            store = self._store_for(scope)
            keep = int(config_value(scope, "checkpoints.max_snapshots", 20))
            taken = store.snapshot(
                workdir, reason, session_id=session_id, turn_id=turn_id, keep=max(keep, 1)
            )
        except Exception as exc:
            log.warning("checkpoint failed: %s: %s", type(exc).__name__, exc)
            return f"No checkpoint was taken before this change ({type(exc).__name__}: {exc})."
        if isinstance(taken, Skipped):
            if taken.kind in ("unchanged", "empty"):
                return None
            log.info("checkpoint skipped: %s", taken.reason)
            return f"No checkpoint was taken before this change: {taken.reason}."
        self._maybe_prune(scope, store)
        return None

    def _maybe_prune(self, scope: Scope, store: ShadowStore) -> None:
        if not config_value(scope, "checkpoints.auto_prune", True):
            return
        home = str(scope.home)
        with self._lock:
            if home in self._pruned:
                return
            self._pruned.add(home)
        retention = float(config_value(scope, "checkpoints.retention_days", 7.0))
        cap = int(config_value(scope, "checkpoints.max_total_size_mb", 500))
        interval = float(config_value(scope, "checkpoints.min_interval_hours", 24.0))
        now = store.now()

        def work() -> None:
            from indus.backends.checkpoints import prune

            try:
                prune.auto_prune(
                    store,
                    now=now,
                    retention_days=retention,
                    max_total_size_mb=cap,
                    min_interval_hours=interval,
                )
            except Exception:
                log.warning("automatic checkpoint prune failed", exc_info=True)

        self._run_prune(work)

    async def abefore_mutation(
        self,
        scope: Scope,
        *,
        session_id: str | None,
        turn_id: str | None,
        workdir: str | Path,
        on_host: bool = True,
        reason: str = "before file changes",
    ) -> str | None:
        """``before_mutation`` off the event loop (tools pool, 60 s); never raises."""
        if not self.enabled(scope, session_id):
            return None
        call = functools.partial(
            self.before_mutation,
            scope,
            session_id=session_id,
            turn_id=turn_id,
            workdir=workdir,
            on_host=on_host,
            reason=reason,
        )
        try:
            return await run_blocking(
                call, pool="tools", deadline=SNAPSHOT_DEADLINE_S, label="checkpoint snapshot"
            )
        except Exception as exc:
            log.warning("checkpoint did not finish: %s: %s", type(exc).__name__, exc)
            return f"No checkpoint was taken before this change ({type(exc).__name__})."


TRIGGER = CheckpointTrigger()
"""The process's trigger (see the module notes)."""
