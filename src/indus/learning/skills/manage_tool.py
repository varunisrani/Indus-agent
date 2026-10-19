"""``skill_manage``: the model's tool for creating and changing skills (P09-11).

One call is a single action (``action``, ``name`` and that action's fields at the top level) or
``operations: [...]``, several actions applied all or nothing:

- ``create {name, content, category?}`` — a new skill folder with ``SKILL.md`` in
  ``skills.create_dir`` when set, else ``<home>/skills/``;
- ``patch {name, old_string, new_string, file_path?, replace_all?}`` — replace text in ``SKILL.md``
  or a support file (exactly one match unless ``replace_all``; an empty ``new_string`` deletes);
  ``patch {name, content}`` (alias ``edit``) rewrites ``SKILL.md`` whole;
- ``delete {name, absorbed_into?}``;
- ``write_file {name, file_path, file_content}`` / ``remove_file {name, file_path}`` — support files
  under ``references/``, ``templates/``, ``scripts/`` or ``assets/``.

How a call runs:

1. Every op's shape is checked without touching anything. An op that carries another action's text
   field fails naming that field and where the text belongs; nothing in the call applies.
2. With ``skills.write_approval`` on, the whole call is staged as one pending write (P09-03).
3. The locks of every named skill are taken in a fixed order (``<home>/skills/.locks/``, outside
   the skill folders, so deleting a folder never removes a lock someone waits on). The ops are then
   played against an in-memory overlay of the files they touch, reading the disk under the locks,
   and every guard and format rule runs on the result.
4. Only then are files written. If a write fails part-way, the files already written get their old
   bytes back. Deleted folders are moved aside (or into the archive) last, so they can be put back.

Guards: a pinned skill cannot be deleted (patches are fine); inside the review fork a change to
existing content needs a fresh ``skill_view`` of that file in the same session, and deletes are
allowed only for curator-managed, unpinned skills outside ``external_dirs`` and go to
``<home>/skills/.archive/``; paths must stay inside the skill folder and the skill folder inside a
skills root, after symlinks.

After success (each step best-effort, never failing the call): a ledger entry, the list cache
cleared, usage telemetry, lifecycle and success listeners, and advisory lint findings in the result.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import logging
import os
import secrets
import shutil
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from indus.foundation.fsio import FileLock, atomic_write_bytes
from indus.foundation.scope import Scope
from indus.learning import pending
from indus.learning.skills.format import (
    SKILL_FILE,
    SkillFormatError,
    check_category,
    check_name,
    check_skill_text,
    check_support_path,
    check_support_size,
)
from indus.learning.skills.ledger import FileRef, SkillLedger
from indus.learning.skills.lint import lint_skill
from indus.learning.skills.resolver import (
    TIER_EXTERNAL,
    SkillCatalog,
    SkillError,
    SkillRecord,
    SkillsSettings,
    clear_scan_cache,
    load_catalog,
    skills_setting,
)
from indus.learning.skills.usage import bundled_names, usage_for
from indus.learning.skills.view_tools import READS, clear_list_cache
from indus.tools.calls import ToolResult
from indus.tools.registry import ToolRegistry, ToolTraits

__all__ = [
    "ACTIONS",
    "ARCHIVE_DIR",
    "ARCHIVER",
    "LIFECYCLE",
    "MAX_OPS",
    "SKILL_MANAGE_SCHEMA",
    "SKILL_SCANNER",
    "SUCCESS_LISTENERS",
    "TOOLSET",
    "TOOL_NAME",
    "SkillWriteEvent",
    "apply_pending_skill",
    "lock_path",
    "register_tools",
    "run_operations",
    "schema_overrides",
    "skill_manage",
]

log = logging.getLogger(__name__)

TOOL_NAME = "skill_manage"
TOOLSET = "skills"
MAX_OPS = 20
LOCK_TIMEOUT_S = 30.0
ARCHIVE_DIR = ".archive"
LOCKS_DIR = ".locks"
PREVIEW_CHARS = 400
ACTIONS = ("create", "patch", "edit", "delete", "write_file", "remove_file")
PATCH_TYPES = frozenset({"patch", "edit", "write_file", "remove_file"})

# Text fields, and what reads each one.
_SLOT_OWNER: Mapping[str, str] = {
    "content": "create, or patch as a whole-file rewrite",
    "new_string": "patch (together with old_string)",
    "file_content": "write_file",
}
_SLOTS_READ: Mapping[str, tuple[tuple[str, ...], str]] = {
    "create": (("content",), "'content'"),
    "edit": (("content",), "'content'"),
    "patch": (("content", "new_string"), "'old_string'/'new_string' or 'content'"),
    "write_file": (("file_content",), "'file_content'"),
    "delete": ((), "nowhere (delete takes no text)"),
    "remove_file": ((), "nowhere (remove_file takes no text)"),
}

# -- extension points ------------------------------------------------------------------------------


@dataclass(frozen=True)
class SkillWriteEvent:
    """What one successful call changed (for listeners)."""

    origin: str
    session_id: str | None
    actions: tuple[str, ...]
    skills: tuple[str, ...]
    created: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()
    archived: tuple[str, ...] = ()


type Listener = Callable[[SkillWriteEvent], object]

SUCCESS_LISTENERS: list[Listener] = []
"""Called after every successful call (P09-14 resets the skill review counter here)."""

LIFECYCLE: list[Listener] = []
"""The ``on_skill_lifecycle`` hook: a plain list until P13's hook bus takes it over."""

