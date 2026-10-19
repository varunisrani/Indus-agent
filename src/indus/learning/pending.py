# pyright: strict
"""Pending writes and the write-approval gate (P09-03), shared by memory and skills.

A memory or skill write may be held for a person. ``memory.write_approval`` and
``skills.write_approval`` (default off) turn the gate on. ``decide`` says what happens to one write:

- gate off → ``allow`` (write now);
- gate on, memory, a turn a person runs (``foreground`` or ``/learn``, not the review fork), and
  someone can answer an inline question (the interactive CLI) → ``ask``;
- anything else with the gate on → ``stage``. Skill writes always stage: too large to approve in a
  one-line prompt.

Independently of the gates, an unattended review fork never patches or removes memory directly:
those changes always stage (R07.4; see ``memory.tool``).

A staged write is ``<home>/pending/<subsystem>/<id>.json`` (0600, written atomically), so it
survives restarts. A staged change that targets one memory entry carries that entry's full text
(``pinned_entry``, or per op inside the payload); replaying it applies only while that exact entry
still exists and otherwise refuses and leaves the record pending. ``approve`` / ``reject`` take ids
or ``all`` (creation order). The subsystem supplies the replay through an ``apply`` callback.

Write origin: ``bind_origin`` sets who is writing for the current context (``foreground`` by
default, ``background_review`` inside the review fork, ``learn`` for ``/learn`` turns) and whether a
person started it (``attended``; ``/refine`` is attended). Context variables follow asyncio tasks
and ``contextvars.copy_context``, so a fork that copies its context keeps the binding.
"""

from __future__ import annotations

import contextlib
import contextvars
import difflib
import json
import logging
import secrets
from collections.abc import Callable, Generator, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from indus.foundation.clock import Clock
from indus.foundation.fsio import atomic_write_json
from indus.foundation.home import secure_dir
from indus.foundation.scope import Scope

__all__ = [
    "CHAT_DIFF_LIMIT",
    "ORIGINS",
    "SUBSYSTEMS",
    "Applied",
    "Approval",
    "Decision",
    "Origin",
    "PendingStore",
    "PendingWrite",
    "Subsystem",
    "WriteContext",
    "approve",
    "bind_origin",
    "current_origin",
    "current_write",
    "decide",
    "describe",
    "gate_on",
    "reject",
    "render_approval",
    "skill_diff",
    "staged_message",
    "unattended_review",
]

log = logging.getLogger(__name__)

type Subsystem = Literal["memory", "skills"]
type Origin = Literal["foreground", "background_review", "learn"]
type Surface = Literal["cli", "chat"]

SUBSYSTEMS: tuple[Subsystem, ...] = ("memory", "skills")
ORIGINS: tuple[Origin, ...] = ("foreground", "background_review", "learn")
GATE_KEYS: Mapping[Subsystem, str] = {
    "memory": "memory.write_approval",
    "skills": "skills.write_approval",
}
CHAT_DIFF_LIMIT = 3000
_TRUE_WORDS = frozenset({"true", "yes", "on", "1"})
_CUT_MARK = "\n… (diff cut; run `indus {subsystem} pending` in a terminal for all of it)\n"


# -- write origin ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class WriteContext:
    origin: Origin = "foreground"
    attended: bool = True


_WRITE: contextvars.ContextVar[WriteContext] = contextvars.ContextVar(
    "indus_write_context",
    default=WriteContext(),  # noqa: B039 - a frozen dataclass, never mutated
)


def current_write() -> WriteContext:
    return _WRITE.get()


def current_origin() -> Origin:
    return _WRITE.get().origin


def unattended_review() -> bool:
    """True inside a review fork that no person started."""
    context = _WRITE.get()
    return context.origin == "background_review" and not context.attended


@contextlib.contextmanager
def bind_origin(origin: Origin, *, attended: bool = True) -> Generator[WriteContext]:
    """Mark writes in this context as coming from ``origin`` until the block ends."""
    if origin not in ORIGINS:
        raise ValueError(f"unknown write origin {origin!r}")
    context = WriteContext(origin, attended)
    token = _WRITE.set(context)
    try:
        yield context
    finally:
        _WRITE.reset(token)


# -- the gate -------------------------------------------------------------------------------------


def gate_on(scope: Scope, subsystem: Subsystem) -> bool:
    """``<subsystem>.write_approval``; missing, malformed or unreadable means off."""
    config = scope.config
    if config is None:
        return False
    try:
        value = config.get(GATE_KEYS[subsystem])
    except Exception:
        log.debug("could not read %s; the gate stays off", GATE_KEYS[subsystem], exc_info=True)
        return False
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.strip().lower() in _TRUE_WORDS


type Decision = Literal["allow", "ask", "stage"]


