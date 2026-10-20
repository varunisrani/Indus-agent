"""One session's own tools over the shared registry (P19-13, the daemon's editor MCP servers).

A ``SessionRegistry`` is handed to one agent instead of the process's shared ``ToolRegistry``. Every
read sees both: the shared tools (built-ins, plugins, the profile's MCP servers) and the ones
registered into this registry, which win on a name clash. Every write — ``register``,
``deregister``, ``set_toolset_alias`` — lands here only, so tools an editor's MCP servers bring to
one session never reach another session of the profile, not even one that selects every toolset.

``generation`` moves when either side changes (the toolset resolver and the definitions memo key on
it); availability probes and their cache are the shared registry's.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from indus.tools.registry import ToolEntry, ToolRegistry

if TYPE_CHECKING:
    from indus.foundation.scope import Scope

__all__ = ["SessionRegistry"]


class SessionRegistry(ToolRegistry):
    """See the module notes."""

    def __init__(self, base: ToolRegistry) -> None:
        super().__init__()
        self.base = base
        self.probes = base.probes

    @property
    def generation(self) -> int:
        return self.base.generation + self._generation

    def own_names(self) -> list[str]:
        """Tools registered into this registry (any overlay)."""
        with self._lock:
            names = set(self._global)
            for overlay in self._overlays.values():
                names.update(overlay)
        return sorted(names)

    def _merged(self, scope: Scope | None) -> dict[str, ToolEntry]:
        with self.base._lock:
            merged = self.base._merged(scope)
        merged.update(super()._merged(scope))
        return merged

    def _alias_table(self, scope: Scope | None) -> dict[str, frozenset[str]]:
        with self.base._lock:
            merged = self.base._alias_table(scope)
        for alias, targets in super()._alias_table(scope).items():
            merged[alias] = merged.get(alias, frozenset()) | targets
        return merged

    def get(self, name: str, scope: Scope | None = None) -> ToolEntry | None:
        found = super().get(name, scope)
        return found if found is not None else self.base.get(name, scope)
