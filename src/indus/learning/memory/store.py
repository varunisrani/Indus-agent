# pyright: strict
"""The memory stores (P09-01): two small, bounded plain-text files per profile.

``<home>/memories/MEMORY.md`` holds the agent's own notes (target ``memory``) and
``<home>/memories/USER.md`` what it knows about the user (target ``user``). A file is a list of
entries joined by ``DELIMITER`` (a line holding only ``§``); an entry may span lines. The used size
is the length of the joined text, delimiters included, and each store has a character limit.

Reading strips every entry, drops empty ones and exact duplicates (the first copy wins). Writing
takes an exclusive lock on the sibling ``<file>.lock`` (created 0600 and opened without following a
symlink), re-reads the file under the lock, runs the requested change through the pure
``apply_ops`` and replaces the file atomically. ``apply_ops`` checks the limit on the final list
only, so one batch can trim and add when the store is full.

Two states refuse every rewrite: a file that exists but cannot be decoded (writing would wipe it),
and a file holding one entry larger than the whole limit — free text written without delimiters,
which a rewrite would lose. The second also saves the current bytes to ``<file>.bak`` and returns
repair guidance. A file over its limit (a hand edit, a lowered limit) still loads, with a warning;
it then refuses changes that add or grow until it is back under the limit.

One process per profile home is assumed for the prompt snapshot (see ``prompt_block``); the lock
keeps writers from different processes and threads from losing each other's entries.
"""

from __future__ import annotations

import contextlib
import difflib
import errno
import logging
import os
import sys
import threading
import time
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from indus.foundation.fsio import atomic_write_bytes, atomic_write_text
from indus.foundation.home import secure_dir
from indus.foundation.scope import Scope

__all__ = [
    "BLOCK_TAGS",
    "CONFIG_ENABLE_KEYS",
    "CONFIG_LIMIT_KEYS",
    "DEFAULT_LIMITS",
    "DELIMITER",
    "FILE_NAMES",
    "STORE_LABELS",
    "TARGETS",
    "Change",
    "LockBusy",
    "LockRefused",
    "MemoryStore",
    "Op",
    "Result",
    "Snapshot",
    "Target",
    "Usage",
    "apply_ops",
    "closest_entries",
    "coerce_target",
    "fold_quotes",
    "joined_size",
    "parse_entries",
    "render_block",
    "select_entry",
    "store_lock",
    "used_percent",
]

log = logging.getLogger(__name__)

type Target = Literal["memory", "user"]
type EntryCheck = Callable[[str], str | None]
"""Returns a refusal reason for an entry text the store must not hold, or None."""

TARGETS: tuple[Target, ...] = ("memory", "user")
FILE_NAMES: Mapping[Target, str] = {"memory": "MEMORY.md", "user": "USER.md"}
DELIMITER = "\n§\n"
DEFAULT_LIMITS: Mapping[Target, int] = {"memory": 2200, "user": 1375}
CONFIG_LIMIT_KEYS: Mapping[Target, str] = {
    "memory": "memory.notes_char_limit",
    "user": "memory.profile_char_limit",
}
CONFIG_ENABLE_KEYS: Mapping[Target, str] = {
    "memory": "memory.notes_enabled",
    "user": "memory.profile_enabled",
}
# What each store is called in the prompt block and in messages.
STORE_LABELS: Mapping[Target, str] = {"memory": "saved notes", "user": "user profile"}
BLOCK_TAGS: Mapping[Target, str] = {"memory": "saved-notes", "user": "user-profile"}
LOCK_SUFFIX = ".lock"
BACKUP_SUFFIX = ".bak"
LOCK_TIMEOUT_S = 15.0
_LOCK_POLL_S = 0.01

# Curly quotes, low quotes, primes and backticks all read as a plain apostrophe when matching.
# Each maps one character to one character, so indexes found in folded text hold in the original.
_QUOTES = '"`´‘’‚‛“”„‟′″'
_FOLD = str.maketrans(dict.fromkeys(_QUOTES, "'"))


class LockRefused(OSError):
    """The lock file is a symlink (or otherwise cannot be opened safely)."""


# -- pure helpers ---------------------------------------------------------------------------------


def joined_size(entries: Sequence[str]) -> int:
    """Characters the entries take on disk: their text plus one delimiter between each pair."""
    return len(DELIMITER.join(entries))


def parse_entries(raw: str) -> list[str]:
    """Entries of a file's text: split on the delimiter, stripped, empty ones and exact duplicates
    dropped (first copy wins)."""
    seen: dict[str, None] = {}
    for piece in raw.split(DELIMITER):
        entry = piece.strip()
        if entry:
            seen.setdefault(entry, None)
    return list(seen)