SKILL_SCANNER: Callable[[Path], str | None] | None = None
"""P13's content scanner: returns why a skill folder is refused, or None. Unset here."""


def _default_archiver(scope: Scope, folder: Path, name: str) -> Path:
    """The curator's archive (P09-18): one archive layout, name stamping and usage state for every
    path that archives a skill."""
    from indus.learning.curator.archive import manage_archiver

    return manage_archiver(scope, folder, name)


ARCHIVER: Callable[[Scope, Path, str], Path] = _default_archiver
"""Moves a skill folder into the archive and returns where it went (default: the curator's
``archive.manage_archiver``; tests may replace it)."""


# -- schema ---------------------------------------------------------------------------------------

_DESCRIPTION = (
    "Create and improve skills: saved procedures for recurring kinds of work. Give one action "
    "(action, name and its fields) or operations: a list applied all or nothing. Actions: "
    "create (full SKILL.md with frontmatter name and description; {where}), patch (old_string -> "
    "new_string in SKILL.md or file_path; prefer this; an empty new_string deletes), patch with "
    "content alone (rewrites SKILL.md whole; view it first), write_file / remove_file (support "
    "files under references/, templates/, scripts/ or assets/), delete. Write rules, not stories: "
    "state what to do and why, with no ticket numbers, dates or incident narratives."
)
_WHERE_DEFAULT = "new skills go to this profile's skills folder"

_OP_PROPERTIES: dict[str, Any] = {
    "action": {"type": "string", "enum": list(ACTIONS)},
    "name": {
        "type": "string",
        "description": "The skill (lowercase letters, digits, '.', '_', '-'; at most 64).",
    },
    "content": {
        "type": "string",
        "description": "create: the full SKILL.md. patch without old_string: the new SKILL.md.",
    },
    "category": {"type": "string", "description": "create: optional category folder."},
    "old_string": {"type": "string", "description": "patch: the exact text to replace."},
    "new_string": {"type": "string", "description": "patch: the replacement ('' deletes)."},
    "replace_all": {"type": "boolean", "description": "patch: replace every match."},
    "file_path": {
        "type": "string",
        "description": "Support file relative to the skill folder, e.g. references/api.md.",
    },
    "file_content": {"type": "string", "description": "write_file: the whole file."},
    "absorbed_into": {
        "type": "string",
        "description": "delete: the existing skill that now holds this one's content.",
    },
}

SKILL_MANAGE_SCHEMA: dict[str, Any] = {
    "description": _DESCRIPTION.format(where=_WHERE_DEFAULT),
    "parameters": {
        "type": "object",
        "properties": {
            **_OP_PROPERTIES,
            "operations": {
                "type": "array",
                "description": f"Up to {MAX_OPS} actions applied together; each names its skill.",
                "items": {
                    "type": "object",
                    "properties": _OP_PROPERTIES,
                    "required": ["action", "name"],
                },
            },
        },
        "required": [],
    },
}


def schema_overrides(scope: Scope | None) -> Mapping[str, Any] | None:
    """The description names the folder new skills go to in this profile."""
    if scope is None:
        return None
    try:
        where = SkillsSettings.from_scope(scope).write_root
    except Exception:
        log.debug("skill folder unknown for the schema", exc_info=True)
        return None
    return {"description": _DESCRIPTION.format(where=f"new skills go to {where}")}


# -- parsing --------------------------------------------------------------------------------------


class _Refused(Exception):
    def __init__(self, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.message = message
        self.extra = extra


@dataclass(frozen=True)
class Op:
    index: int
    action: str
    """Normalised: a whole-file ``patch`` becomes ``edit``."""
    name: str
    content: str | None = None
    category: str | None = None
    old_string: str | None = None
    new_string: str | None = None
    replace_all: bool = False
    file_path: str | None = None
    file_content: str | None = None
    absorbed_into: str | None = None

    @property
    def lock_key(self) -> str:
        return Path(self.name.strip().strip("/")).name

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"action": self.action, "name": self.name}
        for key in (
            "content",
            "category",
            "old_string",
            "new_string",
            "file_path",
            "file_content",
            "absorbed_into",
        ):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        if self.replace_all:
            data["replace_all"] = True
        return data

    def label(self) -> str:
        where = f" {self.file_path}" if self.file_path else ""
        return f"{self.action} {self.name}{where}"


