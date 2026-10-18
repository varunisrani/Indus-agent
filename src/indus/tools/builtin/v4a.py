# pyright: strict
"""Multi-file patches in the V4A envelope, applied all or nothing (P05-20).

The envelope is a third-party format (published with OpenAI's ``apply_patch`` tool). Grammar as
we accept it::

    *** Begin Patch
    *** Add File: <path>             new file; every body line starts with "+"
    *** Delete File: <path>          no body
    *** Update File: <path>          then hunks
    *** Move to: <new path>          optional, right after an Update header: rename as well
    @@ <anchor text>                 starts a hunk; the anchor names a line above the change
    @@ <narrower anchor>             (several @@ lines narrow the place step by step; "@@" alone
     context line                     or "@@ text @@" work too)
    -removed line
    +added line
    *** End of File                  optional: the hunk sits at the end of the file
    *** Move File: <a> -> <b>        a plain rename (accepted as well)
    *** End Patch

Lines before ``Begin`` and after ``End`` (code fences, a heredoc wrapper) are ignored. A patch
without ``End Patch`` is refused: a reply cut off mid-patch must never half-apply.

Applying: every touched file is read once and each section is worked out in memory, in order
(a later section sees what earlier ones did). A hunk is found by its context and removed lines,
from the place the previous hunk ended (after its anchors), comparing lines exactly, then
ignoring trailing spaces, then ignoring surrounding spaces, then with punctuation normalised;
when it is not found there, the whole file is tried. A hunk whose result is already in the file
is skipped with a note. Before the first write, every path is offered to the write guards
(``PatchFiles.refusal``) and the fail-closed parse rule (``lint.gate``); any refusal stops the
whole patch. Writes then run in order, deletes last; if one fails, everything already done is
put back from the copies read at the start. Existing files keep their line ending (LF or CRLF)
and whether they end with a newline; added files end with one.

File access goes through ``PatchFiles``; the patch tool (P05-19) adapts the file-ops layer and
the write guards to it and calls ``run_patch_mode`` for ``mode: "patch"``.
"""

from __future__ import annotations

import copy
import difflib
import json
import logging
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from indus.tools.builtin.exec_host import CancelLike, ExecHost
from indus.tools.builtin.lint import gate, lint_edit

__all__ = [
    "PATCH_MODE_PROPERTIES",
    "FileOp",
    "Hunk",
    "Patch",
    "PatchFiles",
    "PatchOutcome",
    "PatchSyntaxError",
    "WriteAction",
    "apply_patch",
    "parse",
    "run_patch_mode",
    "wants_v4a",
    "with_patch_mode",
]

log = logging.getLogger(__name__)

type OpKind = Literal["add", "delete", "update", "move"]
type WriteAction = Literal["write", "delete"]

_FLAGS = re.IGNORECASE
_BEGIN = re.compile(r"^\*\*\*\s*begin\s+patch\s*$", _FLAGS)
_END = re.compile(r"^\*\*\*\s*end\s+patch\s*$", _FLAGS)
_HEADER = re.compile(r"^\*\*\*\s*(add|delete|update)\s+file\s*:\s*(.*?)\s*$", _FLAGS)
_MOVE_TO = re.compile(r"^\*\*\*\s*move\s+to\s*:\s*(.*?)\s*$", _FLAGS)
_MOVE_FILE = re.compile(r"^\*\*\*\s*move\s+file\s*:\s*(.*?)\s*->\s*(.*?)\s*$", _FLAGS)
_END_OF_FILE = re.compile(r"^\*\*\*\s*end\s+of\s+file\s*$", _FLAGS)
_KINDS: dict[str, OpKind] = {"add": "add", "delete": "delete", "update": "update"}

GRAMMAR_HINT = (
    "Expected: '*** Begin Patch', then sections '*** Add File: <path>' (lines starting with '+'), "
    "'*** Delete File: <path>', '*** Update File: <path>' (optional '*** Move to: <path>', then "
    "hunks: '@@ <anchor>' and lines starting with ' ', '-' or '+'), then '*** End Patch'."
)


