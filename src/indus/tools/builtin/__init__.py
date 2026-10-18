"""The built-in tool modules, listed by hand (D-16.73: no scanning of module source).

Each listed module defines ``register_tools(registry)`` and registers its tools there; importing a
module has no side effects. ``load_builtins`` imports the list in order. A module marked optional
that fails to import or register is logged and skipped, so one missing extra never takes the other
tools down; a required module's failure is raised. Phases add their modules to ``BUILTIN_MODULES``
(the core tools arrive in P05).
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Iterable
from dataclasses import dataclass

from indus.tools.registry import ToolRegistry

log = logging.getLogger(__name__)

REGISTER_HOOK = "register_tools"


@dataclass(frozen=True)
class BuiltinModule:
    path: str
    optional: bool = True


BUILTIN_MODULES: tuple[BuiltinModule, ...] = ()


def load_builtins(
    registry: ToolRegistry, modules: Iterable[BuiltinModule] = BUILTIN_MODULES
) -> list[str]:
    """Import each module and let it register; return the paths that loaded."""
    loaded: list[str] = []
    for module in modules:
        try:
            imported = importlib.import_module(module.path)
            hook = getattr(imported, REGISTER_HOOK)
            hook(registry)
        except Exception as exc:
            if not module.optional:
                raise
            log.warning(
                "built-in tool module %s skipped: %s: %s", module.path, type(exc).__name__, exc
            )
            continue
        loaded.append(module.path)
    return loaded
