# pyright: strict
"""``read_file``: a text file page by page, behind the read guards and the loop breaker (P05-16).

Guard order (the first that fires answers):

1. Windows NT/device namespace prefix on the raw path text.
2. Device and ``/proc`` leak files, on the literal path, every symlink hop and the real path.
3. FIFO, socket or device file (host only): a read would block, so nothing is opened.
4. Indus secret stores and control files; project secret env files (``.env``, ``.envrc``).
5. Documents (notebook, PDF, office, OpenDocument, RTF, EPUB, SQLite) → extracted text (P05-17).
6. Other binary extensions → refused, pointing at the vision tool.
7. A path found missing in the last 60 s (and still missing) → the remembered answer.
8. The loop breaker: a repeat of a region served in full this generation, with the file
   unchanged, gets a short stub; the next repeat is refused (R02.15).
9. The read: files over ``MAX_FILE_BYTES`` refused, content sniffed for binary, UTF-16 decoded
   with a note, lines numbered ``N|text`` and cut at ``MAX_LINE_CHARS``, the page cut to the
   char budget on a line boundary with ``next_offset``, merge-conflict blocks counted, secrets
   masked.

Stubs and refusals of the loop breaker carry ``harness_refusal: true`` and are not error results,
so the failure classifiers do not escalate them. Identical reads in a row warn at 3 and are
refused at 4. A full unredacted view of the current bytes records the write baseline P05-18 needs.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import posixpath
import shlex
import stat as stat_mod
from collections.abc import Mapping
from typing import Any

from indus.foundation.errors import IndusError
from indus.foundation.scope import Scope
from indus.safety import path_guards
from indus.safety.redaction import redact_output
from indus.tools.builtin import extract, paths, read_tracking
from indus.tools.builtin.file_ops import FileOpError, FileOps, is_backend_error, ops_for_call
from indus.tools.calls import ToolResult
from indus.tools.registry import ToolRegistry, ToolTraits

__all__ = [
    "DEFAULT_LIMIT",
    "DEFAULT_MAX_CHARS",
    "MAX_FILE_BYTES",
    "READ_FILE_SCHEMA",
    "STUB_MESSAGE",
    "format_lines",
    "read_file",
    "register_tools",
]

TOOL_NAME = "read_file"
TOOLSET = "file"
DEFAULT_LIMIT = 2_000
MAX_LIMIT = 2_000
MAX_LINE_CHARS = 2_000
DEFAULT_MAX_CHARS = 100_000
MAX_FILE_BYTES = 10 * 1024 * 1024
LARGE_FILE_BYTES = 512_000
WIDE_READ_LINES = 200
SUGGESTIONS = 5
MAX_CHARS_KEY = "file_read_max_chars"
LINE_CUT = " … [line cut]"
STUB_MESSAGE = (
    "This file region has not changed since you last read it; the earlier read_file result in "
    "this conversation is still current. Use it instead of reading again."
)


def _int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def _page_args(args: Mapping[str, Any]) -> tuple[int, int]:
    offset = max(1, _int(args.get("offset"), 1))
    limit = _int(args.get("limit"), DEFAULT_LIMIT)
    limit = DEFAULT_LIMIT if limit < 1 else min(limit, MAX_LIMIT)
    return offset, limit


def _max_chars(scope: Scope | None) -> int:
    value = scope.config.get(MAX_CHARS_KEY) if scope is not None and scope.config else None
    if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
        return int(value)
    return DEFAULT_MAX_CHARS


def _error(message: str, **extra: Any) -> str:
    return ToolResult.error(message, **extra).text


def _dumps(body: Mapping[str, Any]) -> str:
    return json.dumps(body, ensure_ascii=False)


def _refusal(path: str, message: str, **extra: Any) -> str:
    return _dumps(
        {"status": "blocked", "harness_refusal": True, "path": path, "message": message, **extra}
    )


# -- decoding --------------------------------------------------------------------------------------


def _utf16_kind(data: bytes) -> str | None:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    sample = data[:4096]
    if len(sample) < 4 or len(sample) % 2:
        return None
    pairs = len(sample) // 2
    even_zero = sum(1 for i in range(0, len(sample), 2) if sample[i] == 0)
    odd_zero = sum(1 for i in range(1, len(sample), 2) if sample[i] == 0)
    if odd_zero / pairs > 0.4 and even_zero / pairs < 0.05:
        return "utf-16-le"
    if even_zero / pairs > 0.4 and odd_zero / pairs < 0.05:
        return "utf-16-be"
    return None


def _looks_binary(data: bytes) -> bool:
    sample = data[:8192]
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    controls = sum(1 for b in sample if b < 32 and b not in (9, 10, 12, 13, 27))
    return controls / len(sample) > 0.3


def decode_text(data: bytes) -> tuple[str, str | None]:
    """The text of ``data`` and an encoding note when it was not plain UTF-8."""
    kind = _utf16_kind(data)
    if kind is not None:
        return data.decode(kind, errors="replace").lstrip("﻿"), kind
    return data.decode("utf-8", errors="replace").lstrip("﻿"), None


def split_lines(text: str) -> list[str]:
    """Lines as ``cat -n`` counts them: a final newline ends the last line, CR of CRLF dropped."""
    if not text:
        return []
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def format_lines(lines: list[str], first: int) -> tuple[str, bool]:
    """``N|text`` lines and whether any line was cut at ``MAX_LINE_CHARS``."""
    cut = False
    out: list[str] = []
    for number, line in enumerate(lines, start=first):
        if len(line) > MAX_LINE_CHARS:
            line = line[:MAX_LINE_CHARS] + LINE_CUT
            cut = True
        out.append(f"{number}|{line}")
    return "\n".join(out), cut


def _fit_budget(numbered: list[str], max_chars: int) -> tuple[list[str], bool]:
    """The leading lines that fit in ``max_chars`` (at least one, cut if it alone is too long)."""
    kept: list[str] = []
    used = 0
    for line in numbered:
        extra = len(line) + (1 if kept else 0)
        if used + extra > max_chars:
            break
        kept.append(line)
        used += extra
    if not kept and numbered:
        return [numbered[0][:max_chars]], True
    return kept, len(kept) < len(numbered)


def _conflict_blocks(lines: list[str]) -> int:
    opens = sum(1 for line in lines if line.startswith("<<<<<<< ") or line == "<<<<<<<")
    closes = sum(1 for line in lines if line.startswith(">>>>>>> ") or line == ">>>>>>>")
    return min(opens, closes)


# -- the guards ----------------------------------------------------------------------------------


def _special_kind(path: str) -> str | None:
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return None
    if stat_mod.S_ISREG(mode) or stat_mod.S_ISDIR(mode):
        return None
    if stat_mod.S_ISFIFO(mode):
        return "a FIFO (named pipe)"
    if stat_mod.S_ISSOCK(mode):
        return "a socket"
    if stat_mod.S_ISCHR(mode) or stat_mod.S_ISBLK(mode):
        return "a device file"
    return "not a regular file"


async def _suggestions(ops: FileOps, resolved: str) -> list[str]:
    directory, name = posixpath.split(resolved)
    names = await ops.list_dir(directory or "/")
    if not names:
        return []
    lowered = {n.lower(): n for n in names}
    picks: list[str] = []
    same_case = lowered.get(name.lower())
    if same_case is not None and same_case != name:
        picks.append(same_case)
    picks += difflib.get_close_matches(name, names, n=SUGGESTIONS, cutoff=0.6)
    unique = list(dict.fromkeys(picks))[:SUGGESTIONS]
    return [posixpath.join(directory, n) for n in unique]


# -- the tool --------------------------------------------------------------------------------------


async def read_file(
    args: Mapping[str, Any],
    *,
    scope: Scope | None = None,
    task_id: str | None = None,
    session_id: str | None = None,
    environments: Any = None,
    cancel: Any = None,
) -> str:
    raw = args.get("path")
    if not isinstance(raw, str) or not raw.strip():
        return _error("path is required: the file to read, absolute or relative to the cwd")
    offset, limit = _page_args(args)
    refused = path_guards.nt_namespace_error(raw, verb="Read")
    if refused:
        return _error(refused)
    try:
        ops = await ops_for_call(
            scope,
            task_id=task_id,
            session_id=session_id,
            environments=environments,
            cancel=cancel,
        )
    except IndusError as exc:
        if not is_backend_error(exc):
            raise
        return _error(f"The file could not be read: the terminal backend is unavailable ({exc}).")
    base = paths.base_dir(scope, ops.cwd, host=ops.host)
    literal = paths.expand_tilde(raw.strip(), scope)
    if not posixpath.isabs(literal):
        literal = posixpath.join(base, literal)
    if path_guards.blocked_device(literal, follow_links=ops.host):
        return _error(
            f"Cannot read {raw!r}: it is a device or process file that never ends, blocks, or "
            "exposes another process's secrets."
        )
    resolved = paths.resolve(raw, base=base, scope=scope, host=ops.host)
    if ops.host:
        kind = _special_kind(resolved)
        if kind is not None:
            return _error(
                f"Cannot read {raw!r}: it is {kind}, so a read would block; nothing was opened. "
                "Use the terminal if you need to talk to it."
            )
    denied = path_guards.read_denial(resolved, scope)
    if denied:
        return _error(f"Access refused: {denied}")
    tracker = read_tracking.tracker_for(scope, task_id)
    try:
        return await _read(ops, raw, resolved, offset, limit, scope, tracker)
    except IndusError as exc:
        if not is_backend_error(exc):
            raise
        return _error(f"The file could not be read: the terminal backend is unavailable ({exc}).")


async def _read(
    ops: FileOps,
    raw: str,
    resolved: str,
    offset: int,
    limit: int,
    scope: Scope | None,
    tracker: read_tracking.TaskReads,
) -> str:
    if extract.is_document(resolved):
        answer = await _read_document(ops, raw, resolved, offset, limit, scope, tracker)
        if answer is not None:
            return answer
    if path_guards.has_binary_extension(resolved):
        suffix = posixpath.splitext(resolved)[1]
        return _error(
            f"Cannot read the binary file {raw!r} ({suffix}) as text. Use the vision tool for "
            "images, or the terminal to inspect other binary files."
        )
    now = scope.clock.monotonic() if scope is not None else 0.0
    remembered = tracker.cached_missing(resolved, now)
    if remembered is not None:
        if (await ops.stat(resolved)).kind == "missing":
            return remembered
        tracker.forget_missing(resolved)
    info = await ops.stat(resolved)
    if info.kind == "file" and info.size > MAX_FILE_BYTES:
        return _error(
            f"{raw!r} is {info.size / 1048576:.1f} MB; read_file reads files up to "
            f"{MAX_FILE_BYTES // 1048576} MB. Use the terminal (head, sed -n, grep) for a part."
        )
    try:
        data = await ops.read_bytes(resolved)
    except FileOpError as exc:
        return await _read_failed(ops, raw, resolved, exc, tracker, now)
    if len(data) > MAX_FILE_BYTES:
        return _error(f"{raw!r} is larger than {MAX_FILE_BYTES // 1048576} MB; read a part.")
    version = hashlib.sha256(data).hexdigest()
    key: read_tracking.ReadKey = (resolved, offset, limit)
    verdict, asked = tracker.dedup_verdict(key, version)
    if verdict == "block":
        return _refusal(
            raw,
            f"You have asked for this exact region {asked} times and the file has not changed. "
            "Stop reading it: the content from your earlier read_file result is current. Go on "
            "with the task.",
            already_read=asked,
        )
    if verdict == "stub":
        return _dumps(
            {"status": "unchanged", "path": raw, "message": STUB_MESSAGE, "content_returned": False}
        )
    if _looks_binary(data) and _utf16_kind(data) is None:
        return _error(
            f"{raw!r} looks like a binary file (it contains NUL or control bytes). Use the "
            "vision tool for images or the terminal (xxd, file) to inspect it."
        )
    text, encoding = decode_text(data)
    return _page(
        raw,
        resolved,
        split_lines(text),
        len(data),
        offset,
        limit,
        scope,
        tracker,
        version=version,
        encoding=encoding,
        extracted=False,
    )


async def _read_failed(
    ops: FileOps,
    raw: str,
    resolved: str,
    exc: FileOpError,
    tracker: read_tracking.TaskReads,
    now: float,
) -> str:
    if exc.kind == "missing":
        similar = await _suggestions(ops, resolved)
        extra: dict[str, Any] = {"similar_files": similar} if similar else {}
        answer = _error(
            f"File not found: {raw} (resolved to {resolved}). To create it, use write_file.",
            **extra,
        )
        tracker.remember_missing(resolved, now, answer)
        return answer
    if exc.kind == "is_dir":
        return _error(f"{raw!r} is a directory; use search_files to list what is in it.")
    if exc.kind == "not_regular":
        return _error(f"{raw!r} is not a regular file; nothing was read.")
    return _error(f"The file could not be read: {exc.detail or exc}")


async def _read_document(
    ops: FileOps,
    raw: str,
    resolved: str,
    offset: int,
    limit: int,
    scope: Scope | None,
    tracker: read_tracking.TaskReads,
) -> str | None:
    """The page of an extracted document, an error for a failed binary document, or None to
    fall through to the plain read (a notebook that is not valid JSON)."""
    suffix = posixpath.splitext(resolved)[1].lower()
    info = await ops.stat(resolved)
    if info.kind != "file":
        return None
    if info.size > extract.MAX_DOCUMENT_BYTES:
        return _error(
            f"{raw!r} is {info.size / 1048576:.1f} MB; documents over "
            f"{extract.MAX_DOCUMENT_BYTES // 1048576} MB are not extracted."
        )
    try:
        data = await ops.read_bytes(resolved)
        text = extract.extract(data, resolved, clock=scope.clock if scope is not None else None)
    except FileOpError:
        return None
    except extract.MissingExtra as exc:
        return _error(f"Cannot read {raw!r}: {exc}")
    except extract.ExtractionError as exc:
        if suffix == ".ipynb":
            return None
        return _error(
            f"Cannot read {raw!r} ({suffix}): {exc}. Use the terminal to inspect or convert it."
        )
    version = hashlib.sha256(data).hexdigest()
    return _page(
        raw,
        resolved,
        split_lines(text),
        len(data),
        offset,
        limit,
        scope,
        tracker,
        version=version,
        encoding=None,
        extracted=True,
    )


def _page(
    raw: str,
    resolved: str,
    lines: list[str],
    file_size: int,
    offset: int,
    limit: int,
    scope: Scope | None,
    tracker: read_tracking.TaskReads,
    *,
    version: str,
    encoding: str | None,
    extracted: bool,
) -> str:
    total = len(lines)
    if total and offset > total:
        return _error(f"offset {offset} is past the end of {raw!r}, which has {total} lines.")
    window = lines[offset - 1 : offset - 1 + limit]
    numbered, line_cut = format_lines(window, offset)
    numbered_lines = numbered.split("\n") if window else []
    kept, budget_cut = _fit_budget(numbered_lines, _max_chars(scope))
    shown = len(kept)
    last = offset + shown - 1
    content = "\n".join(kept)
    masked = redact_output(content, command=f"cat {shlex.quote(resolved)}", scope=scope)
    redacted = masked != content
    truncated = budget_cut or last < total
    body: dict[str, Any] = {
        "content": masked,
        "total_lines": total,
        "file_size": file_size,
        "truncated": truncated,
    }
    if truncated:
        body["next_offset"] = last + 1
    if budget_cut:
        body["truncated_by"] = "chars"
        body["hint"] = (
            f"The page stopped at the {_max_chars(scope):,}-character read budget after line "
            f"{last} of {total}. Continue with offset={last + 1}."
        )
    elif truncated:
        body["hint"] = f"Showing lines {offset}-{last} of {total}. Continue with offset={last + 1}."
    if extracted:
        body["extracted_document"] = True
    if encoding is not None:
        body["encoding"] = f"{encoding} (shown here as UTF-8 text)"
    conflicts = _conflict_blocks(window[:shown])
    if conflicts:
        body["conflict_blocks"] = conflicts
        body["hint"] = (
            f"{conflicts} unresolved merge-conflict block(s) in these lines; resolve them before "
            "editing around them."
        )
    if file_size > LARGE_FILE_BYTES and limit > WIDE_READ_LINES and truncated:
        body.setdefault(
            "hint",
            f"This file is large ({file_size:,} bytes); read the section you need with offset "
            "and limit.",
        )
    whole = offset == 1 and not truncated and not (extracted and not resolved.endswith(".ipynb"))
    count = tracker.record_read(
        (resolved, offset, limit),
        version,
        first_line=offset,
        last_line=max(last, offset),
        total_lines=total,
        whole=whole,
        redacted=redacted or line_cut or budget_cut,
    )
    if count >= read_tracking.BLOCK_AT:
        return _refusal(
            raw,
            f"You have read this exact region {count} times in a row. You already have its "
            "content; stop re-reading and go on with the task.",
            already_read=count,
        )
    if count >= read_tracking.WARN_AT:
        body["warning"] = (
            f"You have read this exact region {count} times in a row. Use what you already have; "
            "if you are stuck, stop reading and act."
        )
    return _dumps(body)


READ_FILE_SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Read a text file with line numbers (each line is shown as LINE|CONTENT), a page at a "
        f"time: offset is the first line (from 1), limit the number of lines (up to {MAX_LIMIT}). "
        "When the result says truncated, continue with next_offset. Notebooks, PDF, Word, Excel, "
        "PowerPoint, OpenDocument, RTF, EPUB and SQLite files are shown as extracted text. "
        "Prefer this over cat/head/tail in the terminal. Images and other binary files are "
        "refused; secret files and device files cannot be read."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "The file: absolute, ~/..., or relative to the working directory.",
            },
            "offset": {
                "type": "integer",
                "description": "First line to show, counting from 1 (default 1).",
            },
            "limit": {
                "type": "integer",
                "description": f"How many lines to show (default and maximum {MAX_LIMIT}).",
            },
        },
        "required": ["path"],
    },
}


def register_tools(registry: ToolRegistry) -> None:
    registry.register(
        TOOL_NAME,
        TOOLSET,
        READ_FILE_SCHEMA,
        read_file,
        max_result_chars=float("inf"),
        traits=ToolTraits(read_only=True, path_arg="path"),
    )