class PatchSyntaxError(ValueError):
    """The patch text is not a well-formed envelope; ``line`` is 1-based."""

    def __init__(self, line: int, message: str) -> None:
        super().__init__(f"line {line}: {message}")
        self.line = line
        self.reason = message


@dataclass(frozen=True, slots=True)
class Hunk:
    """One change inside an Update section."""

    line: int
    anchors: tuple[str, ...] = ()
    body: tuple[tuple[str, str], ...] = ()  # (" " | "-" | "+", text)
    at_end: bool = False

    @property
    def old(self) -> list[str]:
        return [text for mark, text in self.body if mark != "+"]

    @property
    def new(self) -> list[str]:
        return [text for mark, text in self.body if mark != "-"]

    @property
    def changes(self) -> bool:
        return any(mark != " " for mark, _ in self.body)


@dataclass(frozen=True, slots=True)
class FileOp:
    kind: OpKind
    path: str
    line: int
    new_path: str | None = None
    hunks: tuple[Hunk, ...] = ()
    content: tuple[str, ...] = ()  # Add only


@dataclass(frozen=True, slots=True)
class Patch:
    ops: tuple[FileOp, ...]

    @property
    def has_changes(self) -> bool:
        return any(
            op.kind != "update" or op.new_path or any(h.changes for h in op.hunks)
            for op in self.ops
        )


# -- parsing ----------------------------------------------------------------------------------


@dataclass
class _Section:
    kind: OpKind
    path: str
    line: int
    new_path: str | None = None
    hunks: list[Hunk] = field(default_factory=lambda: [])
    content: list[str] = field(default_factory=lambda: [])
    hunk: Hunk | None = None
    blanks: int = 0  # blank lines seen and not yet placed

    def close_hunk(self) -> None:
        if self.hunk is not None and self.hunk.body:
            self.hunks.append(self.hunk)
        self.hunk = None

    def place_blanks(self) -> None:
        if not self.blanks:
            return
        if self.kind == "add":
            self.content.extend([""] * self.blanks)
        elif self.kind == "update":
            hunk = self.hunk or Hunk(self.line)
            self.hunk = Hunk(hunk.line, hunk.anchors, hunk.body + ((" ", ""),) * self.blanks)
        self.blanks = 0


def _finish(section: _Section) -> FileOp:
    section.blanks = 0  # blank lines before the next header separate sections
    section.close_hunk()
    if section.kind == "update" and not section.hunks and not section.new_path:
        raise PatchSyntaxError(section.line, f"'*** Update File: {section.path}' has no hunks")
    return FileOp(
        section.kind,
        section.path,
        section.line,
        section.new_path,
        tuple(section.hunks),
        tuple(section.content),
    )


def _anchor_text(line: str) -> str:
    text = line[2:].strip()
    if text.endswith("@@"):
        text = text[:-2].strip()
    return text


def _body_line(section: _Section, line: str, number: int) -> None:
    if section.kind == "add":
        if not line.startswith("+"):
            raise PatchSyntaxError(
                number, f"every line of the added file {section.path} must start with '+'"
            )
        section.place_blanks()
        section.content.append(line[1:])
        return
    if section.kind in ("delete", "move"):
        raise PatchSyntaxError(number, f"'{section.kind}' of {section.path} takes no body lines")
    if line.startswith("@@"):
        section.blanks = 0
        anchor = _anchor_text(line)
        if section.hunk is not None and not section.hunk.body:
            hunk = section.hunk
            anchors = hunk.anchors + ((anchor,) if anchor else ())
            section.hunk = Hunk(hunk.line, anchors)
            return
        section.close_hunk()
        section.hunk = Hunk(number, (anchor,) if anchor else ())
        return
    if line.startswith("\\"):
        return  # "\ No newline at end of file" from diff tools
    if line[0] not in " -+":
        raise PatchSyntaxError(
            number,
            f"a line in the update of {section.path} must start with ' ' (context), '-' "
            f"(removed), '+' (added) or '@@' (new hunk); got {line[:60]!r}",
        )
    section.place_blanks()
    hunk = section.hunk or Hunk(number)
    section.hunk = Hunk(hunk.line, hunk.anchors, hunk.body + ((line[0], line[1:]),))


