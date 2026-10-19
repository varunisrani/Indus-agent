"""What this profile has learned, as timeline / map nodes (P09-23).

Nodes:

- skills with a learning signal: made here by the background review (``origin: agent``), by a
  person or ``/learn`` (``origin: learn``), or used at least once. Only skills in the profile's
  own folders count; bundled and hub skills, external folders and the archive are left out, as
  is a hand-written skill nobody has used yet.
- every memory entry, id ``memory:<store>:<index>:<sha8>`` — ``store`` is ``memory`` (the
  agent's notes) or ``user`` (the user profile), ``index`` the entry's position, ``sha8`` the
  first eight hex digits of the SHA-256 of its text. The digest is what names the entry: after
  the list shifts the id still resolves (or is reported stale), never to another entry.

``nodes(scope)`` is the data API for the CLI, the TUI (P12) and the desktop map (P20).
``delete_node`` archives a skill (``indus curator restore`` brings it back) or removes a memory
entry through the memory store and its lock. ``edit_node`` validates new skill text before it is
kept and puts the old bytes back otherwise; memory entries are replaced through the store.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from indus.foundation.fsio import atomic_write_bytes
from indus.foundation.scope import Scope
from indus.learning.curator import ledger_port
from indus.learning.curator.archive import Refused, archive_skill, local_skills, parse_time
from indus.learning.memory.store import TARGETS, MemoryStore, Op, Target
from indus.learning.skills.format import SkillFormatError, check_skill_text
from indus.learning.skills.usage import bundled_names, hub_names, usage_for

__all__ = [
    "JourneyError",
    "Node",
    "delete_node",
    "edit_node",
    "memory_id",
    "node_text",
    "nodes",
]

log = logging.getLogger(__name__)

MEMORY_PREFIX = "memory"
_LABEL_CHARS = 80


class JourneyError(Exception):
    """A node id that cannot be used (unknown, stale, malformed) or a refused change."""


@dataclass(frozen=True)
class Node:
    id: str
    kind: str
    label: str
    category: str
    timestamp: float | None
    when: str | None
    use_count: int = 0
    state: str = "active"
    origin: str | None = None
    pinned: bool = False
    store: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _digest(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:8]


def memory_id(store: Target, index: int, text: str) -> str:
    return f"{MEMORY_PREFIX}:{store}:{index}:{_digest(text)}"


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds") if ts is not None else None


def _label(text: str) -> str:
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    first = first.lstrip("#").strip()
    return first if len(first) <= _LABEL_CHARS else first[: _LABEL_CHARS - 1] + "…"


def _skill_nodes(scope: Scope) -> list[Node]:
    root = scope.home / "skills"
    excluded = bundled_names(root) | hub_names(root)
    records = usage_for(scope).load()
    out: list[Node] = []
    for name, rec in sorted(local_skills(scope).items()):
        if name in excluded:
            continue
        usage = records.get(name) or {}
        origin = usage.get("origin")
        used = int(usage.get("use_count") or 0)
        if origin not in ("agent", "learn") and used <= 0:
            continue
        stamp = parse_time(usage.get("created_at")) or parse_time(usage.get("last_activity_at"))
        if stamp is not None:
            ts: float | None = stamp.timestamp()
        else:
            try:
                ts = rec.skill_md.stat().st_mtime
            except OSError:
                ts = None
        out.append(
            Node(
                id=name,
                kind="skill",
                label=name,
                category=rec.category or "general",
                timestamp=ts,
                when=_iso(ts),
                use_count=used,
                state=str(usage.get("state") or "active"),
                origin=origin if isinstance(origin, str) else None,
                pinned=usage.get("pinned") is True,
            )
        )
    return out


def _memory_nodes(scope: Scope) -> list[Node]:
    store = MemoryStore.for_scope(scope)
    out: list[Node] = []
    for target in TARGETS:
        try:
            mtime: float | None = store.path(target).stat().st_mtime
        except OSError:
            continue
        for index, entry in enumerate(store.entries(target)):
            ts = mtime + index * 0.001 if mtime is not None else None
            out.append(
                Node(
                    id=memory_id(target, index, entry),
                    kind="memory",
                    label=_label(entry),
                    category="memory",
                    timestamp=ts,
                    when=_iso(ts),
                    origin="memory",
                    store=target,
                )
            )
    return out


def nodes(scope: Scope) -> list[Node]:
    """Every node, newest first (unknown times last)."""
    found = _skill_nodes(scope) + _memory_nodes(scope)
    return sorted(found, key=lambda n: (n.timestamp is None, -(n.timestamp or 0.0), n.id))


# -- resolving ids --------------------------------------------------------------------------------


def _parse_memory_id(node_id: str) -> tuple[Target, int, str]:
    parts = node_id.split(":")
    if len(parts) != 4 or parts[0] != MEMORY_PREFIX or parts[1] not in TARGETS:
        raise JourneyError(f"{node_id!r} is not a memory id (memory:<memory|user>:<n>:<hash>)")
    try:
        index = int(parts[2])
    except ValueError:
        raise JourneyError(f"{node_id!r} has a bad position") from None
    store: Target = "memory" if parts[1] == "memory" else "user"
    return store, index, parts[3]


def _memory_entry(scope: Scope, node_id: str) -> tuple[MemoryStore, Target, str]:
    store_name, _index, digest = _parse_memory_id(node_id)
    store = MemoryStore.for_scope(scope)
    for entry in store.entries(store_name):
        if _digest(entry) == digest:
            return store, store_name, entry
    raise JourneyError(f"{node_id!r} no longer matches a memory entry; list the nodes again")


def _skill_dir(scope: Scope, name: str) -> Path:
    root = scope.home / "skills"
    if name in bundled_names(root) or name in hub_names(root):
        raise JourneyError(f"{name!r} is not something this profile learned")
    rec = local_skills(scope).get(name)
    if rec is None:
        raise JourneyError(f"no skill named {name!r} in this profile")
    return rec.skill_dir


def node_text(scope: Scope, node_id: str) -> tuple[str, str]:
    """``(kind, current text)``: the full ``SKILL.md`` or the memory entry."""
    if node_id.startswith(f"{MEMORY_PREFIX}:"):
        _store, _target, entry = _memory_entry(scope, node_id)
        return "memory", entry
    folder = _skill_dir(scope, node_id)
    return "skill", (folder / "SKILL.md").read_text(encoding="utf-8")


# -- changes --------------------------------------------------------------------------------------


def delete_node(scope: Scope, node_id: str) -> str:
    """Archive the skill or remove the memory entry; returns a one-line confirmation."""
    if node_id.startswith(f"{MEMORY_PREFIX}:"):
        store, target, entry = _memory_entry(scope, node_id)
        result = store.apply(target, [Op("remove", old_text=entry, pinned_entry=entry)])
        if not result.ok:
            raise JourneyError(result.message or f"the memory store refused ({result.kind})")
        return f"removed the memory entry {_label(entry)!r}"
    _skill_dir(scope, node_id)
    if usage_for(scope).is_pinned(node_id):
        raise JourneyError(f"{node_id!r} is pinned; `indus curator unpin {node_id}` first")
    try:
        archive_skill(scope, node_id, actor="user")
    except Refused as exc:
        raise JourneyError(str(exc)) from None
    return f"archived {node_id}; `indus curator restore {node_id}` brings it back"


def edit_node(scope: Scope, node_id: str, edit: Callable[[Path], bool]) -> str:
    """Let ``edit(path)`` change the node's text in a file (False = the editor failed).

    Skills are edited in place and checked afterwards; a file that no longer validates (or
    renames the skill) is refused and the original bytes are written back. Memory entries are
    edited in a temporary file next to the store and saved through it.
    """
    if node_id.startswith(f"{MEMORY_PREFIX}:"):
        return _edit_memory(scope, node_id, edit)
    folder = _skill_dir(scope, node_id)
    path = folder / "SKILL.md"
    original = path.read_bytes()
    before = ledger_port.capture(folder, node_id, scope=scope)
    if not edit(path):
        atomic_write_bytes(path, original)
        raise JourneyError("the editor did not finish; nothing was changed")
    try:
        changed = path.read_bytes()
    except OSError as exc:
        atomic_write_bytes(path, original)
        raise JourneyError(f"could not read the edited file: {exc}") from None
    if changed == original:
        return "no changes"
    try:
        check_skill_text(changed.decode("utf-8"), expected_name=node_id)
    except (SkillFormatError, UnicodeDecodeError) as exc:
        atomic_write_bytes(path, original)
        problems = "; ".join(exc.problems) if isinstance(exc, SkillFormatError) else str(exc)
        raise JourneyError(f"not saved, the skill would not load: {problems}") from None
    ledger_port.record("edit", node_id, actor="user", before=before, after_dir=folder, scope=scope)
    usage_for(scope).bump_patch(node_id)
    return f"updated {node_id}"


def _edit_memory(scope: Scope, node_id: str, edit: Callable[[Path], bool]) -> str:
    store, target, entry = _memory_entry(scope, node_id)
    scratch = store.directory / f".journey-edit-{_digest(entry)}.txt"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.write_text(entry + "\n", encoding="utf-8")
    try:
        if not edit(scratch):
            raise JourneyError("the editor did not finish; nothing was changed")
        text = scratch.read_text(encoding="utf-8").strip()
    finally:
        scratch.unlink(missing_ok=True)
    if text == entry.strip():
        return "no changes"
    if not text:
        raise JourneyError("an empty entry is not saved; use delete to remove it")
    result = store.apply(target, [Op("patch", content=text, old_text=entry, pinned_entry=entry)])
    if not result.ok:
        raise JourneyError(result.message or f"the memory store refused ({result.kind})")
    return f"updated the memory entry {_label(text)!r}"
