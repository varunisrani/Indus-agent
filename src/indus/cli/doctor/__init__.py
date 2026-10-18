"""The ``indus doctor`` check registry (P07-19).

A check is a function ``check(ctx) -> list[Finding]`` registered under an id, a section title and
an order number; ``all_checks()`` lists them in order. The built-in checks live in the
``indus.cli.doctor.checks`` module (loaded on first use); later phases add theirs with
``register``.

Severities: ``ok`` and ``info`` are not problems; ``warn`` and ``error`` are. A check run with
``ctx.fix`` may repair what it found and report it as a finding with ``fixed=True`` (severity
``ok``). A check that raises becomes one ``error`` finding naming it, and the run goes on.
"""

from __future__ import annotations

import importlib
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from indus.foundation.scope import Scope

__all__ = [
    "PROBLEMS",
    "Check",
    "CheckSpec",
    "DoctorContext",
    "Finding",
    "Severity",
    "Summary",
    "all_checks",
    "register",
    "run_checks",
    "summarise",
    "unregister",
]

log = logging.getLogger(__name__)

Severity = Literal["ok", "info", "warn", "error"]
PROBLEMS: frozenset[str] = frozenset({"warn", "error"})
BUILTIN_MODULE = "indus.cli.doctor.checks"


@dataclass(frozen=True)
class Finding:
    id: str
    severity: Severity
    message: str
    fix: str | None = None
    fixed: bool = False

    @property
    def problem(self) -> bool:
        return self.severity in PROBLEMS


@dataclass
class DoctorContext:
    scope: Scope
    fix: bool = False
    live: bool = False
    extras: dict[str, Any] = field(default_factory=lambda: {})


type Check = Callable[[DoctorContext], list[Finding]]


@dataclass(frozen=True)
class CheckSpec:
    id: str
    title: str
    order: int
    run: Check


_lock = threading.Lock()
_registry: dict[str, CheckSpec] = {}
_builtin_loaded = False


def register(check_id: str, title: str, *, order: int) -> Callable[[Check], Check]:
    """Decorator: add ``check`` to the run under ``check_id`` (an id registered twice raises)."""

    def decorate(fn: Check) -> Check:
        with _lock:
            if check_id in _registry:
                raise ValueError(f"doctor check {check_id!r} is registered twice")
            _registry[check_id] = CheckSpec(check_id, title, order, fn)
        return fn

    return decorate


def unregister(check_id: str) -> bool:
    with _lock:
        return _registry.pop(check_id, None) is not None


def _load_builtin() -> None:
    global _builtin_loaded
    if _builtin_loaded:
        return
    importlib.import_module(BUILTIN_MODULE)
    _builtin_loaded = True


def all_checks() -> list[CheckSpec]:
    _load_builtin()
    with _lock:
        return sorted(_registry.values(), key=lambda s: (s.order, s.id))


def run_checks(ctx: DoctorContext) -> list[tuple[CheckSpec, list[Finding]]]:
    out: list[tuple[CheckSpec, list[Finding]]] = []
    for spec in all_checks():
        try:
            found = spec.run(ctx)
        except Exception as exc:
            log.warning("doctor check %s failed", spec.id, exc_info=True)
            found = [
                Finding(
                    spec.id,
                    "error",
                    f"the {spec.id} check itself failed: {type(exc).__name__}: {exc}",
                )
            ]
        out.append((spec, found))
    return out


@dataclass(frozen=True)
class Summary:
    fixed: int
    remaining: list[Finding]

    @property
    def exit_code(self) -> int:
        return 1 if self.remaining else 0


def summarise(results: list[tuple[CheckSpec, list[Finding]]]) -> Summary:
    found = [f for _spec, items in results for f in items]
    return Summary(sum(1 for f in found if f.fixed), [f for f in found if f.problem])
