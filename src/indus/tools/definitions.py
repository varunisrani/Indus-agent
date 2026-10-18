"""The tool array a provider call carries, built from a selection of tool names.

``definitions(registry, selection, route)``: names sorted; unknown names and tools whose probe says
no are skipped (each distinct probe runs once per pass); each survivor becomes
``{"type": "function", "function": {name, description, parameters, ...}}`` with its
``schema_overrides`` shallow-merged on top. Then, knowing which tools survived, cross-reference
sentences are added only for tools that are present (R02.21) and per-tool ``rewrite`` hooks run.
Last, the route's schema quirks are fixed on a deep copy (``schema_sanitize``).

The result is memoised by (surviving names, registry generation, profile, quirks, config object);
every caller still gets a new list of new entry dicts, so appending a tool or editing an entry
never leaks into the next call (R01.13).
"""

from __future__ import annotations

import logging
import threading
import weakref
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any, cast

from indus.foundation.scope import Scope, scope_or_none
from indus.tools.registry import Probe, ToolEntry, ToolRegistry, scope_key
from indus.tools.schema_sanitize import Quirk, quirks_for, sanitize

if TYPE_CHECKING:
    from indus.providers.route import RuntimeRoute

log = logging.getLogger(__name__)

MEMO_MAX = 32


class _Same:
    """Identity wrapper so any object (hashable or not) can sit in a memo key."""

    __slots__ = ("obj",)

    def __init__(self, obj: object) -> None:
        self.obj = obj

    def __hash__(self) -> int:
        return id(self.obj)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Same) and other.obj is self.obj


def _copies(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for tool in tools:
        entry = dict(tool)
        fn = entry.get("function")
        if isinstance(fn, dict):
            entry["function"] = dict(cast(dict[str, Any], fn))
        out.append(entry)
    return out


def _function_of(entry: ToolEntry, scope: Scope | None) -> dict[str, Any]:
    fn: dict[str, Any] = {**entry.schema, "name": entry.name}
    if entry.schema_overrides is not None:
        try:
            patch = entry.schema_overrides(scope)
        except Exception as exc:
            log.warning(
                "schema overrides for %s raised %s; static schema used",
                entry.name,
                type(exc).__name__,
            )
            patch = None
        if isinstance(patch, Mapping):
            fn.update(patch)
            fn["name"] = entry.name
    return fn


def _with_cross_refs(entry: ToolEntry, fn: dict[str, Any], present: frozenset[str]) -> None:
    added = [ref.text.strip() for ref in entry.cross_refs if ref.tool in present]
    if not added:
        return
    base = str(fn.get("description", "")).rstrip()
    fn["description"] = " ".join([base, *added]) if base else " ".join(added)


class ToolDefinitions:
    """Memoised definition builder for one registry."""

    def __init__(self, registry: ToolRegistry, *, max_entries: int = MEMO_MAX) -> None:
        self._registry = registry
        self._max = max_entries
        self._memo: OrderedDict[tuple[Any, ...], list[dict[str, Any]]] = OrderedDict()
        self._lock = threading.Lock()

    def invalidate(self) -> None:
        with self._lock:
            self._memo.clear()

    def __len__(self) -> int:
        return len(self._memo)

    def build(
        self,
        selection: Iterable[str],
        route: RuntimeRoute | None = None,
        *,
        scope: Scope | None = None,
    ) -> list[dict[str, Any]]:
        resolved = scope if scope is not None else scope_or_none()
        memo: dict[Probe, bool] = {}
        survivors: list[ToolEntry] = []
        for name in sorted(set(selection)):
            entry = self._registry.get(name, resolved)
            if entry is None:
                continue
            if not self._registry.available(entry, resolved, memo):
                log.debug("tool %s left out: its probe says it is unavailable", name)
                continue
            survivors.append(entry)
        present = frozenset(e.name for e in survivors)
        quirks = quirks_for(route)
        key = (
            present,
            self._registry.generation,
            scope_key(resolved) if resolved is not None else None,
            quirks,
            _Same(resolved.config if resolved is not None else None),
        )
        with self._lock:
            cached = self._memo.get(key)
            if cached is not None:
                self._memo.move_to_end(key)
                return _copies(cached)
        built = sanitize(self._assemble(survivors, present, resolved), quirks)
        with self._lock:
            self._memo[key] = built
            while len(self._memo) > self._max:
                self._memo.popitem(last=False)
        return _copies(built)

    def _assemble(
        self, survivors: list[ToolEntry], present: frozenset[str], scope: Scope | None
    ) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        for entry in survivors:
            fn = _function_of(entry, scope)
            _with_cross_refs(entry, fn, present)
            if entry.rewrite is not None:
                try:
                    rewritten = entry.rewrite(fn, present)
                except Exception as exc:
                    log.warning(
                        "schema rewrite for %s raised %s; tool left out",
                        entry.name,
                        type(exc).__name__,
                    )
                    continue
                if rewritten is None:
                    continue
                fn = {**rewritten, "name": entry.name}
            tools.append({"type": "function", "function": fn})
        return tools


_SHARED: weakref.WeakKeyDictionary[ToolRegistry, ToolDefinitions] = weakref.WeakKeyDictionary()
_SHARED_LOCK = threading.Lock()


def definitions_for(registry: ToolRegistry) -> ToolDefinitions:
    with _SHARED_LOCK:
        builder = _SHARED.get(registry)
        if builder is None:
            builder = _SHARED[registry] = ToolDefinitions(registry)
        return builder


def definitions(
    registry: ToolRegistry,
    selection: Iterable[str],
    route: RuntimeRoute | None = None,
    *,
    scope: Scope | None = None,
) -> list[dict[str, Any]]:
    """The provider-neutral tool array for ``selection`` (see the module docstring)."""
    return definitions_for(registry).build(selection, route, scope=scope)


__all__ = ["Quirk", "ToolDefinitions", "definitions", "definitions_for"]
