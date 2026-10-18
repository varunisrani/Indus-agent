"""Project context files: discovery, bounded reads, injection screening and truncation.

Discovery starts at the working directory and stops at the first category that yields any text;
later categories are not even read:

1. ``.indus.md`` / ``INDUS.md`` — the nearest one walking up from the cwd to the git root (cwd
   only outside git); YAML front matter is dropped.
2. The AGENTS chain — every directory from the git root down to the cwd (root first, cwd last, so
   the most specific file reads last). In each directory the personal override file wins when it
   has text, then the committed file, then its lower-case spelling (``AGENTS_NAMES``). Outside
   git only the cwd.
3. ``CLAUDE.md`` / ``claude.md`` in the cwd.
4. ``.cursorrules`` and ``.cursor/rules/*.mdc`` in the cwd, all of them.

Files inside the Indus install tree are never project context (R01.15). Each file is screened
with ``safety.threat_scan`` and replaced by a one-line marker on a hit, then capped. Every read
runs on a worker thread with a deadline on the scope's clock; a read that misses it is abandoned,
never joined, and counts as empty.

Truncation warnings go to the list installed by ``collect_warnings()`` — a context variable, so
concurrent prompt builds each see only their own.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import threading
from collections.abc import Awaitable, Callable, Generator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

import indus
from indus.core.prompt import texts
from indus.foundation.clock import Clock
from indus.foundation.scope import Scope
from indus.safety import threat_scan

__all__ = [
    "CEILING_CHARS",
    "DEFAULT_READ_TIMEOUT",
    "FLOOR_CHARS",
    "ContextFile",
    "ReadSettings",
    "Reader",
    "collect_warnings",
    "context_cap",
    "find_git_root",
    "install_root",
    "load_project_context",
    "omitted_headings",
    "queue_warning",
    "read_bounded",
    "read_utf8",
    "screen",
    "strip_front_matter",
    "truncate",
]

log = logging.getLogger(__name__)

DEFAULT_READ_TIMEOUT = 5.0
FLOOR_CHARS = 20_000
CEILING_CHARS = 500_000
WINDOW_SHARE = 0.06
CHARS_PER_TOKEN = 4
HEAD_SHARE = 0.7
TAIL_SHARE = 0.2
MAX_OMITTED_HEADINGS = 15

CAP_KEY = "prompt.context_file_max_chars"
TIMEOUT_KEY = "prompt.context_file_read_timeout"

NATIVE_NAMES = (".indus.md", "INDUS.md")
AGENTS_NAMES = ("AGENTS.override.md", "AGENTS.md", "agents.md")
CLAUDE_NAMES = ("CLAUDE.md", "claude.md")
CURSOR_RULES = ".cursorrules"

_BOM = chr(0xFEFF)

Reader = Callable[[Path], str]


def read_utf8(path: Path) -> str:
    """The default reader: UTF-8 with an optional BOM. Raises like ``Path.read_text``."""
    return path.read_text(encoding="utf-8-sig")


# -- warnings -------------------------------------------------------------------------------------

_WARNINGS: ContextVar[list[str] | None] = ContextVar("indus_prompt_warnings", default=None)


@contextmanager
def collect_warnings() -> Generator[list[str]]:
    """Install a fresh warning list for this context (one prompt build) and yield it."""
    bucket: list[str] = []
    token = _WARNINGS.set(bucket)
    try:
        yield bucket
    finally:
        _WARNINGS.reset(token)


def queue_warning(text: str) -> None:
    """Add a status-line warning to the current build's list; outside a build it is only logged."""
    bucket = _WARNINGS.get()
    if bucket is not None:
        bucket.append(text)


# -- bounded reads --------------------------------------------------------------------------------


def _config_value(scope: Scope, key: str) -> object:
    if scope.config is None:
        return None
    try:
        return scope.config.get(key)
    except Exception:  # a broken config view must never break prompt assembly
        log.debug("config read failed for %s", key, exc_info=True)
        return None


