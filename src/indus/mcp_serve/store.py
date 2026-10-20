"""The MCP server's one read-only view of the session store (R03.16, R03.17).

``StoreReader(scope)`` opens the profile's store through ``state.browse.open_readonly`` the first
time something reads (a ``mode=ro`` URI with the path percent-encoded, no schema setup, a missing
file reported and never created) and keeps that single connection for the server's life: the read
tools and the event bridge share it, so the process holds at most one store descriptor however
many tool calls run at once. Calls are serialised by a lock (tools run on worker threads). A
connection that fails is closed and the next read opens a fresh one.

``read(fn)`` runs ``fn(conn)`` and returns its result; ``StoreUnavailable`` (missing, never set up,
newer schema) and ``sqlite3.Error`` reach the caller, which turns them into an ``error`` answer.
``data_version()`` is the store's change counter on this connection: it moves only when another
connection commits, WAL commits and same-second commits included.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from collections.abc import Callable

from indus.foundation.errors import StoreUnavailable
from indus.foundation.scope import Scope
from indus.state import browse
from indus.state.browse import ReadonlyStore

__all__ = ["StoreReader", "describe_problem"]


def describe_problem(exc: BaseException) -> str:
    """A short, path-free sentence for an ``error`` answer."""
    if isinstance(exc, StoreUnavailable):
        kind = exc.kind
        if kind == "missing":
            return "no session store yet: the gateway has not saved a conversation in this profile"
        if kind == "incompatible":
            return "the session store was written by a newer Indus Agent; update to read it"
        if kind == "uninitialised":
            return "the session store was never set up"
        return "the session store cannot be read right now"
    return f"the session store could not be read ({type(exc).__name__})"


class StoreReader:
    """See the module notes."""

    def __init__(self, scope: Scope) -> None:
        self.scope = scope
        self._lock = threading.Lock()
        self._view: ReadonlyStore | None = None
        self._closed = False

    def _open(self) -> ReadonlyStore:
        view = self._view
        if view is None:
            if self._closed:
                raise StoreUnavailable("the store reader is closed", kind="closed")
            view = browse.open_readonly(self.scope)
            self._view = view
        return view

    def _drop(self) -> None:
        view, self._view = self._view, None
        if view is not None:
            with contextlib.suppress(Exception):
                view.close()

    def read[T](self, fn: Callable[[sqlite3.Connection], T]) -> T:
        with self._lock:
            view = self._open()
            try:
                return fn(view.conn)
            except sqlite3.Error:
                self._drop()
                raise

    def data_version(self) -> int:
        return self.read(lambda conn: int(conn.execute("PRAGMA data_version").fetchone()[0]))

    @property
    def is_open(self) -> bool:
        return self._view is not None

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._drop()