def fold_quotes(text: str) -> str:
    """``text`` with every quote-like character as ``'`` (same length, same positions)."""
    return text.translate(_FOLD)


def _occurrences(text: str, span: str) -> list[int]:
    """Start indexes of ``span`` in ``text``, overlapping ones included."""
    if not span:
        return []
    found: list[int] = []
    start = text.find(span)
    while start >= 0:
        found.append(start)
        start = text.find(span, start + 1)
    return found


def _has_word(text: str) -> bool:
    return any(ch.isalnum() for ch in text)


def select_entry(entries: Sequence[str], old_text: str) -> tuple[int | None, list[int]]:
    """``(index, matches)`` for the entry ``old_text`` picks.

    An entry equal to ``old_text`` wins outright (so a short entry stays reachable when a longer one
    contains it); otherwise every entry containing it, compared as written first and with quotes
    folded second. ``index`` is None when nothing or more than one entry matched; ``matches`` lists
    the candidates either way.
    """
    if not old_text:
        return None, []
    for i, entry in enumerate(entries):
        if entry == old_text:
            return i, [i]
    folded = fold_quotes(old_text)
    for i, entry in enumerate(entries):
        if fold_quotes(entry) == folded:
            return i, [i]
    hits = [i for i, entry in enumerate(entries) if old_text in entry]
    if not hits and _has_word(folded):
        hits = [i for i, entry in enumerate(entries) if folded in fold_quotes(entry)]
    return (hits[0] if len(hits) == 1 else None), hits


def closest_entries(entries: Sequence[str], text: str, count: int = 3) -> list[str]:
    """The ``count`` entries most like ``text`` (case- and quote-insensitive), best first."""
    if not entries or not text.strip():
        return []
    probe = fold_quotes(text).lower()
    scored = [
        (difflib.SequenceMatcher(None, probe, fold_quotes(entry).lower()).ratio(), -i, entry)
        for i, entry in enumerate(entries)
    ]
    scored.sort(reverse=True)
    return [entry for _, _, entry in scored[:count]]


@dataclass(frozen=True)
class Op:
    """One change. ``pinned_entry`` (a staged write being replayed) names the exact entry the
    change was reviewed against; when it is no longer present the change is refused."""

    action: str
    content: str = ""
    old_text: str = ""
    pinned_entry: str | None = None


@dataclass(frozen=True)
class Change:
    """What one op did: the full entry before (patch/remove) and after (add/patch)."""

    index: int
    action: str
    before: str | None = None
    after: str | None = None
    duplicate: bool = False


@dataclass(frozen=True)
class Result:
    """The outcome of ``apply_ops`` / ``MemoryStore.apply``.

    ``kind`` is ``applied`` on success; otherwise one of ``invalid``, ``no_match``, ``ambiguous``,
    ``span_repeated``, ``no_op``, ``stale``, ``blocked``, ``over_limit``, ``unreadable``, ``drift``,
    ``lock_refused``, ``lock_busy``. ``entries`` is the final list on success and the unchanged list
    otherwise; ``op_index`` (1-based) names the op that failed.
    """

    ok: bool
    kind: str
    message: str = ""
    entries: tuple[str, ...] = ()
    changes: tuple[Change, ...] = ()
    op_index: int | None = None
    details: Mapping[str, Any] = field(default_factory=dict[str, Any])

    @property
    def wrote(self) -> bool:
        """True when the entry list changed."""
        return self.ok and any(not c.duplicate for c in self.changes)


def _fail(
    kind: str,
    message: str,
    entries: Sequence[str],
    *,
    op_index: int | None = None,
    **details: Any,
) -> Result:
    return Result(False, kind, message, tuple(entries), (), op_index, details)


def _locate(working: list[str], op: Op, pos: int) -> int | Result:
    if op.pinned_entry is not None:
        if op.pinned_entry in working:
            return working.index(op.pinned_entry)
        return _fail(
            "stale",
            "the entry this change was reviewed against is no longer saved",
            working,
            op_index=pos,
            pinned_entry=op.pinned_entry,
        )
    index, hits = select_entry(working, op.old_text)
    if index is not None:
        return index
    if hits:
        return _fail(
            "ambiguous",
            f"'{op.old_text}' matches {len(hits)} entries",
            working,
            op_index=pos,
            matches=[working[i] for i in hits],
        )
    return _fail(
        "no_match",
        f"no entry contains '{op.old_text}'",
        working,
        op_index=pos,
        closest_entries=closest_entries(working, op.old_text),
    )


