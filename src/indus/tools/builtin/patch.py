# pyright: strict
"""``patch``: replace one piece of a file, found with fuzzy matching (P05-19).

The call names ``path``, ``old_string``, ``new_string`` and optionally ``replace_all``. The file's
exact bytes are read, ``fuzzy_match.find_and_replace`` finds the span (nine strategies, see that
module), and the new bytes are written back through the same guards as ``write_file`` (protected
paths, binary documents, instruction-file approval) under the same per-path lock. A CRLF file is
matched as LF text and written back CRLF; a UTF-8 BOM is kept. The result carries a unified diff
and the name of the strategy that matched.

Baselines (R02.14): a patch of a file this task held a current baseline for keeps the task's
baseline (it knows every byte it wrote); a patch without one is recorded as a blind patch and
gives no baseline, so a later whole-file ``write_file`` still needs a full read.

V4A multi-file patches (P05-20): a call that ``v4a.wants_v4a`` (``mode: "patch"``, or a ``patch``
text without ``old_string``) goes to ``v4a.run_patch_mode`` with ``PatchFiles``, the adapter of
this call's ``FileOps`` and the write guards to ``v4a.PatchFiles``. The handler accepts both shapes
from any model; the schema shows the replace shape (a route-aware schema that offers V4A to the
model families that prefer it is a P06 seam).

Lint after edit (P05-20): ``write_file.lint_gate`` refuses an edit that breaks a parsable data
file before writing; ``write_file.lint_after`` reports errors the edit introduced, running shell
checkers in the task's live environment when there is one.
"""

from __future__ import annotations

import difflib
import hashlib
import json
from collections.abc import Mapping
from typing import Any

from indus.foundation.errors import IndusError
from indus.foundation.scope import Scope
from indus.protocol.requests_port import ServerRequestPort
from indus.tools.builtin import fuzzy_match, read_tracking, v4a
from indus.tools.builtin.exec_host import CancelLike
from indus.tools.builtin.file_ops import (
    FileOpError,
    FileOps,
    environment_of,
    is_backend_error,
    ops_for_call,
)
from indus.tools.builtin.write_file import (
    UTF8_BOM,
    Target,
    check_write_targets,
    encode_like,
    lint_after,
    lint_gate,
    path_lock,
    resolve_target,
    write_verified,
)
from indus.tools.calls import ToolResult
from indus.tools.registry import ToolRegistry, ToolTraits

__all__ = ["PATCH_SCHEMA", "PatchFiles", "apply_text_edit", "patch", "register_tools"]

TOOL_NAME = "patch"
TOOLSET = "file"
FAILURES_BEFORE_ADVICE = 3


def _error(message: str, **extra: Any) -> str:
    return ToolResult.error(message, **extra).text


def _flag(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def _decode(data: bytes) -> tuple[str, bool, bool]:
    """The text to match on, and whether the file had a BOM and uses CRLF throughout."""
    bom = data.startswith(UTF8_BOM)
    text = (
        data[len(UTF8_BOM) :].decode("utf-8", "surrogateescape")
        if bom
        else data.decode("utf-8", "surrogateescape")
    )
    crlf = "\r\n" in text and text.count("\r\n") == text.count("\n")
    return (text.replace("\r\n", "\n") if crlf else text), bom, crlf


def _encode(text: str, bom: bool, crlf: bool) -> bytes:
    if crlf:
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    data = text.encode("utf-8", "surrogateescape")
    return UTF8_BOM + data if bom else data


def _diff(before: str, after: str, name: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{name}",
            tofile=f"b/{name}",
        )
    )


