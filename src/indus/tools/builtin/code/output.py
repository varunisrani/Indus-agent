# pyright: strict
"""What a script printed, turned into the text the model sees.

``ByteCapture`` collects one stream from a reader thread: the first ``keep`` bytes whole, then only
a rolling tail, plus the total count. ``present_stdout`` cuts the text to the inline budget by
bytes (first 40 %, last 60 %, one marker naming what was left out) and saves the whole text (at
most ``SPILL_MAX_BYTES``) to ``<home>/cache/exec/stdout-<digest>.txt`` so the model can page it
with ``read_file`` instead of running the script again. ``clean`` strips terminal escapes and
masks secrets; every piece of output passes through it before it is cut or saved.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from indus.backends.processes import strip_ansi
from indus.foundation.scope import Scope
from indus.safety.redaction import redact_output
from indus.tools.builtin.code.settings import (
    SPILL_MAX_BYTES,
    STDERR_BYTES,
    STDOUT_HEAD_SHARE,
    STDOUT_INLINE_BYTES,
)

log = logging.getLogger(__name__)

SPILL_DIR = ("cache", "exec")
# A command name that is neither an env dump nor a file reader: secrets and credential shapes
# are masked, ordinary ``name = value`` lines in printed source are left alone.
REDACT_AS = "python3 script.py"
STDERR_SEPARATOR = "\n--- stderr ---\n"


class ByteCapture:
    """One output stream: whole up to ``keep`` bytes, then a rolling tail (thread-safe)."""

    def __init__(self, keep: int, tail: int = STDOUT_INLINE_BYTES) -> None:
        self._keep = keep
        self._tail_max = tail
        self._head = bytearray()
        self._tail = bytearray()
        self.total = 0
        self._lock = threading.Lock()

    def append(self, data: bytes) -> None:
        if not data:
            return
        with self._lock:
            self.total += len(data)
            room = self._keep - len(self._head)
            if room > 0:
                self._head += data[:room]
                data = data[room:]
            if data:
                self._tail += data
                if len(self._tail) > self._tail_max:
                    del self._tail[: len(self._tail) - self._tail_max]

    @property
    def overflowed(self) -> bool:
        return self.total > self._keep

    def kept(self) -> bytes:
        """The whole stream when it fit, else the kept head."""
        with self._lock:
            return bytes(self._head)

    def last(self, count: int) -> bytes:
        """The last ``count`` bytes of the stream (as far as they were kept)."""
        with self._lock:
            joined = bytes(self._head) + bytes(self._tail) if self._tail else bytes(self._head)
        return joined[-count:] if count > 0 else b""

    def drain(self) -> tuple[bytes, int]:
        """Kept bytes and the total so far; then start empty (per kernel cell)."""
        with self._lock:
            data = bytes(self._head) + bytes(self._tail)
            total = self.total
            self._head.clear()
            self._tail.clear()
            self.total = 0
        return data, total


def decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def clean(text: str, scope: Scope | None) -> str:
    """Escapes stripped, secrets masked."""
    return redact_output(strip_ansi(text), command=REDACT_AS, scope=scope)


def cut_marker(omitted: int, total: int) -> str:
    return (
        f"\n\n[... {omitted:,} of {total:,} bytes of output left out here; "
        "the full text is in the file named in the result ...]\n\n"
    )


def head_tail(raw: bytes, budget: int) -> tuple[str, int]:
    """``raw`` cut to ``budget`` bytes around one marker; returns (text, bytes left out)."""
    if len(raw) <= budget:
        return decode(raw), 0
    head = int(budget * STDOUT_HEAD_SHARE)
    tail = budget - head
    omitted = len(raw) - budget
    return decode(raw[:head]) + cut_marker(omitted, len(raw)) + decode(raw[-tail:]), omitted


def spill(scope: Scope | None, text: str) -> str | None:
    """Save the full text owner-only under ``<home>/cache/exec``; None when it could not be."""
    if scope is None:
        return None
    raw = text.encode("utf-8", errors="replace")
    if len(raw) > SPILL_MAX_BYTES:
        raw = (
            raw[:SPILL_MAX_BYTES]
            + f"\n\n[... saved copy stops at {SPILL_MAX_BYTES:,} bytes ...]".encode()
        )
    try:
        folder = Path(scope.home).joinpath(*SPILL_DIR)
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        digest = hashlib.sha256(raw).hexdigest()[:16]
        path = folder / f"stdout-{digest}.txt"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
        return str(path)
    except OSError as exc:
        log.warning("could not save the full execute_code output: %s", exc)
        return None


@dataclass
class Presented:
    text: str
    fields: dict[str, Any] = field(default_factory=lambda: {})


def present_stdout(text: str, scope: Scope | None) -> Presented:
    """The inline stdout and the truncation fields (``stdout_*``, ``full_output_path``)."""
    raw = text.encode("utf-8", errors="replace")
    total = len(raw)
    if total <= STDOUT_INLINE_BYTES:
        return Presented(text)
    shown, omitted = head_tail(raw, STDOUT_INLINE_BYTES)
    fields: dict[str, Any] = {
        "stdout_truncated": True,
        "stdout_bytes_total": total,
        "stdout_bytes_omitted": omitted,
    }
    path = spill(scope, text)
    if path is not None:
        fields["full_output_path"] = path
        fields["note"] = (
            "The script ran; its printed output was longer than the inline limit, so the middle "
            f"is left out above. The whole output is in {path}: read it with "
            f'read_file(path="{path}", offset=...) rather than running the script again.'
        )
    else:
        fields["note"] = (
            "The script ran; its printed output was longer than the inline limit, so the middle "
            "is left out above. Print less (a summary or a slice) if you need that part."
        )
    return Presented(shown, fields)


def present_stderr(text: str) -> str:
    raw = text.encode("utf-8", errors="replace")
    shown, _ = head_tail(raw, STDERR_BYTES)
    return shown


def with_stderr(stdout: str, stderr: str) -> str:
    if not stderr.strip():
        return stdout
    return f"{stdout}{STDERR_SEPARATOR}{stderr}" if stdout else stderr