def _str_or_none(raw: Mapping[str, Any], key: str, where: str) -> str | None:
    value = raw.get(key)
    if value is None or isinstance(value, str):
        return value
    raise _Refused(f"{where}{key} must be text")


def _parse_one(raw: Mapping[str, Any], index: int, where: str, default_name: str | None) -> Op:
    action = raw.get("action")
    if not isinstance(action, str) or action not in ACTIONS:
        raise _Refused(f"{where}action must be one of: {', '.join(ACTIONS)}")
    name = raw.get("name") or default_name
    if not isinstance(name, str) or not name.strip():
        raise _Refused(f"{where}name is required (the skill this {action} targets)")
    name = name.strip()
    reads, home = _SLOTS_READ[action]
    stray = [k for k in _SLOT_OWNER if k not in reads and raw.get(k) is not None]
    if stray:
        listed = " and ".join(f"'{k}' (that field is for {_SLOT_OWNER[k]})" for k in stray)
        raise _Refused(
            f"{where}{action} on {name!r} carries {listed}; move that text to {home}",
            misplaced=stray,
        )
    fields = {
        key: _str_or_none(raw, key, where)
        for key in (
            "content",
            "category",
            "old_string",
            "new_string",
            "file_path",
            "file_content",
            "absorbed_into",
        )
    }
    replace_all = raw.get("replace_all") is True
    content = fields["content"]
    if action == "patch":
        if content and (fields["old_string"] or fields["new_string"] is not None):
            raise _Refused(
                f"{where}patch takes either old_string/new_string (a targeted change) or content "
                "(a whole-file rewrite), not both"
            )
        if content:
            action = "edit"
        else:
            if not fields["old_string"]:
                raise _Refused(
                    f"{where}patch needs old_string: the exact text now in the file (view the "
                    "skill and copy it), or content to rewrite SKILL.md whole"
                )
            if fields["new_string"] is None:
                raise _Refused(f"{where}patch needs new_string (use '' to delete the text)")
    if action in ("create", "edit") and not content:
        raise _Refused(f"{where}{action} needs content: the full SKILL.md text")
    if action in ("write_file", "remove_file") and not fields["file_path"]:
        raise _Refused(f"{where}{action} needs file_path, e.g. references/notes.md")
    if action == "write_file" and fields["file_content"] is None:
        raise _Refused(f"{where}write_file needs file_content (the whole file)")
    if action == "create":
        problem = check_name(name) or check_category(fields["category"])
        if problem is not None:
            raise _Refused(f"{where}{problem}")
    elif check_name(Path(name.strip("/")).name) is not None:
        raise _Refused(f"{where}{check_name(Path(name.strip('/')).name)}")
    return Op(
        index=index,
        action=action,
        name=name,
        content=content,
        category=fields["category"] or None,
        old_string=fields["old_string"],
        new_string=fields["new_string"],
        replace_all=replace_all,
        file_path=fields["file_path"],
        file_content=fields["file_content"],
        absorbed_into=fields["absorbed_into"],
    )


def parse_operations(args: Mapping[str, Any]) -> list[Op]:
    """Every op of a call, shape-checked; raises ``_Refused`` on the first problem."""
    raw_ops = args.get("operations")
    default_name = args.get("name") if isinstance(args.get("name"), str) else None
    if raw_ops is None:
        if args.get("action") is None:
            raise _Refused("give an action (with name) or an operations list")
        return [_parse_one(args, 0, "", None)]
    if not isinstance(raw_ops, list) or not raw_ops:
        raise _Refused("operations must be a non-empty list")
    items = cast(list[object], raw_ops)
    if len(items) > MAX_OPS:
        raise _Refused(f"at most {MAX_OPS} operations per call; split the work")
    ops: list[Op] = []
    for i, item in enumerate(items):
        where = f"operations[{i}]: "
        if not isinstance(item, Mapping):
            raise _Refused(f"{where}each operation is an object")
        ops.append(_parse_one(cast(Mapping[str, Any], item), i, where, default_name))
    seen: dict[str, list[Op]] = {}
    for op in ops:
        earlier = seen.setdefault(op.lock_key, [])
        if op.action == "create" and earlier:
            raise _Refused(
                f"operations[{op.index}]: create of {op.name!r} must come before the other "
                "operations on it"
            )
        if any(e.action == "delete" for e in earlier):
            raise _Refused(
                f"operations[{op.index}]: {op.name!r} is deleted by an earlier operation; "
                "delete must be the last operation on a skill"
            )
        earlier.append(op)
    return ops


# -- locks ----------------------------------------------------------------------------------------


