# pyright: strict
"""Retained receipts of finished background jobs (P11-13).

When a job ends, the registry writes ``<home>/logs/process-results/<proc_id>.json`` before any
completion event goes out: ``id, command, exit_code, completion_reason, termination_source,
started_at, exited_at, tail, owner_session, cwd``. The command and the tail (the last
``TAIL_CHARS`` characters) are redacted in force mode — a receipt is durable, so it is masked
even when live redaction is off. Atomic write, file 0600, dir 0700. Every write prunes the dir:
receipts that ended more than ``MAX_AGE_S`` ago, then all but the newest ``MAX_KEPT``.

Reading (``find``) is for the owning conversation only: the caller's session must be the
receipt's ``owner_session`` or a compression continuation of it, which the host decides through
``set_lineage_resolver`` (without one, only the owner itself). Knowing an id is not authority.
A receipt is a read-only snapshot: nothing is re-run and no completion is replayed. A failed
write logs a warning and never blocks the completion.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from indus.foundation.fsio import atomic_write_json
from indus.foundation.layout import profile_home_problem
from indus.safety.redaction import redact_output

if TYPE_CHECKING:
    from indus.backends.processes import ProcessRecord

__all__ = [
    "MAX_AGE_S",
    "MAX_KEPT",
    "TAIL_CHARS",
    "Receipt",
    "find",
    "owned_by",
    "prune",
    "receipt_dir",
    "set_lineage_resolver",
    "write_receipt",
]

log = logging.getLogger(__name__)

MAX_KEPT = 64
MAX_AGE_S = 7 * 24 * 3600
TAIL_CHARS = 200_000
MIN_PREFIX_HEX = 4
_ID = re.compile(r"^proc_[0-9a-f]{4,32}$")

type LineageResolver = Callable[[str, str], bool]
"""(owner session, caller session) → is the caller a continuation of the owner?"""

_resolver_lock = threading.Lock()
_resolver: list[LineageResolver] = []


def set_lineage_resolver(resolver: LineageResolver | None) -> None:
    """Install (or remove) the host's compression-lineage check."""
    with _resolver_lock:
        _resolver.clear()
        if resolver is not None:
            _resolver.append(resolver)


@dataclass(frozen=True, slots=True)
class Receipt:
    id: str
    command: str
    exit_code: int | None
    completion_reason: str
    termination_source: str
    started_at: str
    exited_at: str
    tail: str
    total_chars: int
    owner_session: str | None
    cwd: str


def receipt_dir(home: Path) -> Path:
    return home / "logs" / "process-results"


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def write_receipt(record: ProcessRecord, *, now: datetime) -> Path | None:
    """Write the receipt of an exited job (see the module notes); the path, or None."""
    scope = record.scope
    tail = record.buffer[-TAIL_CHARS:]
    receipt = Receipt(
        id=record.id,
        command=redact_output(record.command, force=True, scope=scope),
        exit_code=record.exit_code,
        completion_reason=record.reason,
        termination_source=record.source,
        started_at=_iso(record.started_at),
        exited_at=_iso(now),
        tail=redact_output(tail, command=record.command, force=True, scope=scope),
        total_chars=record.total_chars,
        owner_session=record.session_id,
        cwd=record.cwd,
    )
    directory = receipt_dir(scope.home)
    problem = profile_home_problem(scope.home)
    if problem is not None:  # never bring a deleted profile's home back
        log.info("result of %s not kept: %s", record.id, problem)
        return None
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = atomic_write_json(directory / f"{record.id}.json", asdict(receipt), mode=0o600)
    except OSError:
        log.warning("could not keep the result of %s", record.id, exc_info=True)
        return None
    try:
        prune(scope.home, now=now)
    except OSError:
        log.debug("pruning process receipts failed", exc_info=True)
    return path


def _read(path: Path) -> Receipt | None:
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    data = cast(dict[str, Any], raw)
    try:
        receipt = Receipt(
            id=str(data["id"]),
            command=str(data["command"]),
            exit_code=None if data.get("exit_code") is None else int(data["exit_code"]),
            completion_reason=str(data.get("completion_reason") or "exited"),
            termination_source=str(data.get("termination_source") or ""),
            started_at=str(data["started_at"]),
            exited_at=str(data["exited_at"]),
            tail=str(data.get("tail") or ""),
            total_chars=int(data.get("total_chars") or 0),
            owner_session=None if data.get("owner_session") is None else str(data["owner_session"]),
            cwd=str(data.get("cwd") or ""),
        )
    except (KeyError, TypeError, ValueError):
        return None
    if receipt.id != path.stem or not _ID.match(receipt.id):
        return None
    return receipt


def _ended(path: Path, receipt: Receipt | None) -> float:
    if receipt is not None:
        try:
            return datetime.fromisoformat(receipt.exited_at).timestamp()
        except ValueError:
            pass
    return path.stat().st_mtime


def prune(home: Path, *, now: datetime) -> int:
    """Drop receipts older than ``MAX_AGE_S``, then all but the newest ``MAX_KEPT``."""
    directory = receipt_dir(home)
    if not directory.is_dir():
        return 0
    cutoff = now.timestamp() - MAX_AGE_S
    kept: list[tuple[float, str, Path]] = []
    removed = 0
    for path in directory.glob("proc_*.json"):
        try:
            ended = _ended(path, _read(path))
        except OSError:
            continue
        if ended < cutoff:
            path.unlink(missing_ok=True)
            removed += 1
        else:
            kept.append((ended, path.name, path))
    kept.sort(reverse=True)
    for _, _, path in kept[MAX_KEPT:]:
        path.unlink(missing_ok=True)
        removed += 1
    return removed


def _owns(owner: str | None, caller: str | None) -> bool:
    if not owner or not caller:
        return False
    if owner == caller:
        return True
    with _resolver_lock:
        resolver = _resolver[0] if _resolver else None
    if resolver is None:
        return False
    try:
        return bool(resolver(owner, caller))
    except Exception:
        log.debug("lineage check failed", exc_info=True)
        return False


def owned_by(home: Path, caller_session: str | None) -> list[Receipt]:
    """Every kept result the caller's session may read, oldest first."""
    directory = receipt_dir(home)
    if not caller_session or not directory.is_dir():
        return []
    found = [
        receipt
        for path in directory.glob("proc_*.json")
        if (receipt := _read(path)) is not None and _owns(receipt.owner_session, caller_session)
    ]
    return sorted(found, key=lambda r: (r.started_at, r.id))


def find(home: Path, ident: str, caller_session: str | None) -> Receipt | None:
    """The receipt ``ident`` names (full id or a unique prefix of ≥ 4 hex digits), when the
    caller's session owns it (module notes); else None."""
    query = ident.strip()
    if not query:
        return None
    if not query.startswith("proc_"):
        query = "proc_" + query
    if not re.fullmatch(r"proc_[0-9a-f]+", query):
        return None
    directory = receipt_dir(home)
    exact = directory / f"{query}.json"
    candidates: list[Path]
    if _ID.match(query) and exact.is_file():
        candidates = [exact]
    else:
        if len(query) - len("proc_") < MIN_PREFIX_HEX or not directory.is_dir():
            return None
        candidates = [p for p in directory.glob(f"{query}*.json") if _ID.match(p.stem)]
        if len(candidates) != 1:
            return None
    receipt = _read(candidates[0])
    if receipt is None or not _owns(receipt.owner_session, caller_session):
        return None
    return receipt
