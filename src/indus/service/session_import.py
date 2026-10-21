"""Bring sessions written by two other coding-agent CLIs into a profile's store (P24-28).

Both tools keep one JSON-lines file per conversation in the user's home:

- **Claude Code**: ``~/.claude/projects/<project>/<session>.jsonl``. Each line is a record whose
  ``type`` is ``user`` or ``assistant`` (other types — summaries, system lines — carry no turn);
  its ``message`` has a ``role`` and a ``content`` that is either text or a list of blocks:
  ``text``, ``tool_use`` (``id``, ``name``, ``input``), ``tool_result`` (``tool_use_id``,
  ``content`` as text or text blocks) and ``thinking``. Records also carry ``timestamp`` and
  ``cwd``.
- **Codex CLI**: ``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``. A ``session_meta`` record
  (``id``, ``cwd``, ``timestamp``) opens the file; turns are ``response_item`` records whose
  ``payload`` is a ``message`` (``role``, ``content`` blocks ``input_text`` / ``output_text``), a
  ``function_call`` (``name``, ``arguments`` as JSON text, ``call_id``) or a
  ``function_call_output`` (``call_id``, ``output``). Older files hold the same items unwrapped.

Our mapping (written here, not taken from either tool): person text → ``user``; assistant text →
``assistant``; a tool use / call → an ``assistant`` message carrying one ``tool_calls`` entry
(``{id, type: "function", function: {name, arguments}}``); its result / output → a ``tool``
message with that ``tool_call_id`` and ``tool_name``; thinking and reasoning are dropped, and so
are the context blocks Codex injects as user turns (``<environment_context>``,
``<user_instructions>``). The title is the first person line (80 characters); the session's
``cwd`` and start time come from the file.

``preview`` maps at most ``PREVIEW_RECORDS`` records and never writes; ``import_session`` writes
one new session per file (source ``import-<kind>``) with at most
``MAX_MESSAGES`` messages and records the file's SHA-256 under the ``import`` metadata namespace,
so a second import of the same bytes is refused with the existing session id. A file in which no
record is recognised is an unknown format and is refused; lines that do not parse are counted as
``skipped``. Only files under the two tools' session roots are accepted (``candidates``).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal, cast

from indus.foundation.ids import new_session_id
from indus.state import messages, session_meta, sessions, titles
from indus.state.registry import StoreHandle
from indus.state.writer import TRANSCRIPT

__all__ = [
    "KINDS",
    "MAX_CANDIDATES",
    "MAX_MESSAGES",
    "PREVIEW_RECORDS",
    "Candidate",
    "ImportRefused",
    "Imported",
    "Mapped",
    "SourceKind",
    "candidates",
    "import_session",
    "map_file",
    "preview",
    "resolve_candidate",
    "roots",
    "source_kind",
]

type SourceKind = Literal["claude-code", "codex"]
KINDS: Final[tuple[SourceKind, ...]] = ("claude-code", "codex")
PREVIEW_RECORDS: Final = 2_000
MAX_MESSAGES: Final = 20_000
MAX_CANDIDATES: Final = 200
TITLE_CHARS: Final = 80
IMPORT_NAMESPACE: Final = "import"
_HEAD_LINES: Final = 60
_INJECTED_PREFIXES: Final = ("<environment_context>", "<user_instructions>")


class ImportRefused(Exception):
    """The file cannot be previewed or imported; ``code`` says why."""

    def __init__(self, code: str, message: str, *, session_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.session_id = session_id


@dataclass(frozen=True)
class Candidate:
    kind: SourceKind
    path: Path
    title: str
    cwd: str | None
    modified: float
    size: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.kind,
            "path": str(self.path),
            "title": self.title,
            "cwd": self.cwd,
            "modified": self.modified,
            "size": self.size,
        }


@dataclass
class Mapped:
    """What a file maps to: our message rows plus what we learned about the conversation."""

    kind: SourceKind
    messages: list[dict[str, Any]] = field(default_factory=lambda: list[dict[str, Any]]())
    cwd: str | None = None
    started_at: float | None = None
    recognised: int = 0
    skipped: int = 0
    truncated: bool = False

    @property
    def title(self) -> str:
        for row in self.messages:
            if row["role"] == "user" and isinstance(row.get("content"), str):
                first = str(row["content"]).strip().splitlines()
                if first and first[0].strip():
                    return first[0].strip()[:TITLE_CHARS]
        return "Imported session"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.kind,
            "title": self.title,
            "cwd": self.cwd,
            "started_at": self.started_at,
            "messages": [_preview_row(row) for row in self.messages],
            "skipped": self.skipped,
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class Imported:
    session_id: str
    title: str
    message_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "title": self.title,
            "message_count": self.message_count,
        }


# -- where the files are ---------------------------------------------------------------------------


def roots(home_dir: Path) -> dict[SourceKind, Path]:
    return {
        "claude-code": home_dir / ".claude" / "projects",
        "codex": home_dir / ".codex" / "sessions",
    }


def _files(kind: SourceKind, root: Path) -> Iterator[Path]:
    if not root.is_dir():
        return
    pattern = "*/*.jsonl" if kind == "claude-code" else "**/rollout-*.jsonl"
    for path in root.glob(pattern):
        if path.is_file() and not path.is_symlink():
            yield path


def candidates(home_dir: Path, *, kind: SourceKind | None = None) -> list[Candidate]:
    """Session files of both tools (or one), newest first, at most ``MAX_CANDIDATES``."""
    found: list[tuple[float, SourceKind, Path, int]] = []
    for each, root in roots(home_dir).items():
        if kind is not None and each != kind:
            continue
        for path in _files(each, root):
            try:
                stat = path.stat()
            except OSError:
                continue
            found.append((stat.st_mtime, each, path, stat.st_size))
    found.sort(key=lambda item: item[0], reverse=True)
    out: list[Candidate] = []
    for modified, each, path, size in found[:MAX_CANDIDATES]:
        head = map_file(path, each, limit=_HEAD_LINES)
        out.append(Candidate(each, path, head.title, head.cwd, modified, size))
    return out


def source_kind(text: str | None) -> SourceKind:
    """``text`` as a source kind, or ``ImportRefused("unknown_source")``."""
    for kind in KINDS:
        if text == kind:
            return kind
    raise ImportRefused("unknown_source", "Only Claude Code and Codex sessions can be imported.")


def resolve_candidate(home_dir: Path, kind: str, raw_path: str) -> Path:
    """``raw_path`` when it is one of ``kind``'s session files under the home; else refused."""
    source = source_kind(kind)
    root = roots(home_dir)[source].resolve()
    try:
        path = Path(raw_path).resolve(strict=True)
    except (OSError, RuntimeError):
        raise ImportRefused("not_found", "That session file does not exist.") from None
    if not path.is_relative_to(root) or path.suffix != ".jsonl" or not path.is_file():
        raise ImportRefused("not_a_candidate", "Only the tools' own session files are accepted.")
    if source == "codex" and not path.name.startswith("rollout-"):
        raise ImportRefused("not_a_candidate", "Only the tools' own session files are accepted.")
    return path