async def apply_text_edit(
    ops: FileOps,
    target: Target,
    old: str,
    new: str,
    *,
    replace_all: bool,
    tracker: read_tracking.TaskReads,
    cancel: CancelLike | None = None,
) -> dict[str, Any]:
    """One replace edit on ``target`` (call under ``path_lock``); the result body, with an
    ``error`` key when nothing was written."""
    resolved = target.resolved
    try:
        data = await ops.read_bytes(resolved)
    except FileOpError as exc:
        if exc.kind == "missing":
            return {"error": f"File not found: {target.raw}. To create a file use write_file."}
        return {"error": f"Could not read {target.raw}: {exc.detail or exc}"}
    content, bom, crlf = _decode(data)
    found = fuzzy_match.find_and_replace(content, old, new, replace_all)
    if found.already_applied:
        return {
            "success": True,
            "no_change": True,
            "note": (
                f"{target.raw} already contains new_string and no longer contains old_string: "
                "the edit is already applied. Nothing was written; do not send it again."
            ),
        }
    if found.error is not None:
        body: dict[str, Any] = {"error": found.error}
        if found.error.startswith(fuzzy_match.NO_MATCH):
            failures = tracker.patch_failed(resolved)
            if failures >= FAILURES_BEFORE_ADVICE:
                body["hint"] = (
                    f"This is miss number {failures} on {target.raw}. Stop varying old_string: "
                    "read the file again, copy a longer unique piece exactly, or read it in full "
                    "and replace it with write_file."
                )
        return body
    try:
        out = _encode(found.content, bom, crlf)
    except UnicodeEncodeError:
        return {"error": "new_string has characters that cannot be written as UTF-8"}
    refused = lint_gate(resolved, content, found.content)
    if refused:
        return {"error": refused}
    before = hashlib.sha256(data).hexdigest()
    vouched = tracker.baseline(resolved) == before
    _, verified = await write_verified(ops, resolved, out)
    lint = await lint_after(
        resolved, content, found.content, host=environment_of(ops), cancel=cancel
    )
    after = hashlib.sha256(out).hexdigest()
    tracker.after_write(resolved)
    tracker.patch_succeeded(resolved)
    if vouched:
        tracker.set_baseline(resolved, after)
    else:
        tracker.note_blind_patch(resolved, after)
    result: dict[str, Any] = {
        "success": True,
        "diff": _diff(content, found.content, target.raw),
        "strategy": found.strategy,
        "replacements": found.count,
    }
    if verified is not None:
        result["verified"] = verified
    result["resolved_path"] = resolved
    result["files_modified"] = [resolved]
    if found.notes:
        result["note"] = "\n".join(found.notes)
    if lint is not None:
        result["lint"] = lint
    return result


class PatchFiles:
    """``v4a.PatchFiles`` over one call's ``FileOps`` and the write guards of ``write_file``.

    ``resolve(path)`` → the absolute path the edit lands on; ``read_text(path)`` → the file's
    text, or None when nothing exists there; ``write_text(path, text)`` → write it under the
    per-path lock with the hash check and baseline bookkeeping of this module (the fail-closed
    parse rule already ran in ``v4a.apply_patch`` before the first write, so a rollback can always
    put the old text back); ``delete(path)``; ``refusal(path, action, content)`` → the
    write-guard chain of ``write_file`` (protected paths, binary documents for a write,
    instruction-file approval), or None. Every method takes the path as the patch names it.
    """

    def __init__(
        self,
        ops: FileOps,
        *,
        scope: Scope | None,
        tracker: read_tracking.TaskReads,
        broker: ServerRequestPort | None = None,
        turn_id: str | None = None,
    ) -> None:
        self.ops = ops
        self.scope = scope
        self.tracker = tracker
        self.broker = broker
        self.turn_id = turn_id
        self._targets: dict[str, Target] = {}

    def target(self, path: str) -> Target:
        found = self._targets.get(path)
        if found is None:
            found = self._targets[path] = resolve_target(path, self.ops, self.scope)
        return found

    def resolve(self, path: str) -> str:
        return self.target(path).resolved

    async def read_text(self, path: str) -> str | None:
        resolved = self.resolve(path)
        try:
            data = await self.ops.read_bytes(resolved)
        except FileOpError as exc:
            if exc.kind == "missing":
                return None
            raise
        text = data.decode("utf-8", "surrogateescape")
        return text[1:] if text.startswith("\ufeff") else text

    async def write_text(self, path: str, content: str) -> None:
        resolved = self.resolve(path)
        async with path_lock(resolved):
            try:
                old: bytes | None = await self.ops.read_bytes(resolved)
            except FileOpError as exc:
                if exc.kind != "missing":
                    raise
                old = None
            data = encode_like(content, old)
            await write_verified(self.ops, resolved, data)
            after = hashlib.sha256(data).hexdigest()
            vouched = (
                old is None or self.tracker.baseline(resolved) == hashlib.sha256(old).hexdigest()
            )
            self.tracker.after_write(resolved)
            if vouched:
                self.tracker.set_baseline(resolved, after)
            else:
                self.tracker.note_blind_patch(resolved, after)

    async def delete(self, path: str) -> None:
        resolved = self.resolve(path)
        async with path_lock(resolved):
            await self.ops.delete(resolved)
            self.tracker.after_write(resolved)
            self.tracker.drop_baseline(resolved)

    async def refusal(self, path: str, action: v4a.WriteAction, content: str | None) -> str | None:
        target = self.target(path)
        writes = action == "write" and content is not None
        return await check_write_targets(
            [target],
            content_targets=[target] if writes else [],
            ops=self.ops,
            scope=self.scope,
            broker=self.broker,
            turn_id=self.turn_id,
        )


