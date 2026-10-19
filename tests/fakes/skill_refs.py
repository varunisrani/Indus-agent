"""An in-memory ``SkillReferenceSource`` standing in for the cron store until P14.

``FakeReferenceSource(["a", "b"])`` says it references those skills; ``rewrite`` applies renames
to its own list and remembers each call in ``rewrites``. ``fail=True`` makes ``referenced_skills``
raise, as a broken store would.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping

from indus.learning.curator import ReferenceRewrite


class FakeReferenceSource:
    def __init__(self, names: Iterable[str] = (), *, fail: bool = False, label: str = "fake-cron"):
        self.names: list[str] = list(names)
        self.fail = fail
        self.label = label
        self.rewrites: list[tuple[dict[str, str], list[str]]] = []

    @property
    def name(self) -> str:
        return self.label

    def referenced_skills(self) -> set[str]:
        if self.fail:
            raise OSError("the job store cannot be read")
        return set(self.names)

    def rewrite(self, renames: Mapping[str, str], dropped: Collection[str]) -> ReferenceRewrite:
        self.rewrites.append((dict(renames), list(dropped)))
        changes: list[str] = []
        updated: list[str] = []
        for skill in self.names:
            if skill in renames:
                changes.append(f"{skill} → {renames[skill]}")
                updated.append(renames[skill])
            elif skill in dropped:
                changes.append(f"{skill} dropped")
            else:
                updated.append(skill)
        self.names = list(dict.fromkeys(updated))
        return ReferenceRewrite(self.label, tuple(changes))
