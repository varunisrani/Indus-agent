"""Spilling oversized tool results to disk, and sweeping old spill files.

A result over its threshold (``budget.Budget.threshold``) is saved in full through a ``SpillSink``
and replaced in the context by a ``<spilled-result>`` block: the original size, where the file is,
how to page it with ``read_file`` (offset/limit) instead of calling the tool again, and a preview.
When no sink can save it, the preview is returned inline with a "could not be saved" note.

``enforce_turn_budget`` keeps a whole turn's tool results under ``per_turn`` by spilling the
largest not-yet-spilled results first; a multimodal result counts only its text parts and spills
only its largest text part (images stay).

The host sink writes ``<home>/cache/spill/<safe call id>.txt`` at 0600 and checks the byte count.
The redactor's force mode (P05) and remote-backend copies (P05/P11) plug in as other sinks.

Spill files are kept as long as the sessions that point at them: the sweep deletes a file only
when it is older than the session retention window (``sessions.retention_days``, at least 24 h)
and no stored message mentions it. An unknown reference set keeps everything (R02.16).
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import sqlite3
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from indus.foundation.fsio import atomic_write_bytes
from indus.foundation.home import secure_dir
from indus.safety.redaction import spill_redactor
from indus.tools.budget import DEFAULT_BUDGET, Budget

if TYPE_CHECKING:
    from indus.foundation.scope import Scope
    from indus.state.registry import StoreHandle

log = logging.getLogger(__name__)

SPILL_DIR = Path("cache") / "spill"
OPEN_TAG = "<spilled-result>"
CLOSE_TAG = "</spilled-result>"
TURN_BUDGET_TOOL = "(turn budget)"
FILE_MODE = 0o600
RETENTION_FLOOR_HOURS = 24.0
DEFAULT_RETENTION_DAYS = 90.0
REFERENCE_SCAN_LIMIT = 20_000

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")
_MAX_STEM = 100
_PATH_LINE = re.compile(r"^Saved in full at: (.+)$", re.MULTILINE)
_REFERENCED = re.compile(r"(?:^|[/\\])spill[/\\]+([A-Za-z0-9_.-]+\.txt)")


class SpillSink(Protocol):
    def save(self, name: str, text: str) -> str | None:
        """Store ``text`` under ``name``; return the path the model should read, or None."""
        ...


def spill_folder(home: Path) -> Path:
    return home / SPILL_DIR


class HostSpill:
    """Writes spill files under the profile home. ``redact`` (P05 force mode) runs first."""

    def __init__(
        self,
        home: Path,
        *,
        redact: Callable[[str], str] | None = None,
        after_save: Callable[[Path], None] | None = None,
    ) -> None:
        self.home = home
        self.folder = spill_folder(home)
        self._redact = redact
        self._after_save = after_save

    def save(self, name: str, text: str) -> str | None:
        body = self._redact(text) if self._redact is not None else text
        data = body.encode("utf-8", errors="replace")
        path = self.folder / name
        try:
            secure_dir(self.folder, home=self.home)
            atomic_write_bytes(path, data, mode=FILE_MODE)
            written = path.stat().st_size
        except OSError as exc:
            log.warning("spill file %s could not be written: %s", name, type(exc).__name__)
            return None
        if written != len(data):
            log.warning(
                "spill file %s came out %d bytes, not %d; dropped", name, written, len(data)
            )
            path.unlink(missing_ok=True)
            return None
        if self._after_save is not None:
            try:
                self._after_save(self.home)
            except Exception as exc:
                log.debug("after-spill hook failed: %s", type(exc).__name__)
        return str(path)


def host_spill_for(
    scope: Scope,
    referenced: Callable[[], set[str] | None],
    *,
    redact: Callable[[str], str] | None = None,
) -> HostSpill:
    """A host sink for ``scope`` whose first save sweeps the profile's old spill files once per
    process, with the scope's retention window and clock. Text is force-redacted first
    (``safety.redaction.spill_redactor``) unless the caller passes its own ``redact``."""

    def sweep_after_save(home: Path) -> None:
        sweep_once(
            home,
            retention_h=retention_hours_for(scope),
            now=scope.clock.now().timestamp(),
            referenced=referenced,
        )

    if redact is None:
        redact = spill_redactor(scope)
    return HostSpill(scope.home, redact=redact, after_save=sweep_after_save)


# -- the spilled block --------------------------------------------------------------------------


def spill_file_name(call_id: str | None) -> str:
    """One safe file name per call id; ids that needed changes get a hash suffix."""
    raw = call_id or "result"
    stem = _UNSAFE_NAME.sub("_", raw).strip("._-")
    if stem != raw or len(stem) > _MAX_STEM:
        digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:12]
        stem = (stem[:_MAX_STEM].rstrip("._-") or "result") + "_" + digest
    return f"{stem}.txt"


def preview(text: str, limit: int) -> tuple[str, bool]:
    """The first ``limit`` characters, ending at a newline when one lies past the midpoint."""
    if len(text) <= limit:
        return text, False
    cut = text.rfind("\n", 0, limit)
    return (text[: cut + 1] if cut > limit // 2 else text[:limit]), True


def human_size(chars: int) -> str:
    kib = chars / 1024
    return f"{kib / 1024:.1f} MB" if kib >= 1024 else f"{kib:.1f} KB"


def spilled_block(shown: str, more: bool, total: int, where: str) -> str:
    tail = "\n[... the rest is in the file]" if more else ""
    return (
        f"{OPEN_TAG}\n"
        f"Output too large to show in full: {total:,} characters ({human_size(total)}).\n"
        f"Saved in full at: {where}\n"
        "Read it in pieces with read_file (offset and limit) or process it with code; do not "
        "run the original tool again for the same data. The file is kept while this session "
        "refers to it; if it is gone, check the current state first, because the original call "
        "may already have had its effect.\n\n"
        f"First {len(shown):,} characters:\n{shown}{tail}\n"
        f"{CLOSE_TAG}"
    )


def unsaved_note(shown: str, total: int) -> str:
    return (
        f"{shown}\n\n[Output cut: the tool returned {total:,} characters and the full text "
        "could not be saved anywhere.]"
    )


def spilled_path(text: str) -> str | None:
    """The file path inside a spilled block, or None."""
    if OPEN_TAG not in text:
        return None
    found = _PATH_LINE.search(text)
    return found.group(1).strip() if found else None


def spill_if_needed(
    text: str,
    tool_name: str,
    call_id: str | None,
    *,
    budget: Budget = DEFAULT_BUDGET,
    sink: SpillSink | None,
    threshold: float | None = None,
    registered_cap: float | None = None,
) -> str:
    """``text`` unchanged when it fits; otherwise a spilled block (or the inline fallback)."""
    limit = budget.threshold(tool_name, registered_cap) if threshold is None else threshold
    if math.isinf(limit) or len(text) <= limit:
        return text
    shown, more = preview(text, budget.preview)
    where = sink.save(spill_file_name(call_id), text) if sink is not None else None
    if where is None:
        log.info(
            "tool %s result of %d chars cut inline: nothing could save it", tool_name, len(text)
        )
        return unsaved_note(shown, len(text))
    log.info("tool %s result of %d chars spilled to %s", tool_name, len(text), where)
    return spilled_block(shown, more, len(text), where)


# -- turn budget --------------------------------------------------------------------------------


def _text_parts(parts: Sequence[Any]) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for i, part in enumerate(parts):
        if isinstance(part, Mapping):
            p = cast(Mapping[str, Any], part)
            if p.get("type") == "text" and isinstance(p.get("text"), str):
                found.append((i, cast(str, p["text"])))
    return found


def text_size(content: object) -> int:
    """Characters of text a tool message carries (image parts do not count)."""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(t) for _, t in _text_parts(cast(list[Any], content)))
    return 0


def _spill_largest_part(
    parts: list[Any], call_id: str, budget: Budget, sink: SpillSink | None
) -> tuple[list[Any], int]:
    candidates = [(i, t) for i, t in _text_parts(parts) if OPEN_TAG not in t]
    if not candidates:
        return parts, 0
    index, text = max(candidates, key=lambda it: len(it[1]))
    replaced = spill_if_needed(
        text, TURN_BUDGET_TOOL, f"{call_id}_part{index}", budget=budget, sink=sink, threshold=0
    )
    if replaced == text:
        return parts, 0
    updated = list(parts)
    updated[index] = {**cast(Mapping[str, Any], parts[index]), "text": replaced}
    return updated, len(text) - len(replaced)


def enforce_turn_budget(
    messages: Iterable[Mapping[str, Any]],
    *,
    budget: Budget = DEFAULT_BUDGET,
    sink: SpillSink | None,
) -> list[dict[str, Any]]:
    """New message dicts whose total text fits ``budget.per_turn`` (largest spilled first)."""
    out = [dict(m) for m in messages]
    sizes = [text_size(m.get("content")) for m in out]
    total = sum(sizes)
    if total <= budget.per_turn:
        return out
    order = sorted(
        (
            i
            for i, m in enumerate(out)
            if isinstance(m.get("content"), list)
            or (isinstance(m.get("content"), str) and OPEN_TAG not in m["content"])
        ),
        key=lambda i: sizes[i],
        reverse=True,
    )
    for i in order:
        if total <= budget.per_turn:
            break
        content = out[i].get("content")
        call_id = str(out[i].get("tool_call_id") or f"turn_{i}")
        if isinstance(content, list):
            parts, saved = _spill_largest_part(cast(list[Any], content), call_id, budget, sink)
            if saved:
                out[i]["content"] = parts
                total -= saved
            continue
        text = cast(str, content)
        replaced = spill_if_needed(
            text, TURN_BUDGET_TOOL, call_id, budget=budget, sink=sink, threshold=0
        )
        if replaced != text:
            out[i]["content"] = replaced
            total += len(replaced) - len(text)
    return out


# -- retention ----------------------------------------------------------------------------------


def retention_hours(days: object) -> float:
    """Spill retention in hours from ``sessions.retention_days``; at least 24 h."""
    try:
        value = float(cast(Any, days)) if days is not None else DEFAULT_RETENTION_DAYS
    except (TypeError, ValueError):
        value = DEFAULT_RETENTION_DAYS
    if not math.isfinite(value):
        value = DEFAULT_RETENTION_DAYS
    return max(RETENTION_FLOOR_HOURS, value * 24.0)


def retention_hours_for(scope: Scope) -> float:
    days: object = None
    if scope.config is not None:
        try:
            days = scope.config.get("sessions.retention_days")
        except Exception:
            days = None
    return retention_hours(days)


def names_in_texts(texts: Iterable[object]) -> set[str]:
    """Spill file names mentioned anywhere in ``texts``."""
    found: set[str] = set()
    for text in texts:
        if isinstance(text, str) and "spill" in text:
            found.update(_REFERENCED.findall(text))
    return found


def store_references(store: StoreHandle) -> set[str] | None:
    """Spill names mentioned by stored messages; None when that cannot be known for sure."""
    query = (
        "SELECT content, api_content FROM messages "
        "WHERE instr(content, 'spill') > 0 OR instr(api_content, 'spill') > 0 LIMIT ?"
    )
    try:
        with store.read() as conn:
            rows = conn.execute(query, (REFERENCE_SCAN_LIMIT + 1,)).fetchall()
    except (sqlite3.Error, OSError) as exc:
        log.debug("spill references unreadable: %s", type(exc).__name__)
        return None
    if len(rows) > REFERENCE_SCAN_LIMIT:
        return None
    return names_in_texts(value for row in rows for value in row)


def sweep(
    home: Path,
    *,
    retention_h: float,
    now: float,
    referenced: Callable[[], set[str] | None],
) -> int:
    """Delete spill files older than ``retention_h`` that nothing references; return the count.

    ``now`` is wall-clock epoch seconds (take it from the scope's clock).
    """
    folder = spill_folder(home)
    cutoff = now - retention_h * 3600.0
    try:
        files = [p for p in folder.iterdir() if p.is_file()]
    except OSError:
        return 0
    old: list[Path] = []
    for path in files:
        try:
            if path.stat().st_mtime < cutoff:
                old.append(path)
        except OSError:
            continue
    if not old:
        return 0
    keep = referenced()
    if keep is None:
        log.debug("spill sweep skipped: references unknown, nothing deleted")
        return 0
    removed = 0
    for path in old:
        if path.name in keep:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


_swept: set[str] = set()
_swept_lock = threading.Lock()


def sweep_once(
    home: Path,
    *,
    retention_h: float,
    now: float,
    referenced: Callable[[], set[str] | None],
) -> int | None:
    """``sweep`` at most once per home per process; None when this home was already swept."""
    key = str(home)
    with _swept_lock:
        if key in _swept:
            return None
        _swept.add(key)
    try:
        return sweep(home, retention_h=retention_h, now=now, referenced=referenced)
    except Exception as exc:
        log.debug("spill sweep failed: %s", type(exc).__name__)
        return 0


def reset_sweep_memory() -> None:
    """Tests only: forget which homes were swept."""
    with _swept_lock:
        _swept.clear()