def parse(text: str) -> Patch:
    """Read a V4A envelope; ``PatchSyntaxError`` names the first bad line."""
    lines = [raw.removesuffix("\r") for raw in text.split("\n")]
    begin = next((i for i, line in enumerate(lines) if _BEGIN.match(line.strip())), None)
    if begin is None:
        first = next((i for i, line in enumerate(lines) if line.strip()), 0)
        raise PatchSyntaxError(first + 1, "the patch must start with '*** Begin Patch'")
    end = next((i for i in range(begin + 1, len(lines)) if _END.match(lines[i].strip())), None)
    if end is None:
        raise PatchSyntaxError(
            len(lines),
            "the patch has no '*** End Patch' line (was it cut off?); nothing was applied",
        )
    ops: list[FileOp] = []
    section: _Section | None = None
    for index in range(begin + 1, end):
        line, number = lines[index], index + 1
        header = _HEADER.match(line)
        move_file = _MOVE_FILE.match(line)
        if header or move_file:
            if section is not None:
                ops.append(_finish(section))
            if header:
                path = header.group(2)
                if not path:
                    raise PatchSyntaxError(number, f"'*** {header.group(1)} File:' needs a path")
                section = _Section(_KINDS[header.group(1).lower()], path, number)
            else:
                assert move_file is not None
                source, target = move_file.group(1), move_file.group(2)
                if not source or not target:
                    raise PatchSyntaxError(number, "'*** Move File:' needs '<from> -> <to>'")
                section = _Section("move", source, number, new_path=target)
            continue
        move_to = _MOVE_TO.match(line)
        if move_to:
            if section is None or section.kind != "update" or section.hunk or section.hunks:
                raise PatchSyntaxError(
                    number, "'*** Move to:' must come right after an '*** Update File:' line"
                )
            if section.new_path is not None or not move_to.group(1):
                raise PatchSyntaxError(number, "'*** Move to:' needs one destination path")
            section.new_path = move_to.group(1)
            continue
        if _END_OF_FILE.match(line):
            if section is None or section.kind != "update" or section.hunk is None:
                raise PatchSyntaxError(number, "'*** End of File' must close a hunk of an update")
            hunk = section.hunk
            section.blanks = 0
            section.hunk = Hunk(hunk.line, hunk.anchors, hunk.body, at_end=True)
            section.close_hunk()
            continue
        if line.startswith("***"):
            raise PatchSyntaxError(number, f"unknown patch line {line[:60]!r}. {GRAMMAR_HINT}")
        if not line:
            if section is not None:
                section.blanks += 1
            continue
        if section is None:
            raise PatchSyntaxError(
                number, f"text before the first file section: {line[:60]!r}. {GRAMMAR_HINT}"
            )
        _body_line(section, line, number)
    if section is not None:
        ops.append(_finish(section))
    if not ops:
        raise PatchSyntaxError(begin + 1, "the patch has no file sections")
    return Patch(tuple(ops))


# -- locating hunks ---------------------------------------------------------------------------

_PUNCT = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‚": "'",
        "‛": "'",
        "“": '"',
        "”": '"',
        "„": '"',
        "‟": '"',
        "‐": "-",
        "‑": "-",
        "‒": "-",
        "–": "-",
        "—": "-",
        "―": "-",
        "−": "-",
        "…": "...",
    }
)


def _loose(text: str) -> str:
    text = text.translate(_PUNCT)
    text = "".join(" " if unicodedata.category(ch) == "Zs" else ch for ch in text)
    return " ".join(text.split())