def lock_path(home: Path, name: str) -> Path:
    """The per-skill lock: keyed on the base name, so ``foo`` and ``cat/foo`` share one."""
    digest = hashlib.sha256(Path(name.strip().strip("/")).name.encode("utf-8")).hexdigest()
    return home / "skills" / LOCKS_DIR / f"{digest[:16]}.lock"


@contextlib.contextmanager
def _locked(home: Path, names: Sequence[str]) -> Iterator[None]:
    paths = sorted({lock_path(home, n) for n in names})
    if paths:
        paths[0].parent.mkdir(parents=True, exist_ok=True)
    with contextlib.ExitStack() as stack:
        for path in paths:
            stack.enter_context(FileLock(path, timeout=LOCK_TIMEOUT_S))
        yield


# -- the overlay ----------------------------------------------------------------------------------


def _inside(path: Path | str, folder: Path | str) -> bool:
    real = os.path.realpath(path)
    base = os.path.realpath(folder)
    return real == base or real.startswith(base + os.sep)


@dataclass
class _Target:
    name: str
    folder: Path
    record: SkillRecord | None
    created: bool = False
    files: dict[str, bytes | None] = field(default_factory=dict[str, bytes | None])
    original: dict[str, bytes | None] = field(default_factory=dict[str, bytes | None])
    deleted: bool = False
    archive: bool = False
    absorbed_into: str | None = None
    ops: list[Op] = field(default_factory=list[Op])


