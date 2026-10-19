"""A process table for ledger reaping tests (P14-04).

``FakePids(me=(100, "boot:1"))`` is this process; ``spawn(pid, fingerprint)`` adds another one,
``kill(pid)`` ends it, ``reuse(pid, fingerprint)`` hands the PID to a new process.
``unreadable(pid)`` hides a fingerprint (non-Linux, permissions).
"""

from __future__ import annotations


class FakePids:
    def __init__(self, me: tuple[int, str | None] = (100, "boot:100")) -> None:
        self.me = me
        self.table: dict[int, str | None] = {me[0]: me[1]}
        self.hidden: set[int] = set()

    def current(self) -> tuple[int, str | None]:
        return self.me

    def exists(self, pid: int) -> bool:
        return pid in self.table

    def fingerprint(self, pid: int) -> str | None:
        if pid in self.hidden:
            return None
        return self.table.get(pid)

    def spawn(self, pid: int, fingerprint: str | None = None) -> FakePids:
        self.table[pid] = fingerprint if fingerprint is not None else f"boot:{pid}"
        return self

    def kill(self, pid: int) -> None:
        self.table.pop(pid, None)

    def reuse(self, pid: int, fingerprint: str) -> None:
        self.table[pid] = fingerprint

    def unreadable(self, pid: int) -> None:
        self.hidden.add(pid)

    def as_process(self, pid: int) -> FakePids:
        """The same table seen from process ``pid`` (it must exist)."""
        other = FakePids((pid, self.table[pid]))
        other.table = self.table
        other.hidden = self.hidden
        return other