_PASSES: tuple[tuple[str, Callable[[str], str]], ...] = (
    ("exact", lambda s: s),
    ("trailing spaces ignored", str.rstrip),
    ("surrounding spaces ignored", str.strip),
    ("punctuation and spacing normalised", _loose),
)


def _find_block(
    lines: Sequence[str], block: Sequence[str], start: int, *, at_end: bool
) -> tuple[int, str] | None:
    size = len(block)
    last = len(lines) - size
    if last < start:
        return None
    for label, norm in _PASSES:
        want = [norm(b) for b in block]
        if at_end:
            candidates: Sequence[int] = [last]
        else:
            candidates = range(start, last + 1)
        for i in candidates:
            if all(norm(lines[i + j]) == want[j] for j in range(size)):
                return i, label
    return None


def _anchor_tests(anchor: str) -> tuple[Callable[[str], bool], ...]:
    want = anchor.strip()
    return (
        lambda line: line == anchor,
        lambda line: line.strip() == want,
        lambda line: _loose(line) == _loose(want),
        lambda line: want in line,
    )


def _find_anchor(lines: Sequence[str], anchor: str, start: int) -> tuple[int, int] | None:
    """(first line matching ``anchor`` from ``start``, how many lines match it there), trying
    stricter comparisons first and the whole file when nothing after ``start`` matches."""
    for test in _anchor_tests(anchor):
        for begin in (start, 0):
            hits = [i for i in range(begin, len(lines)) if test(lines[i])]
            if hits:
                return hits[0], len(hits)
    return None


class _Refused(Exception):
    """The patch cannot be applied; nothing was written."""


def _merge(hunk: Hunk, matched: Sequence[str]) -> list[str]:
    """The hunk's new lines, with context lines kept as the file has them (a loose match must
    not rewrite the lines around the change)."""
    out: list[str] = []
    index = 0
    for mark, text in hunk.body:
        if mark == "+":
            out.append(text)
            continue
        if mark == " ":
            out.append(matched[index])
        index += 1
    return out


def _apply_hunks(path: str, text: str, hunks: Sequence[Hunk], notes: list[str]) -> str:
    eol = "\r\n" if "\r\n" in text else "\n"
    ends_with_newline = text.endswith("\n") or not text
    lines = text.split("\n")
    if text.endswith("\n") or not text:
        lines.pop()
    if eol == "\r\n":
        lines = [line.removesuffix("\r") for line in lines]
    cursor = 0
    for number, hunk in enumerate(hunks, 1):
        if not hunk.changes:
            continue
        place = cursor
        repeats = 0
        for anchor in hunk.anchors:
            found = _find_anchor(lines, anchor, place)
            if found is None:
                raise _Refused(
                    f"{path}: hunk {number} (patch line {hunk.line}): the anchor {anchor!r} "
                    "is not in the file"
                )
            place, repeats = found[0] + 1, found[1]
        old, new = hunk.old, hunk.new
        if not old:
            if repeats > 1 and not hunk.at_end:
                # Only added lines: the anchor alone says where, so it has to be unique.
                raise _Refused(
                    f"{path}: hunk {number} (patch line {hunk.line}) adds lines below the anchor "
                    f"{hunk.anchors[-1]!r}, which appears {repeats} times; add a context line "
                    "or a more specific anchor"
                )
            at = place if hunk.anchors and not hunk.at_end else len(lines)
            lines[at:at] = new
            cursor = at + len(new)
            continue
        hit = _find_block(lines, old, place, at_end=hunk.at_end)
        if hit is None and place > 0:
            hit = _find_block(lines, old, 0, at_end=hunk.at_end)
        if hit is None:
            if _find_block(lines, new, 0, at_end=False) is not None:
                notes.append(f"{path}: hunk {number} is already in the file; skipped")
                continue
            raise _Refused(
                f"{path}: hunk {number} (patch line {hunk.line}) does not match the file: no "
                f"place has the {len(old)} context/removed line(s) starting with {old[0]!r}. "
                "Read the file and copy the lines exactly."
            )
        at, label = hit
        if label != "exact":
            notes.append(f"{path}: hunk {number} matched with {label}")
        replacement = _merge(hunk, lines[at : at + len(old)])
        lines[at : at + len(old)] = replacement
        cursor = at + len(replacement)
    joined = eol.join(lines)
    return joined + eol if ends_with_newline and lines else joined


