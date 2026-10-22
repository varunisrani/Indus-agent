"""``complete.path``: the composer's path and ``@`` completion, one definition for every transport.

The in-process bridge (``--local``) and the daemon's WebSocket (an attached TUI, W48-b) both call
``complete_path``, so the same word in the same folder completes the same way. It completes the
word under the cursor:

- a bare ``@`` → the reference forms (``@file:``, ``@folder:``, ``@diff``, ``@git:``, ``@url:``);
- ``@file:…`` / ``@folder:…`` → entries of that kind only; other ``@kind:`` forms → nothing;
- a bare ``@word`` of at least ``FUZZY_MIN`` characters (no slash) on a local backend → a fuzzy
  search of base names under the folder (``MAX_ITEMS`` best; folders first among equals);
- anything else → the entries of the folder the typed path points into whose names start with
  its last part (at most ``MAX_ITEMS``).

The folder is the one the client names, else the session's (``session_folder``), else this
process's working folder on a local backend. Listings run where the session's commands run: when
the terminal backend is not local the folder is listed through that backend (one short ``sh``
script), never on this host, whose same-named tree would look right and be wrong (R05.13). An
unreachable backend gives no items. Hidden entries show only when the typed part starts with a
dot; tool and cache folders are skipped after ``@``.

Who may name which session is the transport's rule: the daemon checks that the connection attached
it before it calls in here.
"""

from __future__ import annotations

import logging
import os
import posixpath
import shlex
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from indus.backends.registry import EnvironmentRegistry
from indus.foundation.blocking import run_blocking
from indus.foundation.home import expand_path_text
from indus.foundation.scope import Scope
from indus.protocol.bridge import CompletePathParams, CompleteResult, CompletionItem
from indus.protocol.methods import ProtocolError

if TYPE_CHECKING:
    from indus.service.session_service import SessionService

__all__ = [
    "FUZZY_MIN",
    "LISTING_SCRIPT",
    "MAX_ITEMS",
    "REFERENCE_HINTS",
    "SKIPPED",
    "complete_path",
    "fuzzy_rank",
    "session_folder",
]

log = logging.getLogger(__name__)

MAX_ITEMS = 30
FUZZY_MIN = 2
WALK_LIMIT = 20_000
REMOTE_TIMEOUT_S = 3.0
REFERENCE_HINTS: tuple[tuple[str, str], ...] = (
    ("@file:", "attach a file"),
    ("@folder:", "attach a folder listing"),
    ("@diff", "the working-tree diff"),
    ("@git:", "one commit"),
    ("@url:", "a web page"),
)
SKIPPED = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        "dist",
        "build",
    }
)
LISTING_SCRIPT = (
    'where=$1; case "$where" in "~") where=$HOME ;; "~/"*) where="$HOME/${where#??}" ;; esac; '
    '[ -d "$where" ] || exit 0; cd "$where" || exit 0; '
    "for entry in * .*; do "
    'case "$entry" in .|..) continue ;; esac; '
    '[ -e "$entry" ] || [ -L "$entry" ] || continue; '
    'if [ -d "$entry" ]; then printf "%s/\\n" "$entry"; else printf "%s\\n" "$entry"; fi; '
    "done"
)
"""Lists one folder as ``name`` / ``name/`` lines (runs inside a non-local backend)."""


def _item(text: str, display: str, meta: str = "") -> CompletionItem:
    return CompletionItem(text=text, display=display, meta=meta)


def fuzzy_rank(name: str, query: str) -> int | None:
    """0 exact, 1 prefix, 2 substring, 3 letters in order; None when ``name`` does not match."""
    low, want = name.lower(), query.lower()
    if low == want:
        return 0
    if low.startswith(want):
        return 1
    if want in low:
        return 2
    letters = iter(low)
    if all(char in letters for char in want):
        return 3
    return None


def _walk(root: Path, show_hidden: bool) -> Iterator[tuple[str, bool]]:
    """``(relative path, is folder)`` under ``root``, at most ``WALK_LIMIT`` entries."""
    seen = 0
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(
            d for d in dirs if d not in SKIPPED and (show_hidden or not d.startswith("."))
        )
        base = Path(folder).relative_to(root)
        for name in dirs:
            yield (base / name).as_posix(), True
            seen += 1
        for name in sorted(files):
            if not show_hidden and name.startswith("."):
                continue
            yield (base / name).as_posix(), False
            seen += 1
        if seen >= WALK_LIMIT:
            return


def _fuzzy(root: Path, query: str, tag: str) -> list[CompletionItem]:
    ranked: list[tuple[int, bool, int, str, bool]] = []
    for rel, is_dir in _walk(root, query.startswith(".")):
        if tag == "file" and is_dir:
            continue
        rank = fuzzy_rank(posixpath.basename(rel), query)
        if rank is not None:
            ranked.append((rank, not is_dir, len(rel), rel, is_dir))
    ranked.sort()
    items: list[CompletionItem] = []
    for _rank, _file, _size, rel, is_dir in ranked[:MAX_ITEMS]:
        name = posixpath.basename(rel)
        if is_dir:
            items.append(_item(f"@folder:{rel}/", name + "/", "dir"))
        else:
            items.append(_item(f"@{tag or 'file'}:{rel}", name, posixpath.dirname(rel)))
    return items