def decide(subsystem: Subsystem, *, gate: bool, origin: Origin, can_ask: bool) -> Decision:
    """What to do with one write (see the module notes)."""
    if not gate:
        return "allow"
    if subsystem == "memory" and origin != "background_review" and can_ask:
        return "ask"
    return "stage"


def staged_message(subsystem: Subsystem) -> str:
    return (
        f"Held for approval ({subsystem}.write_approval is on); it is not saved yet. The user can "
        f"review it with /{subsystem} pending or `indus {subsystem} pending`."
    )


# -- the records ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class PendingWrite:
    id: str
    subsystem: Subsystem
    payload: Mapping[str, Any]
    summary: str
    origin: str
    created_at: float
    seq: int = 0
    pinned_entry: str | None = None

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "subsystem": self.subsystem,
            "summary": self.summary,
            "origin": self.origin,
            "created_at": self.created_at,
            "seq": self.seq,
            "payload": dict(self.payload),
        }
        if self.pinned_entry is not None:
            data["pinned_entry"] = self.pinned_entry
        return data

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> PendingWrite:
        subsystem = data.get("subsystem")
        payload = data.get("payload")
        ident = data.get("id")
        if subsystem not in SUBSYSTEMS or not isinstance(ident, str) or not ident:
            raise ValueError("not a pending write record")
        if not isinstance(payload, Mapping):
            raise ValueError("pending write without a payload")
        pinned = data.get("pinned_entry")
        created = data.get("created_at")
        seq = data.get("seq")
        return cls(
            id=ident,
            subsystem=subsystem,
            payload=cast(Mapping[str, Any], payload),
            summary=str(data.get("summary") or ""),
            origin=str(data.get("origin") or "foreground"),
            created_at=float(created) if isinstance(created, (int, float)) else 0.0,
            seq=seq if isinstance(seq, int) else 0,
            pinned_entry=pinned if isinstance(pinned, str) else None,
        )


def _valid_id(text: str) -> bool:
    return bool(text) and len(text) <= 64 and all(ch.isalnum() or ch in "-_" for ch in text)


class PendingStore:
    """Staged writes of one profile home, one JSON file each."""

    def __init__(self, home: Path, clock: Clock) -> None:
        self.home = home
        self.root = home / "pending"
        self.clock = clock

    def directory(self, subsystem: Subsystem) -> Path:
        return self.root / subsystem

    def _path(self, subsystem: Subsystem, ident: str) -> Path:
        return self.directory(subsystem) / f"{ident}.json"

    def stage(
        self,
        subsystem: Subsystem,
        payload: Mapping[str, Any],
        *,
        summary: str,
        origin: str,
        pinned_entry: str | None = None,
    ) -> PendingWrite:
        """Write a new record and return it. A disk failure raises: the caller must then refuse
        the write (nothing may be committed silently, nothing may be lost silently)."""
        if subsystem not in SUBSYSTEMS:
            raise ValueError(f"unknown subsystem {subsystem!r}")
        json.dumps(payload)  # refuse what cannot be stored before touching the disk
        directory = secure_dir(self.directory(subsystem), home=self.home)
        existing = self.list(subsystem)
        record = PendingWrite(
            id=secrets.token_hex(5),
            subsystem=subsystem,
            payload=dict(payload),
            summary=summary.strip(),
            origin=origin,
            created_at=self.clock.now().timestamp(),
            seq=max((r.seq for r in existing), default=0) + 1,
            pinned_entry=pinned_entry,
        )
        atomic_write_json(directory / f"{record.id}.json", record.to_json(), mode=0o600)
        log.info("staged %s write %s: %s", subsystem, record.id, record.summary[:120])
        return record

    def list(self, subsystem: Subsystem) -> list[PendingWrite]:
        """Every readable record, in creation order."""
        directory = self.directory(subsystem)
        if not directory.is_dir():
            return []
        found: list[PendingWrite] = []
        for path in directory.glob("*.json"):
            record = self._load(path)
            if record is not None:
                found.append(record)
        found.sort(key=lambda r: (r.seq, r.created_at, r.id))
        return found

    def _load(self, path: Path) -> PendingWrite | None:
        try:
            data: object = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(data, Mapping):
                raise ValueError("not a JSON object")
            return PendingWrite.from_json(cast(Mapping[str, Any], data))
        except (OSError, ValueError) as exc:
            log.warning("skipping unreadable pending record %s: %s", path, exc)
            return None

    def get(self, subsystem: Subsystem, ident: str) -> PendingWrite | None:
        if not _valid_id(ident):
            return None
        path = self._path(subsystem, ident)
        return self._load(path) if path.is_file() else None

    def discard(self, subsystem: Subsystem, ident: str) -> bool:
        if not _valid_id(ident):
            return False
        try:
            self._path(subsystem, ident).unlink()
        except FileNotFoundError:
            return False
        return True

    def count(self, subsystem: Subsystem) -> int:
        directory = self.directory(subsystem)
        return sum(1 for _ in directory.glob("*.json")) if directory.is_dir() else 0


