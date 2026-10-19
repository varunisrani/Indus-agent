"""Output documents of cron runs, their retention, and bounded cleanup (P14-12).

Every run leaves ``<home>/cron/output/<job_id>/<YYYYmmddTHHMMSSZ>.md``: a short header (job, run
time in UTC, schedule, status) and length-framed sections — a frame line
``--- <name>: <n> chars ---``, then exactly ``n`` characters, then a newline — for the prompt, the
response, and (failures) the error and a redacted, chained traceback. Frames make the reader exact:
text that quotes a frame line can never be mistaken for a boundary (``read_document``).

Retention: the newest ``cron.output_retention`` documents of a job are kept (``<= 0`` keeps all).
Cleanup work after a run (pruning, agent teardown) goes through ``bounded`` /
``bounded_async``, which give up after ``cron.cleanup_timeout_seconds`` so a hung step can never
wedge the in-flight guard.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil
import threading
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from indus.cron.ports import job_output_dir
from indus.foundation.clock import Clock
from indus.foundation.fsio import atomic_write_text
from indus.foundation.home import secure_dir
from indus.foundation.scope import Scope
from indus.safety.redaction import redact_output

__all__ = [
    "MONITOR_STATE_FILE",
    "Document",
    "bounded",
    "bounded_async",
    "documents",
    "format_failure",
    "prune",
    "read_document",
    "remove_job_output",
    "write_document",
]

log = logging.getLogger(__name__)

SECTIONS = ("prompt", "response", "error", "traceback")
_FRAME = re.compile(r"^--- (?P<name>[a-z]+): (?P<n>\d+) chars ---\n", re.MULTILINE)
_HEADER_FIELD = re.compile(r"^- (?P<key>[a-z_]+): (?P<value>.*)$", re.MULTILINE)
MONITOR_STATE_FILE = "monitor_last_output.txt"
POLL_S = 0.05


@dataclass(frozen=True)
class Document:
    """A parsed output document."""

    path: Path
    status: str
    fields: dict[str, str] = field(default_factory=dict[str, str])
    sections: dict[str, str] = field(default_factory=dict[str, str])

    def section(self, name: str) -> str:
        return self.sections.get(name, "")


def _stamp(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _frame(name: str, body: str) -> str:
    return f"--- {name}: {len(body)} chars ---\n{body}\n"


def render_document(
    *,
    job_id: str,
    label: str,
    schedule: str,
    status: str,
    when: datetime,
    sections: dict[str, str],
) -> str:
    head = [
        f"# Scheduled job: {_one_line(label)}",
        "",
        f"- job: {job_id}",
        f"- run: {when.astimezone(UTC).isoformat()}",
        f"- schedule: {_one_line(schedule) or 'n/a'}",
        f"- status: {status}",
        "",
    ]
    body = "".join(
        _frame(name, sections[name].replace("\r\n", "\n").replace("\r", "\n"))
        for name in SECTIONS
        if name in sections
    )
    return "\n".join(head) + "\n" + body


def write_document(
    home: Path,
    *,
    job_id: str,
    label: str,
    schedule: str,
    status: str,
    when: datetime,
    sections: dict[str, str],
) -> Path:
    """Write one run's document and return its path (never overwrites an earlier one)."""
    folder = job_output_dir(home, job_id)
    secure_dir(folder)
    stem = _stamp(when)
    path = folder / f"{stem}.md"
    n = 2
    while path.exists():
        path = folder / f"{stem}_{n:02d}.md"
        n += 1
    text = render_document(
        job_id=job_id, label=label, schedule=schedule, status=status, when=when, sections=sections
    )
    atomic_write_text(path, text, mode=0o600)
    return path


