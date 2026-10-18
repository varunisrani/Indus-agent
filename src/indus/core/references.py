"""``@`` context references in a user message (P08-19, A8).

``expand(text, scope, cwd)`` finds reference tokens in what the user typed and returns an
``Expansion``: the text the model gets (what was typed, then one context section), the blocks that
were inserted and the references that were refused. Forms:

- ``@file:PATH`` — the file (100 KiB at most, with a truncation marker); ``@file:PATH:a-b`` (or
  ``:a``) only lines ``a`` to ``b``. A path with spaces is quoted: ``@file:"my notes.txt"``.
- ``@folder:PATH`` — a listing two levels deep, at most 200 entries (VCS, dependency and cache
  folders are skipped).
- ``@diff`` — the working-tree diff of the git repository holding the session's folder (against
  ``HEAD``; capped).
- ``@git:REF`` — one commit's patch with its message and stat (capped).
- ``@url:URL`` — the page text through ``web_extract``, so the URL policy (private addresses,
  block list) applies first.
- ``@name:ARG`` — a prefix registered through ``register_prefix`` (the P13 plugin seam).

Paths resolve against the session's working directory and pass the file tools' guards (P05):
Indus secret and control files, secret env files, devices; and on top of those every credential
store a reference must never paste into a conversation (SSH and cloud credential folders, key
files, key stores, ``.netrc`` …). A refused reference shows up as one bracketed line in the
context section — never silently dropped, never expanded.

``budget_chars`` bounds the whole context section; an item that does not fit becomes a one-line
pointer that tells the model how to read it on demand. Trailing sentence punctuation after a token
is not part of it. The same token twice is expanded once. A token whose name is not a known form
(``@alice``) is left alone.
"""

from __future__ import annotations

import os
import posixpath
import re
import subprocess
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from indus.foundation.blocking import run_blocking
from indus.foundation.home import expand_path_text
from indus.foundation.scope import Scope
from indus.platform.locate import which
from indus.safety.child_env import child_env
from indus.safety.path_guards import (
    blocked_device,
    has_binary_extension,
    homes_for,
    nt_namespace_error,
    read_denial,
)
from indus.safety.url_policy import Blocked, Resolver, check_url_async, normalize_url

__all__ = [
    "BUILTIN_KINDS",
    "MAX_FILE_BYTES",
    "MAX_FOLDER_ENTRIES",
    "ContextBlock",
    "Expansion",
    "PrefixHandler",
    "ReferenceContext",
    "ReferenceRefused",
    "Refusal",
    "budget_for_trigger",
    "budget_for_window",
    "credential_reason",
    "expand",
    "find_references",
    "register_prefix",
    "unregister_prefix",
]

MAX_FILE_BYTES = 100 * 1024
MAX_READ_BYTES = 8 * 1024 * 1024  # a line range may sit past the first 100 KiB
MAX_FOLDER_ENTRIES = 200
FOLDER_DEPTH = 2
MAX_GIT_BYTES = 100 * 1024
GIT_TIMEOUT_S = 15.0
BUDGET_SHARE = 0.25  # of the context window, when no compression trigger is known
CHARS_PER_TOKEN = 4

BUILTIN_KINDS = frozenset({"file", "folder", "diff", "git", "url"})
SKIP_DIRS = frozenset(
    {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache",
     ".pytest_cache", ".ruff_cache", ".tox", "dist", "build", ".next", "target"}
)  # fmt: skip

SECTION_OPEN = "=== Context the user referenced ==="
SECTION_CLOSE = "=== End of referenced context ==="

_TOKEN = re.compile(
    r"(?<![\w@./])@(?P<kind>[a-z][a-z0-9_-]*)(?::(?P<arg>\"[^\"]+\"|`[^`]+`|[^\s]+))?"
)
_RANGE = re.compile(r"^(?P<path>.+?):(?P<a>\d+)(?:-(?P<b>\d+))?$")
_GIT_REF = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/~^@{}-]*$")
_TRAILING = ".,;:!?"
_CLOSERS = {")": "(", "]": "[", "}": "{"}