# -- applying ---------------------------------------------------------------------------------


class PatchFiles(Protocol):
    """File access for one patch, in the task's environment (adapted by the patch tool)."""

    def resolve(self, path: str) -> str:
        """The path as the task means it (relative to its cwd); equal keys name one file."""
        ...

    async def read_text(self, path: str) -> str | None:
        """The whole text, or ``None`` when nothing exists there; raises when it cannot tell."""
        ...

    async def write_text(self, path: str, content: str) -> None: ...

    async def delete(self, path: str) -> None: ...

    async def refusal(self, path: str, action: WriteAction, content: str | None) -> str | None:
        """Why the write guards refuse this change, or ``None`` when it may go ahead."""
        ...


@dataclass(frozen=True, slots=True)
class PatchOutcome:
    ok: bool
    error: str = ""
    added: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()
    moved: tuple[str, ...] = ()
    diff: str = ""
    lint: Mapping[str, Mapping[str, Any]] = field(default_factory=lambda: {})
    notes: tuple[str, ...] = ()
    rolled_back: bool = False
    left_changed: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        if not self.ok:
            out: dict[str, Any] = {"success": False, "error": self.error}
            if self.rolled_back:
                out["rolled_back"] = True
            if self.left_changed:
                out["left_changed"] = list(self.left_changed)
            if self.notes:
                out["notes"] = list(self.notes)
            return out
        out = {"success": True}
        for name in ("added", "updated", "deleted", "moved"):
            values: tuple[str, ...] = getattr(self, name)
            if values:
                out[name] = list(values)
        out["diff"] = self.diff
        if self.lint:
            out["lint"] = {path: dict(report) for path, report in self.lint.items()}
        if self.notes:
            out["notes"] = list(self.notes)
        return out


class _Plan:
    """Every touched file: what it held at the start and what the patch makes of it."""

    def __init__(self, files: PatchFiles) -> None:
        self.files = files
        self.before: dict[str, str | None] = {}
        self.after: dict[str, str | None] = {}
        self.names: dict[str, str] = {}
        self.order: list[str] = []

    async def current(self, path: str) -> tuple[str, str | None]:
        key = self.files.resolve(path)
        if key not in self.after:
            try:
                text = await self.files.read_text(path)
            except Exception as exc:
                raise _Refused(f"could not read {path}: {exc}") from exc
            self.before[key] = text
            self.after[key] = text
            self.names[key] = path
            self.order.append(key)
        return key, self.after[key]


async def _plan(patch: Patch, files: PatchFiles, notes: list[str]) -> tuple[_Plan, list[str]]:
    plan = _Plan(files)
    moves: list[str] = []
    for op in patch.ops:
        key, text = await plan.current(op.path)
        if op.kind == "add":
            if text is not None:
                raise _Refused(
                    f"{op.path} already exists; use '*** Update File:' to change it "
                    f"(patch line {op.line})"
                )
            body = "\n".join(op.content)
            plan.after[key] = body + "\n" if op.content else ""
            continue
        if text is None:
            what = {"delete": "delete", "update": "update", "move": "move"}[op.kind]
            raise _Refused(f"cannot {what} {op.path}: it does not exist (patch line {op.line})")
        if op.kind == "delete":
            plan.after[key] = None
            continue
        result = _apply_hunks(op.path, text, op.hunks, notes) if op.hunks else text
        if op.new_path is None:
            plan.after[key] = result
            continue
        target, existing = await plan.current(op.new_path)
        if target != key and existing is not None:
            raise _Refused(
                f"cannot move {op.path} to {op.new_path}: the destination exists "
                f"(patch line {op.line})"
            )
        plan.after[key] = None
        plan.after[target] = result
        moves.append(f"{op.path} -> {op.new_path}")
    return plan, moves


