# pyright: strict
"""What only the host can tell the backends: session lineage and liveness (P11 integration).

The backends answer some questions from the session store, which they never open themselves:

- ``process_receipts`` lets a conversation read the receipt of a job its owner started only when
  it is the owner or a compression continuation of it (``lineage_ids`` of the caller's chain);
- the Docker orphan sweep removes a running session container only when that session is over and
  no background job of this process still runs for it.

``install(store, processes)`` points both at the host's store and process registry; a later call
(the next run in the same process) replaces them. Every answer fails safe: a store error, an
ambiguous chain or a session the store does not know counts as "still alive" for the sweep and
as "not a continuation" for receipts. A missing row is not taken as "gone" because one process
can serve several profiles while these answers are process-wide.

The lost-job hook of Docker recovery needs no store; ``TerminalRuntime`` registers it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from indus.backends.docker import liveness
from indus.backends.process_receipts import set_lineage_resolver
from indus.state import compression, sessions
from indus.state.registry import StoreHandle

if TYPE_CHECKING:
    from indus.backends.processes import ProcessRegistry

__all__ = ["install", "is_continuation", "jobs_alive", "session_alive"]

log = logging.getLogger(__name__)


def is_continuation(store: StoreHandle, owner: str, caller: str) -> bool:
    """Is ``caller`` the conversation ``owner`` started, after compression?"""
    if not owner or not caller:
        return False
    try:
        return owner in compression.lineage_ids(store, caller)
    except Exception:
        log.debug("lineage lookup failed for %s", caller, exc_info=True)
        return False


def session_alive(store: StoreHandle, task: str) -> bool:
    """False only when the store says the session (its newest segment) has ended."""
    try:
        newest = compression.tip(store, task)
        row = sessions.get(store, newest)
    except Exception:
        log.debug("session lookup failed for %s", task, exc_info=True)
        return True
    if row is None:
        return True
    return row.get("ended_at") is None


def jobs_alive(processes: ProcessRegistry, task: str) -> bool:
    """Does a background job of this process still run for the container task id ``task``?"""
    # Imported here: only the Docker sweep asks, and a run without Docker never loads it (W48-g-3).
    from indus.backends.docker.environment import label_value

    for record in processes.running():
        if record.session_id and label_value(record.session_id) == task:
            return True
        if f":{task}@" in record.env_key:
            return True
    return False


def install(store: StoreHandle, processes: ProcessRegistry) -> None:
    """Wire the receipts lineage check and the orphan sweep's liveness to this host."""
    set_lineage_resolver(lambda owner, caller: is_continuation(store, owner, caller))
    liveness.set_liveness(
        session_alive=lambda task: session_alive(store, task),
        jobs_alive=lambda task: jobs_alive(processes, task),
    )