def _patched(entry: str, old_text: str, new_text: str) -> str | tuple[str, str]:
    """``entry`` with the one occurrence of ``old_text`` replaced, or ``(kind, message)``."""
    starts = _occurrences(entry, old_text)
    if not starts:
        starts = _occurrences(fold_quotes(entry), fold_quotes(old_text))
    if len(starts) > 1:
        return (
            "span_repeated",
            f"'{old_text}' occurs {len(starts)} times in that entry; include more of the text "
            "around it so it occurs once",
        )
    if not starts:  # the entry was picked by equality after folding: the span is the whole entry
        return new_text.strip()
    at = starts[0]
    return (entry[:at] + new_text + entry[at + len(old_text) :]).strip()


def _apply_one(working: list[str], op: Op, pos: int, check: EntryCheck | None) -> Change | Result:
    action = op.action
    if action == "add":
        content = op.content.strip()
        if not content:
            return _fail("invalid", "add needs content", working, op_index=pos)
        if content in working:
            return Change(pos, action, after=content, duplicate=True)
        if check is not None and (reason := check(content)) is not None:
            return _fail("blocked", reason, working, op_index=pos)
        working.append(content)
        return Change(pos, action, after=content)
    if action not in ("patch", "remove"):
        return _fail("invalid", f"unknown action '{action}'", working, op_index=pos)
    if op.pinned_entry is None and not op.old_text.strip():
        return _fail("invalid", f"{action} needs old_text", working, op_index=pos)
    found = _locate(working, op, pos)
    if isinstance(found, Result):
        return found
    before = working[found]
    if action == "remove":
        del working[found]
        return Change(pos, action, before=before)
    if not op.content.strip():
        return _fail(
            "invalid",
            "patch needs content (the new text for the old_text span); to delete the whole entry "
            "use remove",
            working,
            op_index=pos,
        )
    span = op.old_text if op.old_text else before
    after = _patched(before, span, op.content)
    if isinstance(after, tuple):
        return _fail(after[0], after[1], working, op_index=pos)
    if after == before:
        return _fail("no_op", "the patch leaves the entry unchanged", working, op_index=pos)
    if check is not None and (reason := check(after)) is not None:
        return _fail("blocked", reason, working, op_index=pos)
    if after in working:  # the patch made it a copy of another entry: keep one
        del working[found]
    else:
        working[found] = after
    return Change(pos, action, before=before, after=after)


def apply_ops(
    entries: Sequence[str],
    ops: Sequence[Op],
    *,
    limit: int,
    check: EntryCheck | None = None,
) -> Result:
    """Apply ``ops`` in order to a copy of ``entries`` (pure; nothing is read or written).

    All or nothing: the first failing op ends the call with the original list. The limit is checked
    once, on the final list: it must fit, or — for a store already over its limit — the batch must
    add nothing and leave the store no larger than it was. ``check`` vets every entry text an add or
    patch would store.
    """
    original = list(entries)
    if not ops:
        return _fail("invalid", "no operations given", original)
    working = list(original)
    changes: list[Change] = []
    for pos, op in enumerate(ops, start=1):
        outcome = _apply_one(working, op, pos, check)
        if isinstance(outcome, Result):
            return Result(
                False,
                outcome.kind,
                outcome.message,
                tuple(original),
                (),
                outcome.op_index,
                outcome.details,
            )
        changes.append(outcome)
    used, final = joined_size(original), joined_size(working)
    added = sum(len(c.after or "") for c in changes if c.action == "add" and not c.duplicate)
    if final > limit and (added or final > used):
        return _fail(
            "over_limit",
            f"the result would take {final:,} of {limit:,} chars",
            original,
            used=used,
            limit=limit,
            new_size=final,
            added_chars=added,
            overflow=final - limit,
        )
    return Result(True, "applied", "", tuple(working), tuple(changes))


# -- usage and rendering --------------------------------------------------------------------------


@dataclass(frozen=True)
class Usage:
    used: int
    limit: int

    @property
    def percent(self) -> int:
        return used_percent(self.used, self.limit)

    @property
    def text(self) -> str:
        return f"{self.percent}% — {self.used:,}/{self.limit:,} chars"


def used_percent(used: int, limit: int) -> int:
    return (used * 100) // limit if limit > 0 else 0