def _diff(name_from: str, name_to: str, old: str | None, new: str | None) -> str:
    return "".join(
        difflib.unified_diff(
            (old or "").splitlines(keepends=True),
            (new or "").splitlines(keepends=True),
            fromfile="/dev/null" if old is None else f"a/{name_from}",
            tofile="/dev/null" if new is None else f"b/{name_to}",
        )
    )


async def _undo(files: PatchFiles, done: Sequence[tuple[str, str | None]]) -> list[str]:
    """Put back what ``done`` changed, newest first; the paths that could not be restored."""
    failed: list[str] = []
    for name, original in reversed(done):
        try:
            if original is None:
                await files.delete(name)
            else:
                await files.write_text(name, original)
        except Exception as exc:
            log.warning("could not restore %s after a failed patch: %s", name, exc)
            failed.append(name)
    return failed


async def apply_patch(
    patch: Patch,
    files: PatchFiles,
    *,
    lint_host: ExecHost | None = None,
    cancel: CancelLike | None = None,
) -> PatchOutcome:
    """Apply ``patch`` all or nothing (see the module notes)."""
    if not patch.has_changes:
        return PatchOutcome(False, "the patch changes nothing: its hunks hold only context lines")
    notes: list[str] = []
    try:
        plan, moves = await _plan(patch, files, notes)
    except _Refused as exc:
        return PatchOutcome(False, f"{exc}. Nothing was changed.", notes=tuple(notes))
    changed = [key for key in plan.order if plan.after[key] != plan.before[key]]
    if not changed:
        return PatchOutcome(
            True, notes=(*notes, "every change is already in place; nothing written")
        )
    refusals: list[str] = []
    for key in changed:
        name, before, after = plan.names[key], plan.before[key], plan.after[key]
        try:
            reason = await files.refusal(name, "delete" if after is None else "write", after)
        except Exception as exc:
            reason = f"the write guards could not check {name} ({type(exc).__name__}: {exc})."
        if reason is None and after is not None:
            reason = gate(name, before, after)
        if reason:
            refusals.append(reason)
    if refusals:
        return PatchOutcome(False, " ".join(refusals) + " Nothing was changed.")
    writes = [key for key in changed if plan.after[key] is not None]
    deletes = [key for key in changed if plan.after[key] is None]
    done: list[tuple[str, str | None]] = []
    for key in writes + deletes:
        name, after = plan.names[key], plan.after[key]
        try:
            if after is None:
                await files.delete(name)
            else:
                await files.write_text(name, after)
        except Exception as exc:
            failed = await _undo(files, done)
            if failed:
                error = (
                    f"writing {name} failed ({exc}); the earlier changes could not all be undone"
                )
            else:
                error = f"writing {name} failed ({exc}); every earlier change was undone"
            return PatchOutcome(False, error, rolled_back=not failed, left_changed=tuple(failed))
        done.append((name, plan.before[key]))

    moved_targets = {m.split(" -> ", 1)[1] for m in moves}
    moved_sources = {m.split(" -> ", 1)[0] for m in moves}
    added = [
        plan.names[k]
        for k in writes
        if plan.before[k] is None and plan.names[k] not in moved_targets
    ]
    updated = [plan.names[k] for k in writes if plan.before[k] is not None]
    deleted = [plan.names[k] for k in deletes if plan.names[k] not in moved_sources]
    diffs: list[str] = []
    for move in moves:
        source, target = move.split(" -> ", 1)
        old = plan.before.get(files.resolve(source))
        new = plan.after.get(files.resolve(target))
        diffs.append(f"rename {source} -> {target}\n" + _diff(source, target, old, new))
    for key in changed:
        name = plan.names[key]
        if name in moved_targets or name in moved_sources:
            continue
        diffs.append(_diff(name, name, plan.before[key], plan.after[key]))
    lint: dict[str, Mapping[str, Any]] = {}
    for key in writes:
        name, after = plan.names[key], plan.after[key]
        assert after is not None
        report = await lint_edit(name, plan.before[key], after, host=lint_host, cancel=cancel)
        if report.status in ("introduced", "preexisting"):
            lint[name] = report.as_dict()
    return PatchOutcome(
        True,
        added=tuple(added),
        updated=tuple(updated),
        deleted=tuple(deleted),
        moved=tuple(moves),
        diff="".join(diffs),
        lint=lint,
        notes=tuple(notes),
    )