class _Plan:
    def __init__(
        self,
        scope: Scope,
        catalog: SkillCatalog,
        settings: SkillsSettings,
        *,
        session_id: str | None,
        review: bool,
        archive_deletes: bool,
    ) -> None:
        self.scope = scope
        self.catalog = catalog
        self.settings = settings
        self.session_id = session_id
        self.review = review
        self.archive_deletes = archive_deletes
        self.targets: dict[str, _Target] = {}
        self.order: list[str] = []
        self.batch = False
        self.usage = usage_for(scope)

    # -- lookup -----------------------------------------------------------------------------------

    def roots(self) -> list[Path]:
        return [root for _tier, root in self.settings.roots()]

    def _target(self, op: Op) -> _Target:
        key = op.lock_key
        found = self.targets.get(key)
        if found is not None:
            return found
        try:
            rec = self.catalog.resolve(op.name)
        except SkillError as exc:
            raise _Refused(f"{exc}; skills_list shows the skills you can change") from None
        if rec.plugin is not None:
            raise _Refused(f"{rec.name!r} comes from a plugin and cannot be changed here")
        if not any(_inside(rec.skill_dir, root) for root in self.roots()):
            raise _Refused(f"{rec.name!r} resolves outside the skill folders; it is not changed")
        target = _Target(name=rec.name, folder=rec.skill_dir, record=rec)
        self.targets[key] = target
        self.order.append(key)
        return target

    def _create(self, op: Op) -> _Target:
        name = op.name
        if any(r.name == name for r in self.catalog.records):
            where = next(r.skill_dir for r in self.catalog.records if r.name == name)
            raise _Refused(f"a skill named {name!r} already exists at {where}")
        root = self.settings.write_root
        folder = root / (op.category or "") / name
        if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
            raise _Refused(f"cannot create {name!r}: {folder} exists and is not a folder")
        if folder.is_dir() and any(folder.iterdir()):
            raise _Refused(f"cannot create {name!r}: {folder} already holds files")
        if not _inside(folder.parent, root) and folder.parent.exists():
            raise _Refused(f"cannot create {name!r}: {folder.parent} leads outside {root}")
        target = _Target(name=name, folder=folder, record=None, created=True)
        self.targets[op.lock_key] = target
        self.order.append(op.lock_key)
        return target

    # -- reading through the overlay --------------------------------------------------------------

    def _path(self, target: _Target, rel: str) -> Path:
        path = target.folder / rel
        if not target.created and not _inside(path, target.folder):
            raise _Refused(f"{rel!r} leads outside the folder of {target.name!r}; refused")
        return path

    def _disk(self, target: _Target, rel: str) -> bytes | None:
        if rel not in target.original:
            if target.created:
                target.original[rel] = None
            else:
                path = self._path(target, rel)
                try:
                    target.original[rel] = path.read_bytes() if path.is_file() else None
                except OSError as exc:
                    raise _Refused(f"could not read {rel!r}: {exc.strerror or exc}") from None
        return target.original[rel]

    def read(self, target: _Target, rel: str) -> bytes | None:
        if rel in target.files:
            return target.files[rel]
        return self._disk(target, rel)

    def _guard_read(self, target: _Target, rel: str, action: str) -> None:
        """Review fork: changing existing content needs a fresh view of exactly that content."""
        if not self.review or rel in target.files or target.created:
            return
        current = self._disk(target, rel)
        if current is None:
            return
        if READS.fresh(self.session_id, target.name, rel, current):
            return
        hint = (
            f"skill_view(name='{target.name}')"
            if rel == SKILL_FILE
            else f"skill_view(name='{target.name}', file_path='{rel}')"
        )
        raise _Refused(
            f"{action} of {rel} in {target.name!r} refused: its current content was not viewed in "
            f"this review. Call {hint}, then retry with the text it returns.",
            view_required=True,
        )

    # -- the actions ------------------------------------------------------------------------------

    def apply(self, op: Op) -> None:
        where = f"operations[{op.index}] ({op.label()}): " if self.batch else ""
        try:
            getattr(self, f"_do_{op.action}")(op)
        except _Refused as exc:
            raise _Refused(where + exc.message, failed_index=op.index, **exc.extra) from None
        except SkillFormatError as exc:
            raise _Refused(where + str(exc), failed_index=op.index) from None

    def _do_create(self, op: Op) -> None:
        target = self._create(op)
        target.ops.append(op)
        target.files[SKILL_FILE] = (op.content or "").encode("utf-8")

    def _do_edit(self, op: Op) -> None:
        target = self._target(op)
        self._live(target, op)
        self._guard_read(target, SKILL_FILE, "rewrite")
        target.ops.append(op)
        target.files[SKILL_FILE] = (op.content or "").encode("utf-8")

    def _do_patch(self, op: Op) -> None:
        target = self._target(op)
        self._live(target, op)
        rel = SKILL_FILE
        if op.file_path and op.file_path.strip() not in (SKILL_FILE, f"./{SKILL_FILE}"):
            rel = check_support_path(op.file_path).as_posix()
        self._path(target, rel)
        current = self.read(target, rel)
        if current is None:
            raise _Refused(f"{target.name!r} has no file {rel}")
        self._guard_read(target, rel, "patch")
        try:
            text = current.decode("utf-8")
        except UnicodeDecodeError:
            raise _Refused(f"{rel} is not text; replace it with write_file") from None
        old = op.old_string or ""
        found = text.count(old)
        if found == 0:
            preview = text[:PREVIEW_CHARS] + ("…" if len(text) > PREVIEW_CHARS else "")
            raise _Refused(
                f"old_string was not found in {rel}; copy it exactly from the file",
                file_preview=preview,
            )
        if found > 1 and not op.replace_all:
            raise _Refused(
                f"old_string occurs {found} times in {rel}; include more of the surrounding text "
                "or set replace_all"
            )
        new = op.new_string or ""
        text = text.replace(old, new) if op.replace_all else text.replace(old, new, 1)
        target.ops.append(op)
        target.files[rel] = text.encode("utf-8")

    def _do_write_file(self, op: Op) -> None:
        target = self._target(op)
        self._live(target, op)
        rel = check_support_path(op.file_path or "").as_posix()
        data = (op.file_content or "").encode("utf-8")
        check_support_size(data)
        self._path(target, rel)
        self._guard_read(target, rel, "write_file over")
        target.ops.append(op)
        target.files[rel] = data

    def _do_remove_file(self, op: Op) -> None:
        target = self._target(op)
        self._live(target, op)
        rel = check_support_path(op.file_path or "").as_posix()
        self._path(target, rel)
        if self.read(target, rel) is None:
            raise _Refused(f"{target.name!r} has no file {rel}")
        self._guard_read(target, rel, "remove_file of")
        target.ops.append(op)
        target.files[rel] = None

    def _do_delete(self, op: Op) -> None:
        target = self._target(op)
        self._live(target, op)
        name = target.name
        if target.created:
            raise _Refused(f"{name!r} is created in this same call; leave it out instead")
        if self.usage.is_pinned(name):
            raise _Refused(
                f"{name!r} is pinned, so it cannot be deleted (patches are still allowed). The "
                f"user can run `indus curator unpin {name}` first."
            )
        if self.review:
            reason = self._review_delete_problem(target)
            if reason is not None:
                raise _Refused(f"the review may not delete {name!r}: {reason}")
        if target.folder.is_symlink():
            raise _Refused(f"{name!r} is a link to another folder; it is not deleted here")
        for root in self.roots():
            if os.path.realpath(target.folder) == os.path.realpath(root):
                raise _Refused(f"{name!r} resolves to a skills root; refused")
        absorbed = (op.absorbed_into or "").strip()
        if absorbed:
            if Path(absorbed).name == Path(name).name:
                raise _Refused("absorbed_into must name another skill")
            other = self.targets.get(Path(absorbed).name)
            if (other is None or other.deleted) and self.catalog.find(absorbed) is None:
                raise _Refused(
                    f"absorbed_into={absorbed!r} does not exist; create or patch it first"
                )
        target.ops.append(op)
        target.deleted = True
        target.archive = self.archive_deletes
        target.absorbed_into = absorbed or None

    def _review_delete_problem(self, target: _Target) -> str | None:
        rec = target.record
        if rec is not None and rec.tier == TIER_EXTERNAL:
            return "it lives in skills.external_dirs, which the review does not manage"
        if not self.usage.is_curator_managed(target.name):
            return (
                "only skills the review created itself (curator-managed) may be removed by it; "
                "this one was written by hand, by /learn or ships with Indus Agent"
            )
        return None

    def _live(self, target: _Target, op: Op) -> None:
        if target.deleted:
            raise _Refused(f"{target.name!r} is deleted earlier in this call")

    # -- final checks -----------------------------------------------------------------------------

    def check(self) -> None:
        for key in self.order:
            target = self.targets[key]
            if target.deleted or SKILL_FILE not in target.files:
                continue
            data = target.files[SKILL_FILE]
            if data is None:
                raise _Refused(f"SKILL.md of {target.name!r} cannot be removed")
            try:
                check_skill_text(data.decode("utf-8"), expected_name=target.name)
            except SkillFormatError as exc:
                verb = "created" if target.created else "changed"
                raise _Refused(f"{target.name!r} would be invalid once {verb}: {exc}") from None


