"""Host gating through one marker: ``@pytest.mark.platforms(*specs)`` (P23-01, R10.21).

A spec is ``linux``, ``macos``, ``windows`` or ``posix`` (Linux or macOS), optionally written
``not <name>``. A test runs when the host matches at least one of its specs; on any other host it is
deselected (it never shows as a skip). A test may carry one ``platforms`` mark only — two (say one
on the module and one on the function) are a usage error, because "any of" across two marks reads
like "all of" and nobody can tell which was meant. Bare ``skipif(sys.platform …)`` marks are
reported by ``tests/unit/packaging/test_platform_marks.py``.

``tests/conftest.py`` calls `apply` from its collection hook; this module also works as a plugin
(``-p tests.platform_marks``) for the child pytest runs that test it.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

import pytest

MARK = "platforms"
HOSTS = ("linux", "macos", "windows")
SPECS = (*HOSTS, "posix")


def host_name(platform: str = sys.platform) -> str:
    """``linux``, ``macos`` or ``windows`` for a ``sys.platform`` value (others count as linux)."""
    if platform == "darwin":
        return "macos"
    if platform in ("win32", "cygwin"):
        return "windows"
    return "linux"


def _single(spec: str, host: str) -> bool:
    if spec == "posix":
        return host in ("linux", "macos")
    return spec == host


def check_spec(spec: object) -> str | None:
    """Why ``spec`` is not a valid platforms spec, or None."""
    if not isinstance(spec, str):
        return f"spec {spec!r} is not a string"
    words = spec.split()
    if len(words) == 2 and words[0] == "not":
        words = words[1:]
    if len(words) != 1 or words[0] not in SPECS:
        return f"spec {spec!r} is not one of {', '.join(SPECS)} (optionally 'not <name>')"
    return None


def host_matches(specs: Sequence[str], host: str) -> bool:
    """Whether ``host`` matches at least one spec."""
    for spec in specs:
        words = spec.split()
        if words[0] == "not":
            if not _single(words[1], host):
                return True
        elif _single(words[0], host):
            return True
    return False


def problem(item: pytest.Item) -> str | None:
    """A usage problem with ``item``'s platforms marks, or None."""
    marks = list(item.iter_markers(MARK))
    if not marks:
        return None
    if len(marks) > 1:
        return f"{item.nodeid}: {len(marks)} platforms marks; put every spec in one mark"
    mark = marks[0]
    if mark.kwargs:
        return f"{item.nodeid}: platforms takes specs only, not {sorted(mark.kwargs)}"
    if not mark.args:
        return f"{item.nodeid}: platforms needs at least one spec"
    for spec in mark.args:
        found = check_spec(spec)
        if found is not None:
            return f"{item.nodeid}: {found}"
    return None


def apply(config: pytest.Config, items: list[pytest.Item], host: str | None = None) -> None:
    """Refuse bad marks, then deselect the tests this host does not match."""
    problems = [found for item in items if (found := problem(item)) is not None]
    if problems:
        raise pytest.UsageError("bad platforms marks:\n  " + "\n  ".join(problems))
    here = host if host is not None else host_name()
    kept: list[pytest.Item] = []
    dropped: list[pytest.Item] = []
    for item in items:
        mark = item.get_closest_marker(MARK)
        if mark is None or host_matches([str(a) for a in mark.args], here):
            kept.append(item)
        else:
            dropped.append(item)
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = kept


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    apply(config, items)
