"""A scripted DNS resolver for the URL policy tests (no network).

``FakeResolver({"example.com": ["93.184.216.34"]})`` answers from the table; a host mapped to a
list of lists answers with the next list on each lookup (the last one repeats), which is how the
rebinding tests make a name public for the check and private at connect time. A missing host, or
an entry that is an ``OSError``, fails the lookup.
"""

from __future__ import annotations

import socket
from collections.abc import Mapping, Sequence


class FakeResolver:
    def __init__(
        self, table: Mapping[str, Sequence[str] | Sequence[Sequence[str]] | OSError] | None = None
    ) -> None:
        self._table: dict[str, list[list[str]] | OSError] = {}
        self.lookups: list[str] = []
        for host, answer in (table or {}).items():
            self.set(host, answer)

    def set(self, host: str, answer: Sequence[str] | Sequence[Sequence[str]] | OSError) -> None:
        if isinstance(answer, OSError):
            self._table[host] = answer
        elif answer and not isinstance(answer[0], str):
            self._table[host] = [list(a) for a in answer]  # type: ignore[arg-type]
        else:
            self._table[host] = [list(answer)]  # type: ignore[arg-type]

    def __call__(self, host: str, port: int | None) -> list[str]:
        self.lookups.append(host)
        entry = self._table.get(host)
        if entry is None:
            raise socket.gaierror(socket.EAI_NONAME, f"no such host {host}")
        if isinstance(entry, OSError):
            raise entry
        answer = entry[0]
        if len(entry) > 1:
            entry.pop(0)
        return list(answer)