# -- credentials -----------------------------------------------------------------------------------

# Folders under a home whose every file is a credential, and single credential files there.
_HOME_CREDENTIAL_DIRS = (".ssh", ".aws", ".gnupg", ".azure", ".config/gcloud", ".kube")
_HOME_CREDENTIAL_FILES = (".netrc", ".pgpass", ".git-credentials", ".npmrc", ".pypirc",
                          ".docker/config.json")  # fmt: skip
_KEY_NAMES = re.compile(r"^id_(?:rsa|dsa|ecdsa|ed25519)(?:_sk)?$", re.IGNORECASE)
_KEY_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".kdbx", ".ppk"})


def _under(path: str, directory: str) -> bool:
    return path == directory or path.startswith(directory.rstrip("/") + "/")


def credential_reason(path: str, scope: Scope | None) -> str | None:
    """Why ``path`` (absolute, normalised) must not be pasted into a conversation, or ``None``."""
    denial = read_denial(path, scope)
    if denial is not None:
        return denial
    for home in homes_for(scope):
        for name in _HOME_CREDENTIAL_DIRS:
            if _under(path, posixpath.join(home, name)):
                return f"{path} is inside the credential folder ~/{name}"
        for name in _HOME_CREDENTIAL_FILES:
            if path == posixpath.join(home, name):
                return f"{path} holds login credentials"
    base = posixpath.basename(path)
    if _KEY_NAMES.match(base):
        return f"{path} looks like a private SSH key"
    suffix = posixpath.splitext(base)[1].lower()
    if suffix in _KEY_SUFFIXES:
        return f"{path} looks like a key or key store ({suffix})"
    return None


# -- results ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextBlock:
    token: str
    kind: str
    title: str
    body: str
    pointer: bool = False  # the body did not fit and a one-line pointer stands in for it


@dataclass(frozen=True)
class Refusal:
    token: str
    reason: str


@dataclass(frozen=True)
class Expansion:
    text: str
    blocks: tuple[ContextBlock, ...] = ()
    refusals: tuple[Refusal, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.blocks or self.refusals)


class ReferenceRefused(Exception):
    """Raised by a reference handler (built-in or plugin) to refuse with a visible reason."""


@dataclass(frozen=True)
class ReferenceContext:
    scope: Scope
    cwd: Path
    resolver: Resolver | None = None
    extract: Callable[[str], Awaitable[str]] | None = None


type PrefixHandler = Callable[[str, ReferenceContext], Awaitable[str]]

_PREFIXES: dict[str, PrefixHandler] = {}


def register_prefix(name: str, handler: PrefixHandler) -> None:
    """Add ``@name:ARG``; ``handler(arg, ctx)`` returns the text or raises ``ReferenceRefused``.
    Built-in names cannot be replaced."""
    key = name.strip().lower()
    if key in BUILTIN_KINDS or not re.fullmatch(r"[a-z][a-z0-9_-]*", key):
        raise ValueError(f"cannot register the reference prefix {name!r}")
    _PREFIXES[key] = handler


def unregister_prefix(name: str) -> None:
    _PREFIXES.pop(name.strip().lower(), None)


def budget_for_window(window_tokens: int | None) -> int | None:
    """Characters the context section may use for a window (``None``: no budget known); the
    fallback when compression is off."""
    if not window_tokens or window_tokens <= 0:
        return None
    return int(window_tokens * CHARS_PER_TOKEN * BUDGET_SHARE)


def budget_for_trigger(trigger_tokens: int, used_tokens: int | None) -> int | None:
    """Characters the context section may use: the room left under the compression trigger
    (P08-02) after the prompt the provider last reported (``None``: no trigger known). With no
    room left every item becomes a pointer."""
    if trigger_tokens <= 0:
        return None
    room = trigger_tokens - max(0, used_tokens or 0)
    return max(0, room) * CHARS_PER_TOKEN


# -- tokens ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Token:
    raw: str
    kind: str
    arg: str


