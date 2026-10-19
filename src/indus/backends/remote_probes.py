# pyright: strict
"""Flapping guard and unscoped rule for the cloud backends' probes (P21-12, R02.13, R02.29).

The rows ``singularity``, ``modal``, ``daytona`` and ``vercel_sandbox`` probe without the network
(CLI on PATH, SDK extra installed, credentials present). Those lookups can still wobble: a secret
store that times out once makes a token look missing. ``GuardedProbe`` keeps a short history per
profile so one wobble does not take the terminal away:

* a success is remembered as last-good and ends any failure streak;
* a failure within ``FLAP_GRACE_S`` of the last success is served as last-good (available) and
  starts a streak; further failures are served too while the streak is younger than
  ``CONFIRM_GAP_S``;
* a failure once the streak is ``CONFIRM_GAP_S`` old, or with no success in ``FLAP_GRACE_S``, is
  honoured: its reason is returned and last-good is dropped, so only a new success brings the
  backend back.

A probe asked with no scope (a host with no profile yet), or whose check raises
``ScopeUnresolved``, logs quietly, records nothing and answers ``UNSCOPED_REASON``; the first call
with a scope runs the check for real (R02.29).
"""

from __future__ import annotations

import dataclasses
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from indus.foundation.clock import Clock
from indus.foundation.errors import ScopeUnresolved
from indus.foundation.scope import Scope

if TYPE_CHECKING:
    from indus.backends.registry import BackendEntry

__all__ = [
    "CONFIRM_GAP_S",
    "FLAP_GRACE_S",
    "GUARDED_BACKENDS",
    "UNSCOPED_REASON",
    "GuardedProbe",
    "guard_entry",
    "guarded_probe",
    "reset_guards",
]

log = logging.getLogger(__name__)

FLAP_GRACE_S = 120.0
CONFIRM_GAP_S = 30.0
GUARDED_BACKENDS = ("singularity", "modal", "daytona", "vercel_sandbox")
UNSCOPED_REASON = (
    "not checked yet: no profile is active here; it is checked again on the first turn"
)

type Check = Callable[[Scope], str | None]


@dataclass(slots=True)
class _History:
    last_ok: float | None = None
    streak_from: float | None = None
    reason: str | None = None


class GuardedProbe:
    """One row's probe with the flapping guard (see the module notes)."""

    def __init__(
        self,
        name: str,
        check: Check,
        *,
        clock: Clock | None = None,
        grace_s: float = FLAP_GRACE_S,
        confirm_s: float = CONFIRM_GAP_S,
    ) -> None:
        self.name = name
        self.check = check
        self.clock = clock
        self.grace_s = grace_s
        self.confirm_s = confirm_s
        self._lock = threading.Lock()
        self._history: dict[str, _History] = {}
        self._unscoped_seen = False

    def clear(self) -> None:
        with self._lock:
            self._history.clear()
            self._unscoped_seen = False

    def last_reason(self, scope: Scope) -> str | None:
        """What the latest failure for this profile said (None after a success or none yet)."""
        with self._lock:
            history = self._history.get(str(scope.home))
            return history.reason if history is not None else None

    def _unscoped(self, why: str) -> str:
        with self._lock:
            self._unscoped_seen = True
        log.debug("%s probe not run (%s); checked again on the first scoped turn", self.name, why)
        return UNSCOPED_REASON

    def __call__(self, scope: Scope | None) -> str | None:
        if scope is None:
            return self._unscoped("no profile scope")
        with self._lock:
            if self._unscoped_seen:
                self._unscoped_seen = False
                log.info("%s probe runs now that a profile scope exists", self.name)
        clock = self.clock if self.clock is not None else scope.clock
        try:
            problem = self.check(scope)
        except ScopeUnresolved:
            return self._unscoped("the check lost its profile scope")
        except Exception as exc:  # a probe answers, never raises
            problem = f"the {self.name} check failed: {type(exc).__name__}: {exc}"
        now = clock.monotonic()
        with self._lock:
            history = self._history.setdefault(str(scope.home), _History())
            if problem is None:
                history.last_ok = now
                history.streak_from = None
                history.reason = None
                return None
            history.reason = problem
            recent = history.last_ok is not None and now - history.last_ok < self.grace_s
            if history.streak_from is None:
                history.streak_from = now
            young = now - history.streak_from < self.confirm_s
            if recent and young:
                log.warning(
                    "%s probe failed %.0f s after it passed (%s); serving the last good answer",
                    self.name,
                    now - (history.last_ok or now),
                    problem,
                )
                return None
            history.last_ok = None
            return problem


_GUARDS: dict[str, GuardedProbe] = {}
_GUARDS_LOCK = threading.Lock()


def guarded_probe(name: str, check: Check | None = None) -> GuardedProbe | None:
    """The process-wide guard of row ``name`` (created around ``check`` on first use)."""
    with _GUARDS_LOCK:
        guard = _GUARDS.get(name)
        if guard is None and check is not None:
            guard = _GUARDS[name] = GuardedProbe(name, check)
        return guard


def reset_guards() -> None:
    """Forget every guard's history (tests, and after a config or secret change)."""
    with _GUARDS_LOCK:
        guards = list(_GUARDS.values())
    for guard in guards:
        guard.clear()


def guard_entry(entry: BackendEntry) -> BackendEntry:
    """``entry`` with its probe behind the process-wide guard of its name (unchanged when the row
    has no probe or is not one of ``GUARDED_BACKENDS``)."""
    if (
        entry.probe is None
        or entry.name not in GUARDED_BACKENDS
        or isinstance(entry.probe, GuardedProbe)
    ):
        return entry
    guard = guarded_probe(entry.name, entry.probe)
    if guard is None:  # pragma: no cover - created just above
        return entry
    guard.check = entry.probe
    return dataclasses.replace(entry, probe=guard)