# -- mapping ---------------------------------------------------------------------------------------


def _text_of(content: object) -> str:
    """Text from a plain string or a list of text-like blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in cast(list[object], content):
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                entry = cast(dict[str, object], block)
                if entry.get("type") in ("text", "input_text", "output_text") and isinstance(
                    entry.get("text"), str
                ):
                    parts.append(cast(str, entry["text"]))
        return "\n".join(parts)
    return ""


def _timestamp(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) / 1000 if value > 1e12 else float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _call(call_id: str, name: str, arguments: object) -> dict[str, Any]:
    text = arguments if isinstance(arguments, str) else json.dumps(arguments, sort_keys=True)
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": text}}
        ],
    }


class _Mapper:
    def __init__(self, kind: SourceKind) -> None:
        self.out = Mapped(kind)
        self.names: dict[str, str] = {}

    def note(self, record: Mapping[str, object]) -> None:
        stamp = _timestamp(record.get("timestamp"))
        if stamp is not None and self.out.started_at is None:
            self.out.started_at = stamp
        cwd = record.get("cwd")
        if isinstance(cwd, str) and cwd and self.out.cwd is None:
            self.out.cwd = cwd

    def add(self, row: dict[str, Any]) -> None:
        self.out.messages.append(row)

    def user_text(self, text: str) -> None:
        if text.strip() and not text.lstrip().startswith(_INJECTED_PREFIXES):
            self.add({"role": "user", "content": text})

    def assistant_text(self, text: str) -> None:
        if text.strip():
            self.add({"role": "assistant", "content": text})

    def tool_call(self, call_id: str, name: str, arguments: object) -> None:
        self.names[call_id] = name
        self.add(_call(call_id, name, arguments))

    def tool_result(self, call_id: str, content: object) -> None:
        name = self.names.get(call_id, "tool")
        self.add(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "tool_name": name,
                "content": _text_of(content),
            }
        )

    # -- Claude Code ------------------------------------------------------------------------------

    def claude(self, record: Mapping[str, object]) -> bool:
        kind = record.get("type")
        message = record.get("message")
        if kind not in ("user", "assistant") or not isinstance(message, dict):
            return False
        body = cast(dict[str, object], message)
        self.note(record)
        content = body.get("content")
        if isinstance(content, str):
            (self.user_text if kind == "user" else self.assistant_text)(content)
            return True
        if not isinstance(content, list):
            return True
        texts: list[str] = []
        for block in cast(list[object], content):
            if not isinstance(block, dict):
                continue
            entry = cast(dict[str, object], block)
            tag = entry.get("type")
            if tag == "text" and isinstance(entry.get("text"), str):
                texts.append(cast(str, entry["text"]))
            elif tag == "tool_use":
                self._flush(kind, texts)
                self.tool_call(
                    str(entry.get("id") or ""),
                    str(entry.get("name") or "tool"),
                    entry.get("input") or {},
                )
            elif tag == "tool_result":
                self._flush(kind, texts)
                self.tool_result(str(entry.get("tool_use_id") or ""), entry.get("content"))
        self._flush(kind, texts)
        return True

    def _flush(self, kind: object, texts: list[str]) -> None:
        if texts:
            joined = "\n".join(texts)
            (self.user_text if kind == "user" else self.assistant_text)(joined)
            texts.clear()

    # -- Codex ------------------------------------------------------------------------------------

    def codex(self, record: Mapping[str, object]) -> bool:
        kind = record.get("type")
        payload = record.get("payload")
        if kind == "session_meta" and isinstance(payload, dict):
            self.note(cast(dict[str, object], payload))
            self.note(record)
            return True
        item: Mapping[str, object]
        if kind == "response_item" and isinstance(payload, dict):
            item = cast(dict[str, object], payload)
            self.note(record)
        elif kind in ("message", "function_call", "function_call_output"):
            item = record
        else:
            return kind in ("event_msg", "turn_context", "compacted") or (
                kind is None and isinstance(record.get("instructions"), str)
            )
        tag = item.get("type")
        if tag == "message":
            text = _text_of(item.get("content"))
            role = item.get("role")
            if role == "user":
                self.user_text(text)
            elif role == "assistant":
                self.assistant_text(text)
            return True
        if tag == "function_call":
            self.tool_call(
                str(item.get("call_id") or ""),
                str(item.get("name") or "tool"),
                item.get("arguments") or "{}",
            )
            return True
        if tag == "function_call_output":
            output = item.get("output")
            if isinstance(output, dict):
                output = cast(dict[str, object], output).get("content", "")
            self.tool_result(str(item.get("call_id") or ""), output)
            return True
        return tag == "reasoning"


def map_file(path: Path, kind: SourceKind, *, limit: int | None = None) -> Mapped:
    """Map ``path`` read as ``kind``; ``limit`` caps the records read (the preview's cap)."""
    mapper = _Mapper(kind)
    read = 0
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ImportRefused("unreadable", f"The file could not be read ({exc.strerror}).") from None
    with handle:
        for line in handle:
            if not line.strip():
                continue
            if limit is not None and read >= limit:
                mapper.out.truncated = True
                break
            read += 1
            try:
                record: object = json.loads(line)
            except ValueError:
                mapper.out.skipped += 1
                continue
            if not isinstance(record, dict):
                mapper.out.skipped += 1
                continue
            entry = cast(dict[str, object], record)
            known = mapper.claude(entry) if kind == "claude-code" else mapper.codex(entry)
            if known:
                mapper.out.recognised += 1
            else:
                mapper.out.skipped += 1
            if len(mapper.out.messages) >= MAX_MESSAGES:
                mapper.out.truncated = True
                break
    return mapper.out


def _recognised(mapped: Mapped) -> Mapped:
    if mapped.recognised == 0:
        raise ImportRefused(
            "unknown_format",
            "This file is not a Claude Code or Codex session in a format we know.",
        )
    return mapped


def preview(path: Path, kind: SourceKind) -> Mapped:
    """The mapped messages of the first ``PREVIEW_RECORDS`` records; nothing is written."""
    return _recognised(map_file(path, kind, limit=PREVIEW_RECORDS))


def _preview_row(row: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"role": row["role"], "content": row.get("content", "")}
    calls = row.get("tool_calls")
    if isinstance(calls, list) and calls:
        first = cast(dict[str, Any], cast(list[object], calls)[0])
        out["tool"] = cast(dict[str, Any], first["function"])["name"]
    if row.get("tool_name"):
        out["tool"] = row["tool_name"]
    return out


# -- import ----------------------------------------------------------------------------------------


def _digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            sha.update(chunk)
    return sha.hexdigest()


def _already(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute(
        "SELECT session_id FROM session_meta WHERE namespace = ? AND key = ?",
        (IMPORT_NAMESPACE, key),
    ).fetchone()
    return None if row is None else str(row["session_id"])


def import_session(store: StoreHandle, path: Path, kind: SourceKind) -> Imported:
    """Write the file as one new session of ``store``'s profile (refused when already imported)."""
    mapped = _recognised(map_file(path, kind))
    if not mapped.messages:
        raise ImportRefused("empty", "The session has no messages to import.")
    key = f"sha256:{_digest(path)}"
    clock = store.scope.clock
    now = clock.now().timestamp()
    session_id = new_session_id(clock)
    origin = {"source": kind, "file": path.name, "skipped": mapped.skipped}
    rows: Sequence[dict[str, Any]] = mapped.messages

    def write(conn: sqlite3.Connection) -> str:
        existing = _already(conn, key)
        if existing is not None:
            return existing
        fields: dict[str, Any] = {"profile_id": store.scope.profile_id}
        if mapped.cwd:
            fields["cwd"] = mapped.cwd
        if mapped.started_at is not None:
            fields["started_at"] = mapped.started_at
        sessions.create_in(conn, session_id, f"import-{kind}", now=now, **fields)
        messages.append_batch_in(conn, session_id, rows, now=now)
        titles.set_title_in(
            conn, session_id, titles.next_lineage_title_in(conn, mapped.title), source="derived"
        )
        session_meta.put_in(conn, session_id, IMPORT_NAMESPACE, key, origin, now=now)
        return session_id

    written = store.write(write, budget=TRANSCRIPT)
    if written != session_id:
        raise ImportRefused(
            "already_imported", "This session was imported before.", session_id=written
        )
    return Imported(session_id, titles.get_title(store, session_id) or mapped.title, len(rows))
