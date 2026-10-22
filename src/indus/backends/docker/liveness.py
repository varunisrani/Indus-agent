# pyright: strict
"""The host's liveness answers for the Docker orphan sweep (``reaper``), kept apart from it.

``service.terminal_host`` installs them at every start; holding them here means a run that never
touches Docker does not import the Docker backend to do so (W48-g-3). Until a host answers, every
session counts as alive and no task has a live job, so running containers are kept.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

__all__ = ["Alive", "current", "reset", "set_liveness"]

type Alive = Callable[[str], bool]


def _always(_: str) -> bool:
    return True


def _never(_: str) -> bool:
    return False


_lock = threading.Lock()
_liveness: dict[str, Alive] = {"session": _always, "jobs": _never}


def set_liveness(*, session_alive: Alive | None = None, jobs_alive: Alive | None = None) -> None:
    """The host's answers: is the session with this id still open, does this task id have a
    running background job."""
    with _lock:
        if session_alive is not None:
            _liveness["session"] = session_alive
        if jobs_alive is not None:
            _liveness["jobs"] = jobs_alive


def current() -> tuple[Alive, Alive]:
    """The installed ``(session_alive, jobs_alive)`` answers."""
    with _lock:
        return _liveness["session"], _liveness["jobs"]


def reset() -> None:
    """Back to the defaults (tests)."""
    with _lock:
        _liveness.update(session=_always, jobs=_never)