# -- committing -----------------------------------------------------------------------------------


@dataclass
class _Undo:
    files: list[tuple[Path, bytes | None]] = field(default_factory=list[tuple[Path, bytes | None]])
    moves: list[tuple[Path, Path]] = field(default_factory=list[tuple[Path, Path]])
    made: list[Path] = field(default_factory=list[Path])

    def run(self) -> None:
        for src, dest in reversed(self.moves):
            try:
                shutil.move(str(dest), str(src))
            except OSError as exc:
                log.error("could not move %s back to %s: %s", dest, src, exc)
        for path, data in reversed(self.files):
            try:
                if data is None:
                    path.unlink(missing_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write_bytes(path, data)
            except OSError as exc:
                log.error("could not restore %s: %s", path, exc)
        for folder in reversed(self.made):
            _prune(folder, stop=None)


def _prune(folder: Path, stop: Path | None) -> None:
    """Remove ``folder`` and its empty parents while empty, never ``stop`` or above."""
    current = folder
    while stop is None or (current != stop and _inside(current, stop)):
        try:
            current.rmdir()
        except OSError:
            return
        if stop is None:
            return
        current = current.parent


def _commit(plan: _Plan) -> tuple[_Undo, dict[str, Path]]:
    """Write every change; on failure undo what was written and raise."""
    undo = _Undo()
    archived: dict[str, Path] = {}
    aside: list[Path] = []
    try:
        for key in plan.order:
            target = plan.targets[key]
            if target.deleted:
                continue
            if target.created:
                missing = [p for p in (target.folder, *target.folder.parents) if not p.exists()]
                target.folder.mkdir(parents=True, exist_ok=True)
                undo.made.extend(reversed(missing))
            for rel, data in target.files.items():
                path = target.folder / rel
                before = target.original.get(rel)
                if data is None:
                    if path.is_file() or path.is_symlink():
                        path.unlink()
                        undo.files.append((path, before))
                        _prune(path.parent, stop=target.folder)
                    continue
                if not path.parent.is_dir():
                    made = [p for p in (path.parent, *path.parent.parents) if not p.exists()]
                    path.parent.mkdir(parents=True, exist_ok=True)
                    undo.made.extend(reversed(made))
                atomic_write_bytes(path, data, mode=None if before is not None else 0o644)
                undo.files.append((path, before))
        for key in plan.order:
            target = plan.targets[key]
            if not target.deleted:
                continue
            if target.archive:
                dest = ARCHIVER(plan.scope, target.folder, target.name)
                undo.moves.append((target.folder, dest))
                archived[target.name] = dest
            else:
                dest = target.folder.with_name(
                    f".{target.folder.name}.deleting-{secrets.token_hex(3)}"
                )
                os.replace(target.folder, dest)
                undo.moves.append((target.folder, dest))
                aside.append(dest)
    except BaseException:
        undo.run()
        raise
    for folder in aside:
        try:
            shutil.rmtree(folder)
        except OSError as exc:
            log.warning("deleted skill left at %s: %s", folder, exc)
    for key in plan.order:
        target = plan.targets[key]
        if target.deleted and not target.archive:
            root = next((r for r in plan.roots() if _inside(target.folder, r)), None)
            if root is not None:
                _prune(target.folder.parent, stop=root)
    return undo, archived


# -- running a call -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    ok: bool
    body: dict[str, Any]
    diffs: tuple[tuple[str, str, str], ...] = ()
    """``(path, before, after)`` text of each changed file (for approval output)."""


def _ledger_action(target: _Target) -> str:
    if target.created:
        return "create"
    if target.deleted:
        return "archive" if target.archive else "delete"
    kinds = {op.action for op in target.ops}
    return kinds.pop() if len(kinds) == 1 else "patch"


def _side_effects(
    plan: _Plan,
    origin: str,
    before: Mapping[str, list[FileRef] | None],
    archived: Mapping[str, Path],
    body: dict[str, Any],
) -> None:
    scope = plan.scope
    targets = [plan.targets[k] for k in plan.order]
    try:
        ledger = SkillLedger.for_scope(scope)
        for target in targets:
            prior = before.get(target.name)
            evidence: dict[str, Any] = {"session_id": plan.session_id}
            paths = [op.file_path for op in target.ops if op.file_path]
            if len(paths) == 1:
                evidence["file_path"] = paths[0]
            if target.deleted:
                evidence["absorbed_into"] = target.absorbed_into
                evidence["archived"] = target.archive
            ledger.record(
                _ledger_action(target),
                target.name,
                before=prior if prior is not None else [],
                after_dir=archived.get(target.name)
                if target.archive
                else (None if target.deleted else target.folder),
                evidence=evidence,
            )
    except Exception:
        log.warning("skill ledger not written", exc_info=True)
    try:
        clear_list_cache(scope)
        clear_scan_cache()
    except Exception:
        log.warning("skill caches not cleared", exc_info=True)
    try:
        usage = usage_for(scope)
        for target in targets:
            if target.created:
                usage.record_created(target.name, origin=origin)
            elif target.deleted and target.archive:
                usage.set_state(target.name, "archived")
            elif target.deleted:
                usage.forget(target.name)
            elif any(op.action in PATCH_TYPES for op in target.ops):
                usage.bump_patch(target.name)
    except Exception:
        log.warning("skill usage not recorded", exc_info=True)
    lint: list[dict[str, str]] = []
    notes: list[str] = []
    try:
        shipped = bundled_names(scope.home / "skills")
        for target in targets:
            if target.deleted:
                continue
            data = target.files.get(SKILL_FILE)
            text = data.decode("utf-8") if data else (target.folder / SKILL_FILE).read_text("utf-8")
            for finding in lint_skill(text, target.folder):
                lint.append({"skill": target.name, **finding.as_dict()})
            if not target.created and target.name in shipped:
                notes.append(
                    f"{target.name!r} ships with Indus Agent; it now counts as edited, so "
                    "`indus skills sync` will leave it as it is"
                )
    except Exception:
        log.warning("skill lint failed", exc_info=True)
    if lint:
        body["lint"] = lint
        body["lint_hint"] = "Saved. These are advice, not errors; patch the skill to address them."
    if notes:
        body["notes"] = notes
    event = SkillWriteEvent(
        origin=origin,
        session_id=plan.session_id,
        actions=tuple(op.action for t in targets for op in t.ops),
        skills=tuple(t.name for t in targets),
        created=tuple(t.name for t in targets if t.created),
        deleted=tuple(t.name for t in targets if t.deleted and not t.archive),
        archived=tuple(t.name for t in targets if t.archive),
    )
    for listener in (*LIFECYCLE, *SUCCESS_LISTENERS):
        try:
            listener(event)
        except Exception:
            log.warning("a skill write listener failed", exc_info=True)


def _scan(plan: _Plan) -> str | None:
    if not bool(skills_setting(plan.scope, "skills.scan_agent_created")):
        return None
    scanner = SKILL_SCANNER
    if scanner is None:
        log.debug("skills.scan_agent_created is on but no scanner is registered; scan skipped")
        return None
    for key in plan.order:
        target = plan.targets[key]
        if target.deleted:
            continue
        try:
            problem = scanner(target.folder)
        except Exception:
            log.warning("skill scan of %s failed", target.folder, exc_info=True)
            continue
        if problem:
            return f"{target.name!r} was refused by the content scan: {problem}"
    return None


def _diffs(plan: _Plan) -> tuple[tuple[str, str, str], ...]:
    out: list[tuple[str, str, str]] = []
    for key in plan.order:
        target = plan.targets[key]
        if target.deleted:
            continue
        for rel, data in target.files.items():
            old = target.original.get(rel)
            try:
                before = old.decode("utf-8") if old else ""
                after = data.decode("utf-8") if data else ""
            except UnicodeDecodeError:
                continue
            out.append((f"{target.name}/{rel}", before, after))
    return tuple(out)


def _capture_before(plan: _Plan) -> dict[str, list[FileRef] | None]:
    """The ledger's before-state of every touched skill (telemetry: failures give None)."""
    try:
        ledger = SkillLedger.for_scope(plan.scope)
    except Exception:
        log.warning("skill ledger unavailable; this change is not recorded", exc_info=True)
        return {}
    out: dict[str, list[FileRef] | None] = {}
    for key in plan.order:
        target = plan.targets[key]
        out[target.name] = [] if target.created else ledger.capture_quietly(target.folder)
    return out


def run_operations(
    scope: Scope,
    ops: Sequence[Op],
    *,
    session_id: str | None = None,
    origin: str | None = None,
    replay: bool = False,
) -> Outcome:
    """Apply already-parsed ops under the locks (no approval gate). ``replay`` marks a person's
    approval of a staged write: the review fork's read guard does not apply then."""
    who = origin or pending.current_origin()
    review = who == "background_review" and not replay
    try:
        settings = SkillsSettings.from_scope(scope)
        with _locked(scope.home, [op.lock_key for op in ops]):
            catalog = load_catalog(scope, settings=settings)
            plan = _Plan(
                scope,
                catalog,
                settings,
                session_id=session_id,
                review=review,
                archive_deletes=who == "background_review",
            )
            plan.batch = len(ops) > 1
            for op in ops:
                plan.apply(op)
            plan.check()
            before = _capture_before(plan)
            try:
                undo, archived = _commit(plan)
            except OSError as exc:
                return Outcome(False, {"error": f"nothing was saved: {exc.strerror or exc}"})
            problem = _scan(plan)
            if problem is not None:
                undo.run()
                return Outcome(False, {"error": problem + "; nothing was saved"})
            body = _success_body(plan, archived)
            _side_effects(plan, who, before, archived, body)
            return Outcome(True, body, _diffs(plan))
    except _Refused as exc:
        return Outcome(False, {"error": exc.message, **exc.extra})
    except TimeoutError as exc:
        return Outcome(False, {"error": f"the skill is being changed elsewhere; retry ({exc})"})


def _success_body(plan: _Plan, archived: Mapping[str, Path]) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for key in plan.order:
        target = plan.targets[key]
        for op in target.ops:
            row: dict[str, Any] = {"action": op.action, "name": target.name}
            if op.file_path:
                row["file_path"] = op.file_path
            if op.action == "create":
                row["path"] = str(target.folder)
            if op.action == "delete" and target.name in archived:
                row["archived_to"] = str(archived[target.name])
            results.append(row)
    count = len(results)
    names = ", ".join(plan.targets[k].name for k in plan.order)
    return {
        "success": True,
        "message": f"{count} change{'s' if count != 1 else ''} saved ({names}).",
        "results": results,
    }


def _summary(ops: Sequence[Op]) -> str:
    return "; ".join(op.label() for op in ops)


def skill_manage(
    args: Mapping[str, Any], *, scope: Scope | None = None, session_id: str | None = None
) -> str:
    """The tool handler (see the module notes)."""
    if scope is None:
        return ToolResult.error("skill_manage needs a profile scope").text
    try:
        ops = parse_operations(args)
    except _Refused as exc:
        return ToolResult.error(exc.message, **exc.extra).text
    origin = pending.current_origin()
    decision = pending.decide(
        "skills", gate=pending.gate_on(scope, "skills"), origin=origin, can_ask=False
    )
    if decision != "allow":
        try:
            record = pending.PendingStore(scope.home, scope.clock).stage(
                "skills",
                {"operations": [op.to_json() for op in ops], "session_id": session_id},
                summary=_summary(ops),
                origin=origin,
            )
        except Exception as exc:
            log.error("staging a skill write failed", exc_info=True)
            return ToolResult.error(f"the write could not be held for approval ({exc})").text
        return json.dumps(
            {
                "success": True,
                "staged": True,
                "pending_id": record.id,
                "message": pending.staged_message("skills"),
            },
            ensure_ascii=False,
        )
    outcome = run_operations(scope, ops, session_id=session_id, origin=origin)
    if not outcome.ok:
        message = str(outcome.body.get("error", "refused"))
        extra = {k: v for k, v in outcome.body.items() if k != "error"}
        return ToolResult.error(message, **extra).text
    return json.dumps(outcome.body, ensure_ascii=False)


def apply_pending_skill(scope: Scope, record: pending.PendingWrite) -> pending.Applied:
    """Replay a staged skill write for ``indus skills approve`` / ``/skills approve`` (the full
    diff; ``pending.render_approval`` cuts it for chat surfaces)."""
    payload = record.payload
    try:
        ops = parse_operations({"operations": copy.deepcopy(payload.get("operations"))})
    except _Refused as exc:
        return pending.Applied(False, f"the staged record is not valid: {exc.message}")
    session = payload.get("session_id")
    outcome = run_operations(
        scope,
        ops,
        session_id=session if isinstance(session, str) else None,
        origin=record.origin,
        replay=True,
    )
    if not outcome.ok:
        return pending.Applied(False, str(outcome.body.get("error", "refused")))
    diff = "".join(
        pending.skill_diff(before, after, path, surface="cli")
        for path, before, after in outcome.diffs
    )
    return pending.Applied(True, str(outcome.body.get("message", "")), diff=diff)


def register_tools(registry: ToolRegistry) -> None:
    registry.register(
        TOOL_NAME,
        TOOLSET,
        SKILL_MANAGE_SCHEMA,
        skill_manage,
        schema_overrides=schema_overrides,
        traits=ToolTraits(mutating=True),
    )