def _local_entries(folder: str) -> list[tuple[str, bool]]:
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    return [(name, os.path.isdir(os.path.join(folder, name))) for name in names]


async def _remote_entries(
    scope: Scope,
    environments: EnvironmentRegistry | None,
    session_id: str | None,
    folder: str,
) -> list[tuple[str, bool]]:
    if environments is None:
        from indus.tools.builtin import terminal

        environments = terminal.runtime().environments
    try:
        _key, env = await environments.acquire(scope, session_id=session_id)
        command = f"sh -c {shlex.quote(LISTING_SCRIPT)} sh {shlex.quote(folder)}"
        result = await env.execute(command, timeout=REMOTE_TIMEOUT_S, bounded=False)
    except Exception:
        log.info("the terminal backend could not list %s", folder, exc_info=True)
        return []
    if result.returncode != 0 or result.timed_out:
        return []
    entries: list[tuple[str, bool]] = []
    for line in result.output.splitlines():
        is_dir = line.endswith("/")
        name = line.rstrip("/")
        if name and "/" not in name:
            entries.append((name, is_dir))
    return sorted(entries)


def session_folder(service: SessionService | None, session_id: str | None) -> str | None:
    """The folder a session's commands run in, when the session is live here and names one."""
    if service is None or not session_id:
        return None
    try:
        agent = service.agent(session_id)
    except ProtocolError:
        return None
    found = agent.options.cwd or agent.options.session_fields.get("cwd")
    return str(found) if found else None


async def complete_path(
    scope: Scope,
    params: CompletePathParams,
    *,
    folder: str | None,
    environments: EnvironmentRegistry | None = None,
) -> CompleteResult:
    """``complete.path`` for ``params``; ``folder`` is the session's (``session_folder``)."""
    word = params.word
    if not word:
        return CompleteResult(items=[])
    local = EnvironmentRegistry.backend_name(scope) == "local"
    root = params.cwd or folder or (os.getcwd() if local else ".")
    is_ref = word.startswith("@")
    query = word[1:] if is_ref else word
    if is_ref and not query:
        return CompleteResult(items=[_item(text, text, meta) for text, meta in REFERENCE_HINTS])
    tag, path_part = "", query
    if is_ref and (query in ("file", "folder") or query.startswith(("file:", "folder:"))):
        tag, _, path_part = query.partition(":")
    elif is_ref and ":" in query:
        return CompleteResult(items=[])
    bare = is_ref and bool(path_part) and "/" not in path_part
    if local and bare and len(path_part.strip()) >= FUZZY_MIN and tag != "folder":
        found = await run_blocking(_fuzzy, Path(root), path_part.strip(), tag, label="fuzzy paths")
        return CompleteResult(items=found)
    listing = _Listing(scope, environments, params.session_id, local)
    return CompleteResult(items=await listing.items(root, path_part, tag, is_ref))


class _Listing:
    """Entries of the folder a typed path points into, listed where the session's commands run."""

    def __init__(
        self,
        scope: Scope,
        environments: EnvironmentRegistry | None,
        session_id: str | None,
        local: bool,
    ) -> None:
        self.scope = scope
        self.environments = environments
        self.session_id = session_id
        self.local = local

    async def items(
        self, root: str, path_part: str, tag: str, is_ref: bool
    ) -> list[CompletionItem]:
        """Each candidate keeps what was typed before the last part, so the word is completed
        in place."""
        local = self.local
        paths = os.path if local else posixpath
        if path_part.endswith("/") or path_part in ("", ".", "~"):
            stem = ""
        else:
            stem = paths.basename(path_part)
        prefix = path_part[: len(path_part) - len(stem)]
        if path_part == "~":
            prefix = "~/"
        folder = prefix or "."
        if local and folder.startswith("~"):
            folder = str(expand_path_text(folder, self.scope.env))
        if not (paths.isabs(folder) or folder.startswith("~")):
            folder = paths.normpath(paths.join(root, folder))
        if local:
            entries = await run_blocking(_local_entries, folder, label="path listing")
        else:
            entries = await _remote_entries(self.scope, self.environments, self.session_id, folder)
        items: list[CompletionItem] = []
        for name, is_dir in entries:
            if stem and not name.lower().startswith(stem.lower()):
                continue
            if name.startswith(".") and not stem.startswith("."):
                continue
            if is_ref and name in SKIPPED:
                continue
            if tag and (tag == "folder") != is_dir:
                continue
            suffix = "/" if is_dir else ""
            body = f"{prefix}{name}{suffix}"
            text = f"@{tag or ('folder' if is_dir else 'file')}:{body}" if is_ref else body
            items.append(_item(text, name + suffix, "dir" if is_dir else ""))
            if len(items) >= MAX_ITEMS:
                break
        return items