def render_block(target: Target, entries: Sequence[str], limit: int) -> str:
    """The prompt text of one store: a tag naming it with its usage, the entries separated by
    ``§`` lines, the closing tag. An empty store renders as ""."""
    if not entries:
        return ""
    tag = BLOCK_TAGS[target]
    usage = Usage(joined_size(entries), limit).text
    body = DELIMITER.join(_defuse(entry) for entry in entries)
    return f'<{tag} usage="{usage}">\n{body}\n</{tag}>'


def _defuse(entry: str) -> str:
    # An entry line that looks like one of the block tags would confuse ``prompt_block.extract``.
    tags = tuple(f"<{t}" for t in BLOCK_TAGS.values()) + tuple(
        f"</{t}" for t in BLOCK_TAGS.values()
    )
    lines = entry.split("\n")
    return "\n".join("> " + line if line.lstrip().startswith(tags) else line for line in lines)


@dataclass(frozen=True)
class Snapshot:
    """Rendered blocks of both stores (each "" when empty or disabled)."""

    memory: str = ""
    user: str = ""

    def block(self, target: Target) -> str:
        return self.memory if target == "memory" else self.user


# -- the lock -------------------------------------------------------------------------------------

if sys.platform == "win32":
    import msvcrt

    def _try_lock(fd: int) -> bool:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        with contextlib.suppress(OSError):
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                return False
            raise
        return True

    def _unlock(fd: int) -> None:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)


class LockBusy(TimeoutError):
    """The store lock stayed held past the timeout."""


@contextlib.contextmanager
def store_lock(path: Path, timeout: float = LOCK_TIMEOUT_S) -> Generator[None]:
    """Exclusive lock on ``<path>.lock`` across threads and processes (real time, not the scope
    clock: it bounds an actual wait). A symlinked lock file raises ``LockRefused``."""
    lock_path = path.with_name(path.name + LOCK_SUFFIX)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow and lock_path.is_symlink():
        raise LockRefused(errno.ELOOP, "refusing a symlinked lock file", str(lock_path))
    flags = os.O_RDWR | os.O_CREAT | nofollow | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise LockRefused(exc.errno, "refusing a symlinked lock file", str(lock_path)) from exc
        raise
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        deadline = time.monotonic() + timeout
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                raise LockBusy(f"timed out after {timeout:g}s waiting for {lock_path}")
            threading.Event().wait(_LOCK_POLL_S)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


# -- the store ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Read:
    raw: str
    entries: list[str]
    readable: bool


def _setting(scope: Scope, key: str) -> object:
    config = scope.config
    if config is None:
        return None
    try:
        return config.get(key)
    except Exception:
        log.debug("config read failed for %s", key, exc_info=True)
        return None


def _limit_from(value: object, target: Target) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    if value is not None:
        log.warning(
            "%s is %r, not a positive whole number; using %d",
            CONFIG_LIMIT_KEYS[target],
            value,
            DEFAULT_LIMITS[target],
        )
    return DEFAULT_LIMITS[target]


def _flag_from(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "no", "off", "0")
    return True


