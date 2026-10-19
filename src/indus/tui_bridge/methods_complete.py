"""Completion methods of the bridge (P12-31): ``complete.slash`` and ``complete.path``.

``complete.slash`` asks the shared registry (``indus.service.commands``) with the names this
surface offers plus the user's quick commands and the runnable skills and bundles.

``complete.path`` completes the word under the cursor:

- a bare ``@`` → the reference forms (``@file:``, ``@folder:``, ``@diff``, ``@git:``, ``@url:``);
- ``@file:…`` / ``@folder:…`` → entries of that kind only; other ``@kind:`` forms → nothing;
- a bare ``@word`` of at least ``FUZZY_MIN`` characters (no slash) on a local backend → a fuzzy
  search of base names under the folder (``MAX_ITEMS`` best; folders first among equals);
- anything else → the entries of the folder the typed path points into whose names start with
  its last part (at most ``MAX_ITEMS``).

Listings run where the session's commands run: when the terminal backend is not local the folder
is listed through that backend (one short ``sh`` script), never on this host, whose same-named
tree would look right and be wrong (R05.13). An unreachable backend gives no items. Hidden entries
show only when the typed part starts with a dot; tool and cache folders are skipped after ``@``.
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
from indus.protocol.bridge import (
    CompletePathParams,
    CompleteResult,
    CompleteSlashParams,
    CompletionItem,
)
from indus.protocol.methods import ProtocolError
from indus.service.commands.registry import REGISTRY
from indus.tui_bridge import methods_slash

if TYPE_CHECKING:
    from indus.tui_bridge.bridge import Bridge
    from indus.tui_bridge.dispatch import MethodTable

__all__ = ["FUZZY_MIN", "LISTING_SCRIPT", "MAX_ITEMS", "fuzzy_rank", "register"]

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


# -- slash -----------------------------------------------------------------------------------------


async def complete_slash(bridge: Bridge, params: CompleteSlashParams) -> CompleteResult:
    extra: dict[str, str] = {}
    for name, spec in methods_slash.quick_commands(bridge).items():
        extra[name] = str(spec.get("description") or "quick command")
    table = methods_slash.skill_table(bridge, params.session_id)
    if table is not None:
        for name, note in table.names().items():
            extra.setdefault(name, note)
    found = REGISTRY.complete(params.text, REGISTRY.keys(methods_slash.SURFACE), extra=extra)
    return CompleteResult(items=[_item(c.text, c.text, c.meta) for c in found[:200]])


# -- paths -----------------------------------------------------------------------------------------


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
    bridge: Bridge, session_id: str | None, folder: str
) -> list[tuple[str, bool]]:
    from indus.tools.builtin import terminal

    environments: EnvironmentRegistry = (
        bridge.environments if bridge.environments is not None else terminal.runtime().environments
    )
    try:
        _key, env = await environments.acquire(bridge.scope, session_id=session_id)
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


def _session_folder(bridge: Bridge, session_id: str | None) -> str | None:
    service = bridge.built
    if service is None or not session_id:
        return None
    try:
        agent = service.agent(session_id)
    except ProtocolError:
        return None
    folder = agent.options.cwd or agent.options.session_fields.get("cwd")
    return str(folder) if folder else None


async def complete_path(bridge: Bridge, params: CompletePathParams) -> CompleteResult:
    word = params.word
    if not word:
        return CompleteResult(items=[])
    local = EnvironmentRegistry.backend_name(bridge.scope) == "local"
    root = (
        params.cwd or _session_folder(bridge, params.session_id) or (os.getcwd() if local else ".")
    )
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
    return CompleteResult(
        items=await _listing(bridge, params.session_id, root, path_part, tag, is_ref, local)
    )


async def _listing(
    bridge: Bridge,
    session_id: str | None,
    root: str,
    path_part: str,
    tag: str,
    is_ref: bool,
    local: bool,
) -> list[CompletionItem]:
    """Entries of the folder ``path_part`` points into; each candidate keeps what was typed
    before the last part, so the word is completed in place."""
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
        folder = str(expand_path_text(folder, bridge.scope.env))
    if not (paths.isabs(folder) or folder.startswith("~")):
        folder = paths.normpath(paths.join(root, folder))
    if local:
        entries = await run_blocking(_local_entries, folder, label="path listing")
    else:
        entries = await _remote_entries(bridge, session_id, folder)
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


def register(table: MethodTable) -> None:
    table.add("complete.slash", complete_slash)
    table.add("complete.path", complete_path)