def _positive_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value > 0 else None


@dataclass(frozen=True)
class ReadSettings:
    """How context files are read: the clock the deadline runs on, the deadline, the reader."""

    clock: Clock
    deadline: float = DEFAULT_READ_TIMEOUT
    reader: Reader = field(default=read_utf8)

    @classmethod
    def for_scope(cls, scope: Scope, reader: Reader | None = None) -> ReadSettings:
        deadline = _positive_number(_config_value(scope, TIMEOUT_KEY)) or DEFAULT_READ_TIMEOUT
        return cls(clock=scope.clock, deadline=deadline, reader=reader or read_utf8)


def _settle(future: asyncio.Future[str], value: str | None, error: BaseException | None) -> None:
    if future.done():
        return
    if error is not None:
        future.set_exception(error)
    else:
        future.set_result(value or "")


def _drain(future: asyncio.Future[str]) -> None:
    # An abandoned read may still fail later; fetch the error so asyncio does not report it.
    if not future.cancelled():
        future.exception()


def _start_read(path: Path, reader: Reader) -> asyncio.Future[str]:
    """Run ``reader(path)`` on a daemon thread. The thread is never joined: a read stuck on a
    network file system must not hold up the turn, nor the process exit."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()

    def run() -> None:
        value: str | None = None
        error: BaseException | None = None
        try:
            value = reader(path)
        except BaseException as exc:  # handed to the awaiting task, not raised here
            error = exc
        with contextlib.suppress(RuntimeError):  # the loop is gone; nobody is waiting any more
            loop.call_soon_threadsafe(_settle, future, value, error)

    threading.Thread(target=run, name=f"indus-context-read:{path.name}", daemon=True).start()
    return future


async def read_bounded(path: Path, settings: ReadSettings) -> str | None:
    """The file's text, or None when it is missing, unreadable or not read within the deadline."""
    work = _start_read(path, settings.reader)
    work.add_done_callback(_drain)  # also covers a read abandoned by an interrupt
    timer = asyncio.ensure_future(settings.clock.sleep(settings.deadline))
    try:
        done, _ = await asyncio.wait({work, timer}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        timer.cancel()
    if work not in done:
        log.warning(
            "context file %s was not read within %.1f s; skipping it", path, settings.deadline
        )
        return None
    try:
        return work.result()
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
        return None
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        log.debug("could not read context file %s: %s", path, exc)
        return None


# -- screening and shaping ------------------------------------------------------------------------


def screen(text: str, name: str, *, user_authored: bool = False) -> str:
    """``text`` when the scan is clean, else a one-line marker (files the user did not write).

    The user's own files in their home (the persona) are the same trust class as the config file:
    a hit is logged and the text is kept.
    """
    text = text.removeprefix(_BOM)
    findings = threat_scan.scan(text, scope="context")
    if not findings:
        return text
    listed = ", ".join(findings)
    if user_authored:
        log.warning(
            "%s matched prompt-injection patterns (%s); loading it anyway because it is your own "
            "file; review it if you did not write that text",
            name,
            listed,
        )
        return text
    log.warning("context file %s blocked: %s", name, listed)
    return texts.BLOCKED_MARKER.format(name=name, findings=listed)


def strip_front_matter(text: str) -> str:
    """Drop a leading YAML front-matter block (``---`` ... ``---``); unterminated blocks stay."""
    text = text.removeprefix(_BOM)
    lines = text.split("\n")
    if not lines or lines[0].rstrip() != "---":
        return text
    for index in range(1, len(lines)):
        if lines[index].rstrip() in ("---", "..."):
            return "\n".join(lines[index + 1 :]).lstrip("\n")
    return text


def context_cap(window: int | None, configured: object = None) -> int:
    """Character cap for one context file: the configured value, else a share of the window."""
    explicit = _positive_number(configured)
    if explicit is not None:
        return int(explicit)
    if isinstance(window, bool) or not isinstance(window, int) or window <= 0:
        return FLOOR_CHARS
    return max(FLOOR_CHARS, min(int(window * CHARS_PER_TOKEN * WINDOW_SHARE), CEILING_CHARS))


_HEADING = re.compile(r" {0,3}#{1,6}[ \t]+(\S.*?)[ \t#]*$")
_FENCE = re.compile(r" {0,3}(```|~~~)")


def omitted_headings(
    text: str, start: int, end: int, limit: int = MAX_OMITTED_HEADINGS
) -> list[str]:
    """Markdown headings starting in ``text[start:end]``; ``#`` lines inside fences are code."""
    found: list[str] = []
    offset = 0
    fence = ""
    for line in text.splitlines(keepends=True):
        bare = line.rstrip("\r\n")
        opener = _FENCE.match(bare)
        if opener is not None:
            if not fence:
                fence = opener.group(1)
            elif opener.group(1) == fence:
                fence = ""
        elif not fence and start <= offset < end:
            heading = _HEADING.match(bare)
            if heading is not None:
                found.append(heading.group(1))
        offset += len(line)
    if len(found) > limit:
        return [*found[:limit], texts.OMITTED_MORE]
    return found


def truncate(text: str, *, name: str, cap: int, read_path: str, warn: bool = True) -> str:
    """Keep 70 % head and 20 % tail of an over-cap text with a marker between them."""
    total = len(text)
    if total <= cap:
        return text
    head = int(cap * HEAD_SHARE)
    tail = int(cap * TAIL_SHARE)
    headings = omitted_headings(text, head, total - tail)
    omitted = texts.OMITTED_SECTIONS.format(headings="; ".join(headings)) if headings else ""
    warning = texts.TRUNCATION_WARNING.format(name=name, total=total, cap=cap)
    log.warning("%s", warning)
    if warn:
        queue_warning(warning)
    marker = texts.TRUNCATION_MARKER.format(
        name=name, head=head, tail=tail, total=total, omitted=omitted, path=read_path
    )
    return text[:head] + marker + text[total - tail :]


# -- discovery ------------------------------------------------------------------------------------


def _exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:  # a parent we may not stat is "nothing here", not a crash
        return False


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def find_git_root(start: Path) -> Path | None:
    """The nearest directory at or above ``start`` holding ``.git`` (a directory or a file)."""
    for directory in (start, *start.parents):
        if _exists(directory / ".git"):
            return directory
    return None


def install_root() -> Path:
    """The Indus install tree: the package directory, or the checkout root in a ``src/`` layout."""
    package = Path(indus.__file__).resolve().parent
    return package.parent.parent if package.parent.name == "src" else package


def _inside(path: Path, root: Path | None) -> bool:
    if root is None:
        return False
    try:
        return path.resolve().is_relative_to(root)
    except OSError:
        return False


@dataclass(frozen=True)
class ContextFile:
    """One file found by discovery; ``text`` is empty when the file is empty or unreadable."""

    category: str
    label: str
    path: Path
    text: str


@dataclass(frozen=True)
class _Walk:
    cwd: Path
    git_root: Path | None
    install: Path | None
    reads: ReadSettings
    cap: int

    def label(self, path: Path) -> str:
        if self.git_root is not None and path.is_relative_to(self.git_root):
            return path.relative_to(self.git_root).as_posix()
        return path.name

    async def read(self, category: str, path: Path) -> ContextFile | None:
        if _inside(path, self.install):
            log.info("ignoring %s: it belongs to the Indus install tree", path)
            return None
        text = await read_bounded(path, self.reads)
        return ContextFile(category, self.label(path), path, (text or "").strip())

    def section(self, item: ContextFile, body: str) -> str:
        rendered = f"## {item.label}\n\n{screen(body, item.label)}"
        return truncate(rendered, name=item.label, cap=self.cap, read_path=str(item.path))


async def _native(walk: _Walk) -> list[str]:
    stop = walk.git_root
    directories = [walk.cwd, *walk.cwd.parents] if stop is not None else [walk.cwd]
    for directory in directories:
        for name in NATIVE_NAMES:
            candidate = directory / name
            if _is_file(candidate):
                item = await walk.read("native", candidate)
                if item is None:
                    return []
                body = strip_front_matter(item.text).strip()
                return [walk.section(item, body)] if body else []
        if directory == stop:
            break
    return []


def _chain(cwd: Path, root: Path | None) -> list[Path]:
    if root is None or root == cwd or not cwd.is_relative_to(root):
        return [cwd]
    parts = cwd.relative_to(root).parts
    return [root, *(root.joinpath(*parts[: depth + 1]) for depth in range(len(parts)))]


async def _agents(walk: _Walk) -> list[str]:
    picked: list[ContextFile] = []
    seen: set[str] = set()
    for directory in _chain(walk.cwd, walk.git_root):
        for name in AGENTS_NAMES:
            candidate = directory / name
            if not _is_file(candidate):
                continue
            item = await walk.read("agents", candidate)
            if item is None or not item.text:
                continue
            if item.text not in seen:  # a copy (or symlink) further down adds nothing
                seen.add(item.text)
                picked.append(item)
            break
    sections = [walk.section(item, item.text) for item in picked]
    if len(sections) <= 1:
        return sections
    # Each file is capped on its own; the merged chain is capped again so depth cannot multiply it.
    merged = "\n\n".join(sections)
    return [
        truncate(
            merged, name="AGENTS.md chain", cap=walk.cap, read_path=str(walk.cwd / "AGENTS.md")
        )
    ]


async def _claude(walk: _Walk) -> list[str]:
    for name in CLAUDE_NAMES:
        candidate = walk.cwd / name
        if not _is_file(candidate):
            continue
        item = await walk.read("claude", candidate)
        if item is not None and item.text:
            return [walk.section(item, item.text)]
    return []


async def _cursor(walk: _Walk) -> list[str]:
    candidates = [walk.cwd / CURSOR_RULES]
    rules = walk.cwd / ".cursor" / "rules"
    try:
        if rules.is_dir():
            candidates.extend(sorted(rules.glob("*.mdc")))
    except OSError:
        pass
    parts: list[str] = []
    for candidate in candidates:
        if not _is_file(candidate):
            continue
        item = await walk.read("cursor", candidate)
        if item is not None and item.text:
            label = item.path.relative_to(walk.cwd).as_posix()
            parts.append(f"## {label}\n\n{screen(item.text, label)}")
    if not parts:
        return []
    joined = "\n\n".join(parts)
    return [
        truncate(joined, name=CURSOR_RULES, cap=walk.cap, read_path=str(walk.cwd / CURSOR_RULES))
    ]


_CATEGORIES: Sequence[Callable[[_Walk], Awaitable[list[str]]]] = (
    _native,
    _agents,
    _claude,
    _cursor,
)


async def load_project_context(
    cwd: Path,
    *,
    reads: ReadSettings,
    cap: int,
    install: Path | None = None,
) -> str:
    """The rendered project-context section for ``cwd``, or "" when nothing applies."""
    try:
        here = cwd.resolve()
    except OSError:
        log.debug("working directory %s cannot be resolved; no project context", cwd)
        return ""
    install_dir = install.resolve() if install is not None else None
    if _inside(here, install_dir):
        log.warning(
            "no project context: the working directory %s is inside the Indus install tree; set "
            "terminal.cwd to your project",
            here,
        )
        return ""
    walk = _Walk(here, find_git_root(here), install_dir, reads, cap)
    for category in _CATEGORIES:
        sections = await category(walk)
        if sections:
            return texts.PROJECT_CONTEXT_HEADING + "\n\n" + "\n\n".join(sections)
    return ""