async def patch(
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
    if v4a.wants_v4a(args):
        return await _patch_mode(
            args,
            scope=scope,
            task_id=task_id,
            session_id=session_id,
            turn_id=turn_id,
            environments=environments,
            cancel=cancel,
            broker=broker,
        )
    mode = args.get("mode")
    if mode not in (None, "", "replace"):
        return _error(f"mode {mode!r} is not available; use mode replace or mode patch")
    raw, old, new = args.get("path"), args.get("old_string"), args.get("new_string")
    if not isinstance(raw, str) or not raw.strip():
        return _error("path is required: the file to change")
    if not isinstance(old, str) or not isinstance(new, str):
        return _error("old_string and new_string are both required (text)")
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
        tracker = read_tracking.tracker_for(scope, task_id)
        async with path_lock(target.resolved):
            body = await apply_text_edit(
                ops,
                target,
                old,
                new,
                replace_all=_flag(args.get("replace_all")),
                tracker=tracker,
                cancel=cancel,
            )
    except IndusError as exc:
        if not is_backend_error(exc):
            raise
        return _error(f"The file was not changed: the terminal backend is unavailable ({exc}).")
    except FileOpError as exc:
        return _error(f"The file was not changed: {exc.detail or exc}")
    if "error" in body:
        message = str(body.pop("error"))
        return _error(message, **body)
    return json.dumps(body, ensure_ascii=False)


async def _patch_mode(
    args: Mapping[str, Any],
    *,
    scope: Scope | None,
    task_id: str | None,
    session_id: str | None,
    turn_id: str | None,
    environments: Any,
    cancel: Any,
    broker: ServerRequestPort | None,
) -> str:
    """The V4A branch: one multi-file patch, all or nothing (``v4a.run_patch_mode``)."""
    try:
        ops = await ops_for_call(
            scope,
            task_id=task_id,
            session_id=session_id,
            environments=environments,
            cancel=cancel,
        )
        files = PatchFiles(
            ops,
            scope=scope,
            tracker=read_tracking.tracker_for(scope, task_id),
            broker=broker,
            turn_id=turn_id,
        )
        return await v4a.run_patch_mode(args, files, lint_host=environment_of(ops), cancel=cancel)
    except IndusError as exc:
        if not is_backend_error(exc):
            raise
        return _error(f"Nothing was changed: the terminal backend is unavailable ({exc}).")


PATCH_SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Edit a file by replacing old_string with new_string. Copy old_string from the file "
        "(without read_file's LINE| prefixes) with enough surrounding lines to be unique; small "
        "whitespace, indentation and quote differences are tolerated. It must match one place "
        "unless replace_all is true. The result shows a unified diff. Use this for edits; use "
        "write_file only for new files or complete rewrites."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "The file: absolute, ~/..., or relative to the working directory.",
            },
            "old_string": {"type": "string", "description": "The text to replace."},
            "new_string": {
                "type": "string",
                "description": "The replacement text (empty to delete old_string).",
            },
            "replace_all": {
                "type": "boolean",
                "description": "Replace every place old_string matches (default false).",
            },
        },
        "required": ["path", "old_string", "new_string"],
    },
}


def register_tools(registry: ToolRegistry) -> None:
    registry.register(
        TOOL_NAME,
        TOOLSET,
        PATCH_SCHEMA,
        patch,
        traits=ToolTraits(path_arg="path", writer=True, mutating=True),
    )
