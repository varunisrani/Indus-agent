"""One profile's cron services wired together: store, ledger database, ledger, notepad, health.

``CronContext.open(scope)`` is what the ticker, the CLI and the tool start from (through the host's
backend). Removing a job through ``context.store`` also clears its notepad; its store refuses a
``script`` that looks like a command line (``script_rule``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from indus.cron.errors import InvalidJob
from indus.cron.ledger import ExecutionLedger, ExecutionsDB, ProcessProbe
from indus.cron.notepad import Notepad
from indus.cron.script import check_script_value
from indus.cron.store import CronStore
from indus.cron.store_health import HealthEvent, StoreHealth
from indus.foundation.scope import Scope

__all__ = ["CronContext", "script_rule"]


def script_rule(value: str) -> None:
    """The store's create/update check for ``script`` (P14-09): a file name under
    ``<home>/scripts/``, never a command line."""
    problem = check_script_value(value)
    if problem:
        raise InvalidJob(problem)


@dataclass
class CronContext:
    scope: Scope
    store: CronStore
    db: ExecutionsDB
    ledger: ExecutionLedger
    notepad: Notepad
    health: StoreHealth

    @classmethod
    def open(
        cls,
        scope: Scope,
        *,
        procs: ProcessProbe | None = None,
        notify: Callable[[HealthEvent], None] | None = None,
        script_check: Callable[[str], None] | None = None,
    ) -> CronContext:
        store = CronStore(scope, script_check=script_check or script_rule)
        store.ensure_dirs()
        db = ExecutionsDB(scope, store.paths.executions_db)
        ledger = ExecutionLedger(db, procs=procs)
        notepad = Notepad(db)
        store.add_remove_hook(notepad.clear)
        health = StoreHealth(store.paths.cron_dir, clock=scope.clock, notify=notify)
        return cls(scope, store, db, ledger, notepad, health)

    def close(self) -> None:
        self.db.close()
