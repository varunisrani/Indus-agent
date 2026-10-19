# pyright: strict
"""Names, filters and description hygiene for MCP tools (P13-02).

A server tool is registered as ``mcp__<server>__<tool>``: in each component every character
outside ``[A-Za-z0-9_]`` becomes ``_`` (D-16.24). Providers refuse function names over 64
characters, so a longer name keeps its head and ends in ``_`` + the first eight hex digits of the
SHA-256 of the whole unclamped name (R07.21); the handler keeps the server's own tool name, so
the clamp never changes what is called.

Filters: with ``include`` set (an empty list too) only matching tools pass and ``exclude`` is
ignored; otherwise every tool except the ``exclude`` matches. An entry containing ``*``, ``?`` or
``[`` is a case-sensitive glob; any other entry must equal the name.

Descriptions lose Unicode TAG characters (U+E0000..U+E007F, an invisible text channel) except
inside a valid flag sequence, so subdivision flags keep rendering.
"""

from __future__ import annotations

import fnmatch
import hashlib
import logging
import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from indus.tools.schema_sanitize import sanitize_parameters

__all__ = [
    "MAX_NAME_CHARS",
    "PREFIX",
    "NameFilter",
    "component",
    "describe",
    "is_glob",
    "matches",
    "strip_tags",
    "tool_name",
    "tool_parameters",
    "tool_schema",
]

log = logging.getLogger(__name__)

PREFIX = "mcp__"
MAX_NAME_CHARS = 64
_SUFFIX_HEX = 8
_OUTSIDE_SAFE = re.compile(r"[^A-Za-z0-9_]")
_GLOB_MARKS = ("*", "?", "[")

_TAG_LOW, _TAG_HIGH = "\U000e0000", "\U000e007f"
# A flag sequence: black flag, one or more tag letters/digits, the cancel tag. Kept whole;
# any other TAG-block character is dropped.
_TAG_OR_FLAG = re.compile("\U0001f3f4[\U000e0020-\U000e007e]+\U000e007f|[\U000e0000-\U000e007f]")

_clamp_lock = threading.Lock()
_clamp_warned: set[str] = set()


def component(text: str) -> str:
    """One name part with every character outside ``[A-Za-z0-9_]`` replaced by ``_``."""
    return _OUTSIDE_SAFE.sub("_", text)


def tool_name(server: str, tool: str) -> str:
    """The registry and wire name for ``tool`` of ``server`` (at most 64 characters)."""
    full = f"{PREFIX}{component(server)}__{component(tool)}"
    if len(full) <= MAX_NAME_CHARS:
        return full
    digest = hashlib.sha256(full.encode("utf-8")).hexdigest()[:_SUFFIX_HEX]
    with _clamp_lock:
        first = full not in _clamp_warned
        _clamp_warned.add(full)
    if first:
        log.warning(
            "MCP tool name %s is %d characters; registered under a shortened name (limit %d)",
            full,
            len(full),
            MAX_NAME_CHARS,
        )
    keep = MAX_NAME_CHARS - _SUFFIX_HEX - 1
    return f"{full[:keep]}_{digest}"


def is_glob(entry: str) -> bool:
    return any(mark in entry for mark in _GLOB_MARKS)


def matches(name: str, entries: Iterable[str]) -> bool:
    """True when ``name`` equals a plain entry or fits a glob entry (case-sensitive)."""
    for entry in entries:
        if entry == name or (is_glob(entry) and fnmatch.fnmatchcase(name, entry)):
            return True
    return False


@dataclass(frozen=True)
class NameFilter:
    """The ``tools.include`` / ``tools.exclude`` rule of one server."""

    include: tuple[str, ...] | None = None
    exclude: tuple[str, ...] = ()

    @classmethod
    def of(cls, include: Iterable[str] | None, exclude: Iterable[str] | None) -> NameFilter:
        return cls(
            tuple(include) if include is not None else None,
            tuple(exclude) if exclude is not None else (),
        )

    def __call__(self, name: str) -> bool:
        if self.include is not None:
            return matches(name, self.include)
        return not matches(name, self.exclude)


def strip_tags(text: str) -> str:
    """``text`` without Unicode TAG characters, flag sequences left intact."""
    if not any(_TAG_LOW <= ch <= _TAG_HIGH for ch in text):
        return text
    return _TAG_OR_FLAG.sub(lambda m: m.group(0) if len(m.group(0)) > 1 else "", text)


def describe(server: str, tool: str, description: str | None) -> str:
    """The description the model sees: TAG characters removed, generic text when empty."""
    cleaned = strip_tags(description or "").strip()
    return cleaned or f"Tool {tool} from the MCP server {server}."


def tool_parameters(input_schema: object) -> dict[str, Any]:
    """The server's input schema as a parameters object, after P04's always-on repairs.

    Route-specific fixes happen later, when the tool list is built for a request.
    """
    return sanitize_parameters(input_schema, ())


def tool_schema(
    server: str, tool: str, description: str | None, input_schema: object
) -> dict[str, Any]:
    """``{name, description, parameters}`` for registering one server tool."""
    return {
        "name": tool_name(server, tool),
        "description": describe(server, tool, description),
        "parameters": tool_parameters(input_schema),
    }
