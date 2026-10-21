"""The facade of the helper-binary manager (``indus pm``, P24-23).

Consumers ask this module for a tool and never touch pm internals (``indus.pm.fetch``,
``indus.pm.store``, ``indus.pm.lock`` are banned outside ``indus.pm`` by ruff's ``banned-api``):

- ``ensure(scope, name)`` installs ``name`` as the lock says (when needed) and returns a
  ``Runner`` (executable, ``PATH`` folders, ``env(scope, child_env)``);
- ``tool_path(scope, name)`` is the installed executable or None, never installing;
- ``env_for(scope, *names)`` is the exports of what is installed.

Everything resolves lazily (PEP 562): importing ``indus.pm.api`` loads neither the store nor the
downloader, so boot and ``--help`` stay cheap. (``indus.pm`` itself holds only its docstring, as
every layer package does.)
"""

from __future__ import annotations

import importlib
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from indus.foundation.scope import Scope
    from indus.pm.core import EnvBuilder, Package, Pm, Runner, current_target, definitions
    from indus.pm.lock import PmError

__all__ = [
    "EnvBuilder",
    "Package",
    "Pm",
    "PmError",
    "Runner",
    "current_target",
    "definitions",
    "ensure",
    "env_for",
    "manager",
    "tool_path",
]

_LAZY = {
    "EnvBuilder": "indus.pm.core",
    "Package": "indus.pm.core",
    "Pm": "indus.pm.core",
    "Runner": "indus.pm.core",
    "current_target": "indus.pm.core",
    "definitions": "indus.pm.core",
    "PmError": "indus.pm.lock",
}

_managers: dict[Path, Any] = {}
_managers_lock = threading.Lock()


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module 'indus.pm.api' has no attribute {name!r}")
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_LAZY})


def manager(scope: Scope) -> Pm:
    """The root's ``Pm`` (one per root per process, so its stat caches are reused), with the
    profile's current ``pm.*`` settings."""
    from indus.pm.core import Pm

    fresh = Pm.for_scope(scope)
    with _managers_lock:
        held = _managers.get(scope.root)
        if held is None:
            _managers[scope.root] = fresh
            return fresh
    if (held.mirror, held.timeout) != (fresh.mirror, fresh.timeout):
        held.close()  # the next download builds a fetcher with the new settings
    held.env, held.mirror, held.timeout, held.clock = (
        fresh.env,
        fresh.mirror,
        fresh.timeout,
        fresh.clock,
    )
    return held


def ensure(scope: Scope, name: str) -> Runner:
    """Install ``name`` when the facts do not match the lock; return its runner."""
    return manager(scope).ensure(name)


def tool_path(scope: Scope, name: str) -> Path | None:
    """The installed executable of ``name``, or None (never installs)."""
    runner = manager(scope).runner(name)
    return runner.executable if runner is not None else None


def env_for(scope: Scope, *names: str) -> dict[str, list[str]]:
    """``{"PATH": [...]}`` contributed by the installed packages among ``names``."""
    return manager(scope).exports(names)
