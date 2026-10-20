"""``GET /api/logs``: the tail of one of the profile's log files, filtered and redacted (P18-13).

Query: ``file`` (``agent``, ``errors``, ``gateway``; default ``agent``), ``lines`` (50, 100, 200 or
500; default 100), ``level`` (the lowest severity shown: ``DEBUG``, ``INFO``, ``WARNING``,
``ERROR``; ``ALL`` or absent shows everything) and ``component`` (``agent``, ``tools``,
``gateway``, ``cli``, ``cron``; ``all`` or absent shows everything). Lines follow P01's format
(``YYYY-MM-DD HH:MM:SS LEVEL logger [tags]: message``); a line without that head (a traceback
frame, the rest of a long message) belongs to the record above it and shares its verdict.

Only the last ``TAIL_BYTES`` of a file are read. Every line that leaves goes through the profile's
redactor with redaction forced on, so a key that reached a log line never reaches the page. A
file that does not exist yet is an empty list. The filter rules restate ``indus logs``'s (the CLI
layer is not importable from here).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Annotated, Any, Final, NoReturn

from fastapi import APIRouter, Depends, HTTPException

from indus.foundation.log import logs_dir
from indus.foundation.redact import Redactor
from indus.foundation.scope import Scope
from indus.web.scope import read_scope

__all__ = ["COMPONENTS", "LEVELS", "LOG_FILES", "ROUTE_LIST", "TAILS", "filter_lines", "router"]

router = APIRouter()

ROUTE_LIST: Final[tuple[tuple[str, str, bool], ...]] = (("GET", "/api/logs", True),)
"""``(method, path, scoped)`` of every route here."""

ReadScope = Annotated[Scope, Depends(read_scope)]

LOG_FILES: Final[dict[str, str]] = {
    "agent": "agent.log",
    "errors": "errors.log",
    "gateway": "gateway.log",
}
LEVELS: Final[dict[str, int]] = {
    "DEBUG": 10,
    "INFO": 20,
    "WARNING": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}
COMPONENTS: Final[dict[str, tuple[str, ...]]] = {
    "agent": ("indus.core", "indus.providers", "indus.learning"),
    "tools": ("indus.tools", "indus.backends", "indus.safety"),
    "gateway": ("indus.gateway",),
    "cli": ("indus.cli", "indus.service"),
    "cron": ("indus.cron",),
}
TAILS: Final = (50, 100, 200, 500)
TAIL_BYTES: Final = 8 * 1024 * 1024
_EVERYTHING: Final = frozenset({"", "all"})
_RECORD_HEAD: Final = re.compile(
    r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}\S*\s+"
    r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+(?P<name>[^\s:\[]+)"
)


def _fail(code: str, message: str) -> NoReturn:
    raise HTTPException(status_code=400, detail={"code": code, "message": message})


def _record_passes(
    level: int, name: str, min_level: int | None, prefixes: tuple[str, ...] | None
) -> bool:
    if min_level is not None and level < min_level:
        return False
    if prefixes is not None:
        return any(name == p or name.startswith(p + ".") for p in prefixes)
    return True


def filter_lines(
    lines: Iterable[str], min_level: int | None, prefixes: tuple[str, ...] | None
) -> list[str]:
    """The lines whose record passes; continuation lines follow their record. Lines before the
    first record head pass only when nothing filters."""
    kept: list[str] = []
    verdict: bool | None = None
    unfiltered = min_level is None and prefixes is None
    for line in lines:
        head = _RECORD_HEAD.match(line)
        if head is not None:
            verdict = _record_passes(
                LEVELS[head.group("level")], head.group("name"), min_level, prefixes
            )
        if verdict if verdict is not None else unfiltered:
            kept.append(line)
    return kept


def _read_tail(path: Path) -> list[str]:
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, 2)
            if size > TAIL_BYTES:
                handle.seek(size - TAIL_BYTES)
                handle.readline()  # the first line is cut; drop it
            else:
                handle.seek(0)
            data = handle.read()
    except FileNotFoundError:
        return []
    return data.decode("utf-8", errors="replace").splitlines()


def _choice(value: str | None, allowed: Iterable[str], code: str, what: str) -> str:
    text = (value or "").strip()
    if text.lower() in _EVERYTHING:
        return ""
    for option in allowed:
        if text.lower() == option.lower():
            return option
    _fail(code, f"{what} must be one of: {', '.join(allowed)} (or all).")


@router.get("/api/logs")
def get_logs(
    scope: ReadScope,
    file: str = "agent",
    lines: int = 100,
    level: str | None = None,
    component: str | None = None,
) -> dict[str, Any]:
    if file not in LOG_FILES:
        _fail("invalid_file", f"file must be one of: {', '.join(LOG_FILES)}.")
    if lines not in TAILS:
        _fail("invalid_lines", f"lines must be one of: {', '.join(str(t) for t in TAILS)}.")
    chosen_level = _choice(level, LEVELS, "invalid_level", "level")
    chosen_component = _choice(component, COMPONENTS, "invalid_component", "component")
    min_level = LEVELS[chosen_level] if chosen_level else None
    prefixes = COMPONENTS[chosen_component] if chosen_component else None
    found = _read_tail(logs_dir(scope.home) / LOG_FILES[file])
    if min_level is not None or prefixes is not None:
        found = filter_lines(found, min_level, prefixes)
    tail = found[-lines:]
    redactor = Redactor(scope)
    return {"file": file, "lines": [redactor.redact(line, force=True) for line in tail]}