def read_document(path: Path) -> Document | None:
    """Parse a document written by ``write_document``; None when it is not one."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    first = _FRAME.search(text)
    header = text if first is None else text[: first.start()]
    fields = {m.group("key"): m.group("value").strip() for m in _HEADER_FIELD.finditer(header)}
    status = fields.get("status")
    if status is None:
        return None
    sections: dict[str, str] = {}
    pos = first.start() if first is not None else len(text)
    while pos < len(text):
        frame = _FRAME.match(text, pos)
        if frame is None:
            break
        start = frame.end()
        end = start + int(frame.group("n"))
        if end > len(text) or text[end : end + 1] != "\n":
            log.debug("output document %s has a truncated %s section", path, frame.group("name"))
            break
        sections[frame.group("name")] = text[start:end]
        pos = end + 1
    return Document(path=path, status=status, fields=fields, sections=sections)


def documents(home: Path, job_id: str) -> list[Path]:
    """The job's documents, newest first."""
    folder = job_output_dir(home, job_id)
    try:
        found = [p for p in folder.iterdir() if p.suffix == ".md" and p.is_file()]
    except OSError:
        return []
    return sorted(found, key=lambda p: p.name, reverse=True)


def prune(home: Path, job_id: str, keep: int) -> int:
    """Delete all but the newest ``keep`` documents; ``keep <= 0`` keeps all. Returns removed."""
    if keep <= 0:
        return 0
    removed = 0
    for path in documents(home, job_id)[keep:]:
        try:
            path.unlink()
            removed += 1
        except OSError as exc:
            log.warning("could not remove old output %s: %s", path, exc)
    return removed


def remove_job_output(home: Path, job_id: str) -> None:
    """Delete a job's whole output folder (a job that removed itself)."""
    folder = job_output_dir(home, job_id)
    shutil.rmtree(folder, ignore_errors=True)


def format_failure(exc: BaseException, *, scope: Scope | None = None) -> str:
    """The chained traceback of ``exc``, secrets masked even when log redaction is off."""
    text = "".join(traceback.format_exception(exc))
    try:
        return redact_output(text, force=True, scope=scope)
    except Exception:
        log.warning("traceback redaction failed; the traceback is left out", exc_info=True)
        return f"{type(exc).__name__} (traceback withheld: redaction failed)"


# -- bounded cleanup -------------------------------------------------------------------------------


async def bounded(fn: Callable[[], object], *, seconds: float, clock: Clock, label: str) -> bool:
    """Run blocking ``fn`` on a daemon thread and wait at most ``seconds`` (``<= 0``: inline, no
    bound). True when it finished without raising; a step still running is abandoned."""
    if seconds <= 0:
        try:
            fn()
            return True
        except Exception:
            log.warning("cron cleanup %s failed", label, exc_info=True)
            return False
    done = threading.Event()
    failed: list[BaseException] = []

    def run() -> None:
        try:
            fn()
        except BaseException as exc:
            failed.append(exc)
        finally:
            done.set()

    threading.Thread(target=run, name=f"cron cleanup: {label}", daemon=True).start()
    deadline = clock.monotonic() + seconds
    while not done.is_set():
        if clock.monotonic() >= deadline:
            log.error("cron cleanup %s took longer than %.0f s; left running", label, seconds)
            return False
        await clock.sleep(POLL_S)
    if failed:
        log.warning("cron cleanup %s failed: %s", label, failed[0])
        return False
    return True


async def bounded_async(
    work: Awaitable[object], *, seconds: float, clock: Clock, label: str
) -> bool:
    """Await ``work`` for at most ``seconds`` on ``clock`` (``<= 0``: no bound); on time-out the
    work is cancelled and left. True when it finished without raising."""
    task = asyncio.ensure_future(work)
    if seconds <= 0:
        try:
            await task
            return True
        except Exception:
            log.warning("cron cleanup %s failed", label, exc_info=True)
            return False
    timer = asyncio.ensure_future(clock.sleep(seconds))
    try:
        await asyncio.wait({task, timer}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        timer.cancel()
    if not task.done():
        log.error("cron cleanup %s took longer than %.0f s; left", label, seconds)
        task.cancel()
        with contextlib.suppress(BaseException):
            await asyncio.wait({task}, timeout=0)
        return False
    if task.cancelled():
        return False
    error = task.exception()
    if error is not None:
        log.warning("cron cleanup %s failed: %s", label, error)
        return False
    return True
