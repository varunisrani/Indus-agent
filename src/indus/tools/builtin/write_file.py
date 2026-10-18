# pyright: strict
"""``write_file``: replace a whole file behind the write guards and the stale-write rule (P05-18).

Guards, in order (``check_write_targets``, shared with ``patch`` and V4A patches):

1. Protected paths after realpath (``safety.path_guards.write_denial``): system locations,
   credential folders in every reachable home, Indus's own secret and control files. Refused.
2. Plain text over a binary document (always for office/ODF/EPUB/RTF; over an existing PDF or other
   binary file). Refused.
3. Protected instruction files (``AGENTS.md``, ``CLAUDE.md``, ``PERSONA.md``, ``.cursorrules``, any
   case, plus ``security.protected_instruction_files`` globs): every write asks the person through
   an ``approval`` server request, answers ``once``/``deny`` only, not bypassed by auto-approve;
   without a surface it is blocked. ``~/.ssh/config`` asks the same way.
4. Content that is our own read output (``N|text`` lines) or the unchanged-read stub. Refused.

Then, under a per-path lock, the stale-write rule (R02.14): an existing file is replaced only when
this task holds a baseline for it (``read_tracking``: a full unredacted read, its own write, or
its own patch made from a baseline) and the bytes on disk still hash to it. Otherwise nothing is
written. Writes keep a CRLF file CRLF and keep a UTF-8 BOM; the result says ``verified: true``
once the bytes on disk hash to what was meant, and that hash becomes the new baseline.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import posixpath
import weakref
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from indus.foundation.config.schema import default_for
from indus.foundation.errors import IndusError
from indus.foundation.scope import Scope
from indus.protocol.events import CancelReason
from indus.protocol.requests_port import RequestUnsupported, RequestWithdrawn, ServerRequestPort
from indus.protocol.server_requests import ApprovalAnswer, ApprovalChoice
from indus.safety import path_guards
from indus.safety.redaction import redact_output
from indus.tools.builtin import lint, mutation_hooks, paths, read_tracking
from indus.tools.builtin.exec_host import CancelLike, ExecHost
from indus.tools.builtin.file_ops import (
    FileOpError,
    FileOps,
    environment_of,
    is_backend_error,
    ops_for_call,
)
from indus.tools.builtin.read_file import STUB_MESSAGE
from indus.tools.calls import ToolResult
from indus.tools.registry import ToolRegistry, ToolTraits

__all__ = [
    "WRITE_FILE_SCHEMA",
    "Target",
    "check_write_targets",
    "encode_like",
    "lint_after",
    "lint_gate",
    "is_tool_display_text",
    "path_lock",
    "register_tools",
    "resolve_target",
    "write_file",
    "write_verified",
]

TOOL_NAME = "write_file"
TOOLSET = "file"
GLOBS_KEY = "security.protected_instruction_files"
REWRITE_MIN_CHARS = 20_000
REWRITE_MAX_CHARS = 400_000
REWRITE_MIN_UNCHANGED = 0.80
UTF8_BOM = b"\xef\xbb\xbf"
MISSING_CONTENT = (
    "content is missing. write_file needs both path and content. When a very long content "
    "argument goes missing it was usually dropped because the context is nearly full: write the "
    "file in smaller parts (create it, then add to it with patch), or generate it with a script "
    "through the terminal."
)


@dataclass(frozen=True)
class Target:
    """One path a write touches: as typed, as normalised text, and its real path."""

    raw: str
    literal: str
    resolved: str

    @property
    def candidates(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((self.literal, self.resolved)))


def resolve_target(raw: str, ops: FileOps, scope: Scope | None) -> Target:
    base = paths.base_dir(scope, ops.cwd, host=ops.host)
    literal = paths.expand_tilde(raw.strip(), scope)
    if not posixpath.isabs(literal):
        literal = posixpath.join(base, literal)
    literal = posixpath.normpath(literal)
    return Target(raw, literal, paths.resolve(raw, base=base, scope=scope, host=ops.host))


def _error(message: str, **extra: Any) -> str:
    return ToolResult.error(message, **extra).text


# -- approval --------------------------------------------------------------------------------------


def _approval_timeout(scope: Scope | None) -> float:
    value = scope.config.get("approvals.timeout") if scope is not None and scope.config else None
    if not isinstance(value, int | float) or isinstance(value, bool) or value <= 0:
        value = default_for("approvals.timeout", 300)
    return float(value)


async def ask_write_approval(
    broker: ServerRequestPort | None,
    *,
    targets: Sequence[str],
    description: str,
    scope: Scope | None,
    turn_id: str | None,
) -> str | None:
    """None when the person approved; otherwise why the write is blocked (deny, timeout and
    "not answered" are worded apart, R02.5)."""
    shown = ", ".join(dict.fromkeys(targets))
    blocked = f"Blocked: the write to {shown} {{why}} The file was not changed; do not retry it "
    blocked += "another way (terminal, scripts) without the user's go-ahead."
    if broker is None:
        return blocked.format(why="needs the user's approval, but nobody here can answer.")
    payload = {
        "command": redact_output(f"write {shown}", scope=scope, force=True),
        "description": description,
        "choices": [ApprovalChoice.ONCE.value, ApprovalChoice.DENY.value],
    }
    try:
        answer = await broker.ask(
            "approval", payload, timeout=_approval_timeout(scope), turn_id=turn_id
        )
    except RequestUnsupported:
        return blocked.format(why="needs the user's approval, but the client cannot show it.")
    except RequestWithdrawn as exc:
        if exc.reason is CancelReason.TIMED_OUT:
            return blocked.format(why="was not approved in time; silence is not consent.")
        return blocked.format(why=f"was not answered (the request ended: {exc.reason.value}).")
    if isinstance(answer, ApprovalAnswer) and answer.choice is not ApprovalChoice.DENY:
        return None
    return blocked.format(why="was denied by the user.")


# -- the shared guard pass -----------------------------------------------------------------------


def _globs(scope: Scope | None) -> list[str]:
    value = scope.config.get(GLOBS_KEY) if scope is not None and scope.config else None
    if isinstance(value, list):
        return [str(v) for v in value if isinstance(v, str) and v]  # pyright: ignore[reportUnknownVariableType]
    return []


async def check_write_targets(
    targets: Sequence[Target],
    *,
    content_targets: Iterable[Target],
    ops: FileOps,
    scope: Scope | None,
    broker: ServerRequestPort | None,
    turn_id: str | None,
) -> str | None:
    """Guards 1-3 of the module notes over every path of one call; the first refusal, or None.
    One approval question covers every protected path of the call."""
    for target in targets:
        refused = path_guards.nt_namespace_error(target.raw, verb="Write")
        refused = refused or path_guards.write_denial(target.candidates, scope)
        if refused:
            return refused
    for target in content_targets:
        exists = (await ops.stat(target.resolved)).kind == "file"
        for candidate in target.candidates:
            refused = path_guards.binary_document_reason(candidate, exists=exists)
            if refused:
                return refused
    globs = _globs(scope)
    protected = [
        name
        for t in targets
        if (name := path_guards.protected_instruction_name(t.candidates, globs)) is not None
    ]
    if protected:
        refused = await ask_write_approval(
            broker,
            targets=[t.raw for t in targets],
            description=(
                f"Write to {', '.join(dict.fromkeys(protected))}: instruction files steer future "
                "agent runs, so every write needs your approval (auto-approve does not apply)."
            ),
            scope=scope,
            turn_id=turn_id,
        )
        if refused:
            return refused
    for target in targets:
        reason = path_guards.write_needs_approval(target.candidates, scope)
        if reason:
            refused = await ask_write_approval(
                broker, targets=[target.raw], description=reason, scope=scope, turn_id=turn_id
            )
            if refused:
                return refused
    return None


def is_tool_display_text(content: str) -> bool:
    """True for our own read output pasted back: the stub text, or ``N|text`` lines that are
    mostly consecutive numbered lines."""
    stripped = content.strip()
    if STUB_MESSAGE in stripped and len(stripped) <= 2 * len(STUB_MESSAGE):
        return True
    lines = [line for line in content.splitlines() if line.strip()]
    if len(lines) < 2:
        return False
    numbers: list[int] = []
    for line in lines:
        head, bar, _ = line.lstrip().partition("|")
        if bar and head.isdigit():
            numbers.append(int(head))
    if len(numbers) < 2 or len(numbers) < 0.6 * len(lines):
        return False
    steps = sum(1 for a, b in zip(numbers, numbers[1:], strict=False) if b == a + 1)
    return steps >= len(numbers) - 1


# -- locking and writing --------------------------------------------------------------------------

_LOCKS: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()


def path_lock(path: str) -> asyncio.Lock:
    """The lock that serialises read-check-write on ``path`` in this process."""
    lock = _LOCKS.get(path)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[path] = lock
    return lock


def encode_like(text: str, old: bytes | None) -> bytes:
    """``text`` as UTF-8 bytes shaped like ``old``: CRLF kept for a CRLF file sent with bare
    newlines, a UTF-8 BOM kept. Raises ``UnicodeEncodeError`` for unwritable characters."""
    if old is not None:
        if b"\r\n" in old[:65536] and "\r\n" not in text and "\n" in text:
            text = text.replace("\n", "\r\n")
        if old.startswith(UTF8_BOM) and not text.startswith("﻿"):
            text = "﻿" + text
    return text.encode("utf-8", "surrogateescape")


async def write_verified(ops: FileOps, path: str, data: bytes) -> tuple[bool, bool | None]:
    """Write ``data`` and hash it back: ``(dirs_created, verified)``; ``verified`` None when the
    backend could not hash. A hash that differs raises ``FileOpError``."""
    created = await ops.write_bytes(path, data)
    digest = await ops.sha256(path)
    if digest is None:
        return created, None
    if digest != hashlib.sha256(data).hexdigest():
        raise FileOpError(
            "failed",
            path,
            f"after writing {path} the bytes on disk differ from what was sent; read the file "
            "and try again",
        )
    return created, True


# -- lint after edit (P05-20) ---------------------------------------------------------------------


def lint_gate(path: str, old_text: str | None, new_text: str) -> str | None:
    """A refusal before ``new_text`` replaces ``old_text`` (None: a new file) at ``path``: the
    fail-closed parse rule for data files (``lint.gate``). None when the write may go ahead."""
    return lint.gate(path, old_text, new_text)


async def lint_after(
    path: str,
    old_text: str | None,
    new_text: str,
    *,
    host: ExecHost | None = None,
    cancel: CancelLike | None = None,
) -> dict[str, Any] | None:
    """The lint verdict on an edit just written (``lint.lint_edit``), for the result's ``lint``
    field; None when the file is clean or no checker applies. Shell checkers run in ``host``."""
    report = await lint.lint_edit(path, old_text, new_text, host=host, cancel=cancel)
    if report.status in ("introduced", "preexisting"):
        return report.as_dict()
    return None


def _rewrite_hint(old: bytes | None, new_text: str) -> str | None:
    if old is None or not REWRITE_MIN_CHARS <= len(new_text) <= REWRITE_MAX_CHARS:
        return None
    old_text = old.decode("utf-8", errors="replace")
    if not REWRITE_MIN_CHARS <= len(old_text) <= REWRITE_MAX_CHARS:
        return None
    old_lines, new_lines = old_text.splitlines(), new_text.splitlines()
    if not old_lines:
        return None
    same = sum((Counter(old_lines) & Counter(new_lines)).values())
    ratio = same / max(len(old_lines), len(new_lines))
    if ratio < REWRITE_MIN_UNCHANGED:
        return None
    return (
        f"{ratio:.0%} of the lines were already on disk ({same:,} of {len(new_lines):,}). For "
        "edits like this use patch: it sends only the changed text."
    )


def _stale_reason(
    tracker: read_tracking.TaskReads, resolved: str, current: str | None
) -> str | None:
    if current is None:
        return None
    baseline = tracker.baseline(resolved)
    if baseline is not None and baseline == current:
        return None
    if baseline is not None:
        return "it changed on disk after you last read or wrote it (someone else edited it)."
    if tracker.blind_patch(resolved) == current:
        return (
            "your own patch changed it without a full view of the file; read it in full before "
            "replacing the whole file, or keep using patch."
        )
    return (
        "this task has not seen its whole current content (never read, read only in part or "
        "with secrets masked)."
    )


# -- the tool --------------------------------------------------------------------------------------


async def write_file(
    args: Mapping[str, Any],
    *,
    scope: Scope | None = None,
    task_id: str | None = None,
    session_id: str | None = None,
    turn_id: str | None = None,
    environments: Any = None,
    cancel: Any = None,
    broker: ServerRequestPort | None = None,
) -> str:
    raw = args.get("path")
    if not isinstance(raw, str) or not raw.strip():
        return _error("path is required: the file to write, absolute or relative to the cwd")
    content = args.get("content")
    if content is None:
        return _error(MISSING_CONTENT)
    if not isinstance(content, str):
        return _error("content must be text (the whole new file)")
    try:
        ops = await ops_for_call(
            scope,
            task_id=task_id,
            session_id=session_id,
            environments=environments,
            cancel=cancel,
        )
        target = resolve_target(raw, ops, scope)
        refused = await check_write_targets(
            [target],
            content_targets=[target],
            ops=ops,
            scope=scope,
            broker=broker,
            turn_id=turn_id,
        )
        if refused:
            return _error(refused)
        if is_tool_display_text(content):
            return _error(
                "Refusing to write read_file display text (LINE|CONTENT lines or the unchanged "
                "notice) as file content. Remove the line-number prefixes and write the real text."
            )
        tracker = read_tracking.tracker_for(scope, task_id)
        notes = await mutation_hooks.before_file_change(
            ops, scope=scope, session_id=session_id, turn_id=turn_id, paths=[target.resolved]
        )
        async with path_lock(target.resolved):
            written = await _write_locked(ops, target, content, tracker, cancel=cancel)
        return mutation_hooks.with_notes(written, notes)
    except IndusError as exc:
        if not is_backend_error(exc):
            raise
        return _error(f"The file was not written: the terminal backend is unavailable ({exc}).")
    except FileOpError as exc:
        return _error(f"The file was not written: {exc.detail or exc}")


async def _write_locked(
    ops: FileOps,
    target: Target,
    content: str,
    tracker: read_tracking.TaskReads,
    *,
    cancel: CancelLike | None = None,
) -> str:
    resolved = target.resolved
    info = await ops.stat(resolved)
    if info.kind == "dir":
        return _error(f"{target.raw!r} is a directory; give a file path.")
    if info.kind == "other":
        return _error(f"{target.raw!r} exists and is not a regular file; nothing was written.")
    old: bytes | None = await ops.read_bytes(resolved) if info.kind == "file" else None
    current = hashlib.sha256(old).hexdigest() if old is not None else None
    stale = _stale_reason(tracker, resolved, current)
    if stale:
        body = {
            "error": (
                f"Refusing to overwrite {target.raw}: {stale} The file was NOT changed. Read the "
                "current file with read_file (every page if it needs offset), merge your change "
                "into it, then call write_file again. For small edits prefer patch."
            ),
            "stale_write_blocked": True,
            "path": target.raw,
            "resolved_path": resolved,
        }
        return json.dumps(body, ensure_ascii=False)
    try:
        data = encode_like(content, old)
    except UnicodeEncodeError:
        return _error("content has characters that cannot be written as UTF-8 (lone surrogates)")
    hint = _rewrite_hint(old, content)
    old_text = old.decode("utf-8", "surrogateescape") if old is not None else None
    refused = lint_gate(resolved, old_text, content)
    if refused:
        return _error(refused)
    created, verified = await write_verified(ops, resolved, data)
    verdict = await lint_after(resolved, old_text, content, host=environment_of(ops), cancel=cancel)
    tracker.after_write(resolved)
    tracker.set_baseline(resolved, hashlib.sha256(data).hexdigest())
    body: dict[str, Any] = {"bytes_written": len(data), "dirs_created": created}
    if verified is not None:
        body["verified"] = verified
    body["resolved_path"] = resolved
    body["files_modified"] = [resolved]
    if verdict is not None:
        body["lint"] = verdict
    if hint:
        body["hint"] = hint
    return json.dumps(body, ensure_ascii=False)


WRITE_FILE_SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Create a file or replace its whole content (parent folders are created). To change an "
        "existing file you must have read all of it with read_file first, and it must not have "
        "changed since; otherwise the write is refused and nothing changes. For edits to part "
        "of a file use patch instead. Writes to system and credential locations are refused, "
        "and instruction files such as AGENTS.md ask the user first."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "The file: absolute, ~/..., or relative to the working directory.",
            },
            "content": {"type": "string", "description": "The complete new content of the file."},
        },
        "required": ["path", "content"],
    },
}


def register_tools(registry: ToolRegistry) -> None:
    registry.register(
        TOOL_NAME,
        TOOLSET,
        WRITE_FILE_SCHEMA,
        write_file,
        traits=ToolTraits(path_arg="path", writer=True, mutating=True),
    )