# -- the patch tool's mode -------------------------------------------------------------------

PATCH_MODE_PROPERTIES: dict[str, Any] = {
    "mode": {
        "type": "string",
        "enum": ["replace", "patch"],
        "description": (
            "'replace' (default) swaps old_string for new_string in one file. 'patch' applies "
            "the multi-file patch given in 'patch'."
        ),
    },
    "patch": {
        "type": "string",
        "description": (
            "For mode 'patch': the whole patch, from '*** Begin Patch' to '*** End Patch'. "
            "Sections: '*** Add File: <path>' with every line starting with '+'; '*** Delete "
            "File: <path>'; '*** Update File: <path>' (optionally followed by '*** Move to: "
            "<path>') with hunks that start at '@@ <a line above the change>' and list lines "
            "starting with ' ' (unchanged), '-' (removed) or '+' (added). Give about three "
            "unchanged lines around each change. Either every file changes or none does."
        ),
    },
}


def with_patch_mode(schema: Mapping[str, Any]) -> dict[str, Any]:
    """``schema`` (the patch tool's) with the ``mode``/``patch`` fields added; ``old_string``
    and ``new_string`` stop being required, because a V4A call does not send them."""
    out = copy.deepcopy(dict(schema))
    params: dict[str, Any] = dict(out.get("parameters") or {"type": "object"})
    props: dict[str, Any] = dict(params.get("properties") or {})
    props.update(copy.deepcopy(PATCH_MODE_PROPERTIES))
    params["properties"] = props
    listed: list[Any] = list(params.get("required") or [])
    required = [r for r in listed if r not in ("old_string", "new_string")]
    if required:
        params["required"] = required
    else:
        params.pop("required", None)
    out["parameters"] = params
    return out


def wants_v4a(args: Mapping[str, Any]) -> bool:
    """True when a patch call should take the V4A branch (any model may send either shape)."""
    mode = str(args.get("mode") or "").strip().lower()
    if mode == "patch":
        return True
    if mode == "replace":
        return False
    text = args.get("patch")
    return isinstance(text, str) and bool(text.strip()) and not args.get("old_string")


async def run_patch_mode(
    args: Mapping[str, Any],
    files: PatchFiles,
    *,
    lint_host: ExecHost | None = None,
    cancel: CancelLike | None = None,
) -> str:
    """The ``mode: "patch"`` branch of the patch tool: JSON text for the model."""
    text = args.get("patch")
    if not isinstance(text, str) or not text.strip():
        return json.dumps(
            {
                "success": False,
                "error": "mode 'patch' needs the patch text in the 'patch' field. " + GRAMMAR_HINT,
            }
        )
    try:
        patch = parse(text)
    except PatchSyntaxError as exc:
        return json.dumps(
            {
                "success": False,
                "error": f"The patch is malformed at line {exc.line}: {exc.reason}. "
                "Nothing was changed.",
                "line": exc.line,
            },
            ensure_ascii=False,
        )
    outcome = await apply_patch(patch, files, lint_host=lint_host, cancel=cancel)
    return json.dumps(outcome.as_dict(), ensure_ascii=False)