# -- approving and rejecting ----------------------------------------------------------------------


@dataclass(frozen=True)
class Applied:
    """What a subsystem's replay of one record did. ``overwritten`` holds the full text of every
    entry the change replaced or removed; ``diff`` a unified diff for skill writes."""

    ok: bool
    message: str = ""
    overwritten: tuple[str, ...] = ()
    diff: str = ""


@dataclass(frozen=True)
class Approval:
    id: str
    found: bool
    applied: Applied
    record: PendingWrite | None = None

    @property
    def ok(self) -> bool:
        return self.found and self.applied.ok


def _selected(
    store: PendingStore, subsystem: Subsystem, which: str | Iterable[str]
) -> list[tuple[str, PendingWrite | None]]:
    if isinstance(which, str) and which.strip().lower() == "all":
        return [(r.id, r) for r in store.list(subsystem)]
    ids = [which] if isinstance(which, str) else list(which)
    return [(ident, store.get(subsystem, ident)) for ident in ids]


def approve(
    store: PendingStore,
    subsystem: Subsystem,
    which: str | Iterable[str],
    apply: Callable[[PendingWrite], Applied],
) -> list[Approval]:
    """Replay the chosen records in creation order. A record leaves the store only when its
    replay succeeded; a refusal (or a raising callback) keeps it pending."""
    picked = _selected(store, subsystem, which)
    # Creation order; ids that name nothing are reported last.
    last = (float("inf"), float("inf"))
    picked.sort(key=lambda pair: (pair[1].seq, pair[1].created_at) if pair[1] else last)
    results: list[Approval] = []
    for ident, record in picked:
        if record is None:
            results.append(Approval(ident, False, Applied(False, f"no pending write {ident}")))
            continue
        try:
            outcome = apply(record)
        except Exception as exc:
            log.error("replaying pending %s write %s failed", subsystem, ident, exc_info=True)
            outcome = Applied(False, f"could not apply: {type(exc).__name__}: {exc}")
        if outcome.ok:
            store.discard(subsystem, ident)
        results.append(Approval(ident, True, outcome, record))
    return results


def reject(store: PendingStore, subsystem: Subsystem, which: str | Iterable[str]) -> list[str]:
    """Delete the chosen records (nothing else changes); returns the ids removed."""
    return [
        ident
        for ident, record in _selected(store, subsystem, which)
        if record is not None and store.discard(subsystem, ident)
    ]


# -- rendering ------------------------------------------------------------------------------------


def skill_diff(
    before: str, after: str, path: str, *, surface: Surface = "cli", subsystem: str = "skills"
) -> str:
    """A unified diff of one skill file; chat surfaces get at most ``CHAT_DIFF_LIMIT`` chars."""
    lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    )
    text = "".join(line if line.endswith("\n") else line + "\n" for line in lines)
    if not text:
        return "(no change)\n"
    if surface == "chat" and len(text) > CHAT_DIFF_LIMIT:
        mark = _CUT_MARK.format(subsystem=subsystem)
        return text[: CHAT_DIFF_LIMIT - len(mark)] + mark
    return text


def render_approval(approval: Approval, *, surface: Surface = "cli") -> str:
    """Lines for one approve result: what was applied, every entry it replaced or removed in full,
    and for skills the diff (cut on chat surfaces)."""
    record = approval.record
    head = f"{approval.id}: {record.summary}" if record is not None else approval.id
    if not approval.ok:
        return f"{head}\n  not applied: {approval.applied.message}"
    lines = [
        f"{head}\n  applied" + (f": {approval.applied.message}" if approval.applied.message else "")
    ]
    for text in approval.applied.overwritten:
        lines.append(
            "  replaced or removed:\n" + "\n".join(f"    {ln}" for ln in text.splitlines())
        )
    if approval.applied.diff:
        diff = approval.applied.diff
        if surface == "chat" and len(diff) > CHAT_DIFF_LIMIT:
            subsystem = record.subsystem if record is not None else "skills"
            mark = _CUT_MARK.format(subsystem=subsystem)
            diff = diff[: CHAT_DIFF_LIMIT - len(mark)] + mark
        lines.append(diff.rstrip("\n"))
    return "\n".join(lines)


def describe(records: Sequence[PendingWrite]) -> str:
    """A short listing for ``pending`` views."""
    if not records:
        return "Nothing is waiting for approval."
    return "\n".join(f"{r.id}  [{r.origin}]  {r.summary}" for r in records)