def _trim(arg: str) -> str:
    if arg[:1] in ('"', "`") and arg[-1:] == arg[:1] and len(arg) >= 2:
        return arg[1:-1]
    while arg:
        last = arg[-1]
        if last in _TRAILING or last in _CLOSERS and arg.count(_CLOSERS[last]) < arg.count(last):
            arg = arg[:-1]
        else:
            break
    return arg


def find_references(text: str, kinds: frozenset[str] | None = None) -> list[tuple[str, str, str]]:
    """``(token, kind, argument)`` for each distinct known reference, in order of appearance."""
    known = kinds if kinds is not None else BUILTIN_KINDS | frozenset(_PREFIXES)
    seen: set[str] = set()
    out: list[tuple[str, str, str]] = []
    for match in _TOKEN.finditer(text):
        kind = match.group("kind")
        if kind not in known:
            continue
        raw_arg = match.group("arg") or ""
        arg = _trim(raw_arg)
        token = f"@{kind}:{arg}" if arg else f"@{kind}"
        if token in seen:
            continue
        seen.add(token)
        out.append((token, kind, arg))
    return out


# -- paths and files -------------------------------------------------------------------------------


def _resolve(arg: str, ctx: ReferenceContext) -> tuple[str, str]:
    """``(literal, real)`` absolute paths for ``arg``; ``ReferenceRefused`` when refused."""
    if not arg:
        raise ReferenceRefused("no path was given")
    namespace = nt_namespace_error(arg, verb="Reference")
    if namespace:
        raise ReferenceRefused(namespace)
    expanded = expand_path_text(arg, env=ctx.scope.env)
    literal = os.path.normpath(str(expanded if expanded.is_absolute() else ctx.cwd / expanded))
    real = os.path.realpath(literal)
    for candidate in dict.fromkeys((literal, real)):
        reason = credential_reason(candidate, ctx.scope)
        if reason is not None:
            raise ReferenceRefused(f"{reason}; credentials are never pasted into a conversation")
    if blocked_device(literal):
        raise ReferenceRefused(f"{literal} is a device or process file")
    return literal, real


def _read_file(real: str, shown: str, lines: tuple[int, int | None] | None) -> tuple[str, str]:
    path = Path(real)
    if path.is_dir():
        raise ReferenceRefused(f"{shown} is a folder; use @folder:{shown}")
    if not path.is_file():
        raise ReferenceRefused(f"{shown} does not exist")
    if has_binary_extension(real):
        raise ReferenceRefused(f"{shown} is a binary file")
    size = path.stat().st_size
    limit = MAX_READ_BYTES if lines is not None else MAX_FILE_BYTES
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if b"\x00" in data[:8192]:
        raise ReferenceRefused(f"{shown} is a binary file")
    text = data[:limit].decode("utf-8", errors="replace")
    title = shown
    if lines is not None:
        first, last = lines
        all_lines = text.splitlines(keepends=True)
        end = len(all_lines) if last is None else min(last, len(all_lines))
        if first < 1 or (last is not None and last < first):
            raise ReferenceRefused(f"{first}-{last} is not a line range")
        if first > len(all_lines):
            raise ReferenceRefused(f"{shown} has only {len(all_lines)} lines")
        text = "".join(all_lines[first - 1 : end])
        title = f"{shown}, lines {first}-{end}"
        data = text.encode("utf-8")
        size = len(data)
    if len(text.encode("utf-8")) > MAX_FILE_BYTES:
        cut = text.encode("utf-8")[:MAX_FILE_BYTES].decode("utf-8", errors="ignore")
        text = (
            f"{cut}\n[... truncated: the first {MAX_FILE_BYTES:,} of {size:,} bytes are shown; "
            "read the rest with a file tool]"
        )
    elif size > len(data):
        text += f"\n[... truncated: {size:,} bytes in the file]"
    return title, text


