"""The cron store as a curator ``SkillReferenceSource`` (P14-03, R07.8).

``indus.cron`` and ``indus.learning`` are sibling layers and may not import each other, so the
adapter lives here. Hosts call ``register_cron_references(store)`` at start (the in-process host
does it for its profile; one-off curator runs use ``cron_references(scope)``); the curator then
never archives a skill any job names (paused jobs included) and asks the store to follow renames.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Collection, Generator, Mapping

from indus.cron.store import CronStore
from indus.foundation.scope import Scope
from indus.learning.curator import ReferenceRewrite, register_reference_source

__all__ = ["CronSkillReferences", "cron_references", "register_cron_references"]


class CronSkillReferences:
    def __init__(self, store: CronStore) -> None:
        self.store = store

    @property
    def name(self) -> str:
        return "cron"

    def referenced_skills(self) -> set[str]:
        return self.store.referenced_skills()

    def rewrite(self, renames: Mapping[str, str], dropped: Collection[str]) -> ReferenceRewrite:
        return ReferenceRewrite(self.name, self.store.rewrite_skills(renames, dropped))


def register_cron_references(store: CronStore) -> Callable[[], None]:
    """Register ``store`` with the curator; returns the undo."""
    return register_reference_source(CronSkillReferences(store))


@contextlib.contextmanager
def cron_references(scope: Scope) -> Generator[None]:
    """The profile's cron jobs protect their skills for the length of the block."""
    undo = register_cron_references(CronStore(scope))
    try:
        yield
    finally:
        undo()