class MemoryStore:
    """Both stores of one profile home. Cheap to build; every call reads the files fresh."""

    def __init__(
        self,
        home: Path,
        *,
        limits: Mapping[Target, int] | None = None,
        enabled: Mapping[Target, bool] | None = None,
        lock_timeout: float = LOCK_TIMEOUT_S,
    ) -> None:
        self.home = home
        self.directory = home / "memories"
        self._limits: dict[Target, int] = {**DEFAULT_LIMITS, **(limits or {})}
        self._enabled: dict[Target, bool] = {t: True for t in TARGETS}
        self._enabled.update(enabled or {})
        self.lock_timeout = lock_timeout
        self._warned: set[tuple[str, int]] = set()

    @classmethod
    def for_scope(cls, scope: Scope) -> MemoryStore:
        """The stores of ``scope``'s home, with limits and switches from its config."""
        limits: dict[Target, int] = {
            t: _limit_from(_setting(scope, CONFIG_LIMIT_KEYS[t]), t) for t in TARGETS
        }
        enabled: dict[Target, bool] = {
            t: _flag_from(_setting(scope, CONFIG_ENABLE_KEYS[t])) for t in TARGETS
        }
        return cls(scope.home, limits=limits, enabled=enabled)

    # -- facts ------------------------------------------------------------------------------------

    def path(self, target: Target) -> Path:
        return self.directory / FILE_NAMES[target]

    def limit(self, target: Target) -> int:
        return self._limits[target]

    def enabled(self, target: Target) -> bool:
        return self._enabled[target]

    @property
    def live_targets(self) -> tuple[Target, ...]:
        return tuple(t for t in TARGETS if self._enabled[t])

    def entries(self, target: Target) -> list[str]:
        """The saved entries now (read from disk; an unreadable file reads as empty here, but
        writes to it are refused)."""
        return self._read(target).entries

    def usage(self, target: Target) -> Usage:
        return Usage(joined_size(self.entries(target)), self.limit(target))

    # -- reading ----------------------------------------------------------------------------------

    def _read(self, target: Target) -> _Read:
        path = self.path(target)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return _Read("", [], True)
        except OSError as exc:
            log.warning("could not read %s: %s", path, exc)
            return _Read("", [], False)
        try:
            raw = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            log.warning("%s is not valid UTF-8; it is left as it is and writes are refused", path)
            return _Read("", [], False)
        entries = parse_entries(raw)
        size = joined_size(entries)
        if size > self.limit(target) and (str(path), size) not in self._warned:
            self._warned.add((str(path), size))
            log.warning(
                "%s holds %d chars, over its limit of %d; it stays loaded, and changes that add "
                "or grow are refused until it is back under the limit",
                path,
                size,
                self.limit(target),
            )
        return _Read(raw, entries, True)

    # -- writing ----------------------------------------------------------------------------------

    def preview(
        self, target: Target, ops: Sequence[Op], *, check: EntryCheck | None = None
    ) -> Result:
        """What ``apply`` would do now, under the lock, without writing."""
        return self._run(target, ops, check, write=False)

    def apply(
        self, target: Target, ops: Sequence[Op], *, check: EntryCheck | None = None
    ) -> Result:
        """Run ``ops`` against the saved entries and write the result (see the module notes)."""
        return self._run(target, ops, check, write=True)

    def _run(
        self, target: Target, ops: Sequence[Op], check: EntryCheck | None, *, write: bool
    ) -> Result:
        path = self.path(target)
        secure_dir(self.directory, home=self.home)
        try:
            with store_lock(path, self.lock_timeout):
                current = self._read(target)
                if not current.readable:
                    return _fail(
                        "unreadable",
                        f"{path.name} exists but could not be read; nothing was changed so its "
                        "contents are not lost. Fix or move the file, then try again.",
                        [],
                    )
                oversized = [e for e in current.entries if len(e) > self.limit(target)]
                if oversized:
                    return self._drift(target, current.raw, current.entries)
                result = apply_ops(current.entries, ops, limit=self.limit(target), check=check)
                if write and result.wrote:
                    atomic_write_text(path, DELIMITER.join(result.entries))
                return result
        except LockRefused as exc:
            log.warning("memory write refused: %s", exc)
            return _fail(
                "lock_refused",
                f"the lock file {exc.filename} is a symlink; nothing was changed",
                [],
            )
        except LockBusy as exc:
            return _fail("lock_busy", str(exc), [])

    def _drift(self, target: Target, raw: str, entries: list[str]) -> Result:
        path = self.path(target)
        backup = path.with_name(path.name + BACKUP_SUFFIX)
        try:
            atomic_write_bytes(backup, raw.encode("utf-8"))
            saved = str(backup)
        except OSError as exc:
            log.warning("could not save %s: %s", backup, exc)
            saved = ""
        log.warning(
            "%s holds text longer than its whole limit (%d chars); rewrites refused",
            path,
            self.limit(target),
        )
        steps = (
            f"{path.name} holds a block of text longer than the store's whole limit, most likely "
            f"written without '§' separators. Rewriting it would lose that text, so nothing was "
            "changed. "
            + (f"Its current contents were copied to {saved}. " if saved else "")
            + f"Edit {path.name} into short entries separated by lines holding only '§' (or move "
            "the long text elsewhere), then try again."
        )
        return _fail("drift", steps, entries, backup=saved)

    # -- the snapshot -----------------------------------------------------------------------------

    def render_snapshot(self, transform: Callable[[Target, str], str] | None = None) -> Snapshot:
        """Rendered blocks of the enabled stores. ``transform`` may swap an entry's text for the
        prompt (the live files are never changed)."""
        blocks: dict[Target, str] = {}
        for target in TARGETS:
            if not self.enabled(target):
                blocks[target] = ""
                continue
            entries = self.entries(target)
            if transform is not None:
                entries = [transform(target, e) for e in entries]
            blocks[target] = render_block(target, entries, self.limit(target))
        return Snapshot(memory=blocks["memory"], user=blocks["user"])


def coerce_target(value: object) -> Target | None:
    """``value`` as a target name, or None."""
    for target in TARGETS:
        if value == target:
            return target
    return None