def _listing(real: str, shown: str) -> str:
    root = Path(real)
    if not root.is_dir():
        raise ReferenceRefused(f"{shown} is not a folder")
    lines: list[str] = []
    more = 0

    def visit(folder: Path, depth: int) -> None:
        nonlocal more
        try:
            children = sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError as exc:
            lines.append(f"{'  ' * depth}[cannot list: {exc.strerror or exc}]")
            return
        for child in children:
            if child.is_dir() and child.name in SKIP_DIRS:
                continue
            if len(lines) >= MAX_FOLDER_ENTRIES:
                more += 1
                continue
            is_dir = child.is_dir() and not child.is_symlink()
            lines.append(f"{'  ' * depth}{child.name}{'/' if is_dir else ''}")
            if is_dir and depth + 1 < FOLDER_DEPTH:
                visit(child, depth + 1)

    visit(root, 0)
    if more:
        lines.append(f"[... {more} more entries not listed]")
    return "\n".join(lines) if lines else "(empty folder)"


# -- git -------------------------------------------------------------------------------------------


def _git(ctx: ReferenceContext, *args: str) -> subprocess.CompletedProcess[str]:
    env = child_env(ctx.scope, variant="minimal")
    exe = which("git", path=env.get("PATH"))
    if exe is None:
        raise ReferenceRefused("git is not installed")
    return subprocess.run(
        [exe, "--no-pager", *args],
        cwd=str(ctx.cwd),
        env={**env, "GIT_TERMINAL_PROMPT": "0"},
        capture_output=True,
        text=True,
        errors="replace",
        timeout=GIT_TIMEOUT_S,
        check=False,
    )


def _git_text(ctx: ReferenceContext, *args: str) -> str:
    try:
        done = _git(ctx, *args)
    except subprocess.TimeoutExpired as exc:
        raise ReferenceRefused(f"git took longer than {GIT_TIMEOUT_S:.0f} s") from exc
    if done.returncode != 0:
        first = (done.stderr or "").strip().splitlines()
        raise ReferenceRefused(f"git failed: {first[0] if first else done.returncode}")
    return done.stdout


def _capped(text: str) -> str:
    data = text.encode("utf-8")
    if len(data) <= MAX_GIT_BYTES:
        return text
    cut = data[:MAX_GIT_BYTES].decode("utf-8", errors="ignore")
    return f"{cut}\n[... truncated: {len(data):,} bytes in total]"


def _diff(ctx: ReferenceContext) -> tuple[str, str]:
    root = _git_text(ctx, "rev-parse", "--show-toplevel").strip()
    flags = ("--no-color", "--no-ext-diff")
    try:
        body = _git_text(ctx, "-C", root, "diff", *flags, "HEAD")
    except ReferenceRefused:
        body = _git_text(ctx, "-C", root, "diff", *flags)  # a repository without commits yet
    return f"working-tree diff of {root}", _capped(body) if body.strip() else "(no changes)"


def _commit(ctx: ReferenceContext, ref: str) -> tuple[str, str]:
    if not _GIT_REF.match(ref) or ".." in ref:
        raise ReferenceRefused(f"{ref!r} is not a single commit reference")
    flags = ("--no-color", "--no-ext-diff", "--stat", "--patch", "--format=fuller")
    body = _git_text(ctx, "show", *flags, ref, "--")
    return f"commit {ref}", _capped(body)


# -- urls ------------------------------------------------------------------------------------------


async def _default_extract(url: str, ctx: ReferenceContext) -> str:
    from indus.tools.builtin.web import DEFAULT_TOOLS
    from indus.tools.builtin.web.extract import run_extract

    body = await run_extract(
        DEFAULT_TOOLS.providers, {"urls": [url]}, ctx.scope, resolver=ctx.resolver
    )
    results: Any = body.get("results") or []
    head: object = results[0] if results else {}
    first: Mapping[str, Any] = head if isinstance(head, Mapping) else {}  # pyright: ignore[reportUnknownVariableType]
    content = first.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ReferenceRefused(str(body.get("error") or first.get("error") or "the page was empty"))
    return content


async def _page(ctx: ReferenceContext, arg: str) -> tuple[str, str]:
    if not arg:
        raise ReferenceRefused("no URL was given")
    url = normalize_url(arg)
    verdict = await check_url_async(url, ctx.scope, resolver=ctx.resolver)
    if isinstance(verdict, Blocked):
        raise ReferenceRefused(f"blocked by the URL policy: {verdict.reason}")
    text = await ctx.extract(url) if ctx.extract is not None else await _default_extract(url, ctx)
    return url, _capped(text)


# -- expansion -------------------------------------------------------------------------------------


async def _one(kind: str, arg: str, ctx: ReferenceContext) -> tuple[str, str]:
    """``(title, body)`` for one reference; ``ReferenceRefused`` with the reason."""
    if kind == "file":
        lines: tuple[int, int | None] | None = None
        target = arg
        ranged = _RANGE.match(arg)
        if ranged and not os.path.exists(os.path.join(ctx.cwd, arg)):
            target = ranged.group("path")
            first = int(ranged.group("a"))
            last = int(ranged.group("b")) if ranged.group("b") else first
            lines = (first, last)
        _, real = _resolve(target, ctx)
        return await run_blocking(_read_file, real, target, lines, label="reference file")
    if kind == "folder":
        _, real = _resolve(arg or ".", ctx)
        return arg or ".", await run_blocking(_listing, real, arg or ".", label="reference folder")
    if kind == "diff":
        return await run_blocking(_diff, ctx, label="reference diff")
    if kind == "git":
        if not arg:
            raise ReferenceRefused("no commit was named (use @git:REF)")
        return await run_blocking(_commit, ctx, arg, label="reference commit")
    if kind == "url":
        return await _page(ctx, arg)
    handler = _PREFIXES.get(kind)
    if handler is None:
        raise ReferenceRefused(f"@{kind} is not a known reference")
    return f"@{kind}:{arg}", await handler(arg, ctx)


@dataclass
class _Section:
    budget: int | None
    used: int = 0
    blocks: list[ContextBlock] = field(default_factory=list[ContextBlock])
    refusals: list[Refusal] = field(default_factory=list[Refusal])
    lines: list[str] = field(default_factory=list[str])

    def add(self, token: str, kind: str, title: str, body: str) -> None:
        if self.budget is not None and self.used + len(body) > self.budget:
            note = (
                f"[{token} ({len(body):,} characters) was not inserted: it does not fit the "
                "context budget. Read it with a tool when you need it.]"
            )
            self.blocks.append(ContextBlock(token, kind, title, note, pointer=True))
            self.lines.append(note)
            self.used += len(note)
            return
        self.used += len(body)
        self.blocks.append(ContextBlock(token, kind, title, body))
        self.lines.append(f"--- {token} ({title}) ---\n{body.rstrip()}\n--- end of {token} ---")

    def refuse(self, token: str, reason: str) -> None:
        self.refusals.append(Refusal(token, reason))
        self.lines.append(f"[{token} was not included: {reason}]")


async def expand(
    text: str,
    scope: Scope,
    cwd: Path,
    *,
    budget_chars: int | None = None,
    resolver: Resolver | None = None,
    extract: Callable[[str], Awaitable[str]] | None = None,
) -> Expansion:
    """Expand the references in ``text`` (see the module notes). No token → ``text`` unchanged."""
    if "@" not in text:
        return Expansion(text)
    found = find_references(text)
    if not found:
        return Expansion(text)
    ctx = ReferenceContext(scope=scope, cwd=cwd, resolver=resolver, extract=extract)
    section = _Section(budget_chars)
    for token, kind, arg in found:
        try:
            title, body = await _one(kind, arg, ctx)
        except ReferenceRefused as exc:
            section.refuse(token, str(exc))
            continue
        except (TimeoutError, OSError, UnicodeError) as exc:
            section.refuse(token, f"could not be read ({type(exc).__name__}: {exc})")
            continue
        section.add(token, kind, title, body)
    body = "\n\n".join(section.lines)
    expanded = f"{text}\n\n{SECTION_OPEN}\n{body}\n{SECTION_CLOSE}"
    return Expansion(expanded, tuple(section.blocks), tuple(section.refusals))
