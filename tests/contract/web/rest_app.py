"""A dashboard app for the REST route tests (P18-11..16).

Builds the real app (``create_app`` in headless serve mode, so no SPA bundle is needed) for a
profile scope — the factory installs the per-request profile resolver (P18-10) itself — puts test
seams into ``DashboardState.extras`` and returns a ``TestClient`` that sends the loopback session
token on every call (``anonymous()`` gives one that sends nothing).

``route_table`` / ``unscoped_writes`` read the mounted routes, including those of included routers,
so each route module's ``ROUTE_LIST`` and the write-scope rule (R09.20) are pinned against the app
the dashboard really serves.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from starlette.routing import BaseRoute
from starlette.testclient import TestClient

from indus.foundation.scope import Scope
from indus.web.app import AppOptions, create_app
from indus.web.scope import read_scope, write_scope
from indus.web.state import DashboardState

SESSION_HEADER = "X-Indus-Session-Token"
BASE_URL = "http://127.0.0.1:9119"
_TICK = threading.Event()  # never set: a wall-clock pause between polls


def pause(seconds: float) -> None:
    """Wait ``seconds`` of wall-clock time (background actions run on real time)."""
    _TICK.wait(seconds)


@dataclass
class RestApp:
    app: FastAPI
    client: TestClient
    state: DashboardState
    _extra_clients: list[TestClient] = field(default_factory=list[TestClient])

    def anonymous(self) -> TestClient:
        """A client that carries no credential at all."""
        client = TestClient(self.app, base_url=BASE_URL, follow_redirects=False)
        self._extra_clients.append(client)
        return client

    def close(self) -> None:
        for client in [self.client, *self._extra_clients]:
            client.close()


def build_rest_app(
    scope: Scope,
    *,
    extras: Mapping[str, Any] | None = None,
    options: AppOptions | None = None,
) -> RestApp:
    app = create_app(scope, "serve", options=options or AppOptions(config={}))
    state: DashboardState = app.state.indus
    state.extras.update(extras or {})
    client = TestClient(app, base_url=BASE_URL, follow_redirects=False)
    client.headers[SESSION_HEADER] = state.auth.token.value
    return RestApp(app, client, state)


def _depends_on(dependant: Any, target: object, seen: set[int]) -> bool:
    if id(dependant) in seen:
        return False
    seen.add(id(dependant))
    if getattr(dependant, "call", None) is target:
        return True
    return any(_depends_on(c, target, seen) for c in getattr(dependant, "dependencies", ()) or ())


def api_routes(routes: Iterable[BaseRoute]) -> Iterator[APIRoute]:
    """Every ``APIRoute``, including those of included routers (FastAPI keeps those wrapped, so
    ``app.routes`` alone does not list them)."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
            continue
        inner: object = getattr(route, "original_router", None)
        if isinstance(inner, APIRouter):
            yield from api_routes(inner.routes)


def _under(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == p or path.startswith(p.rstrip("/") + "/") for p in prefixes)


def route_table(app: FastAPI, prefixes: tuple[str, ...]) -> set[tuple[str, str, bool]]:
    """``(method, path, scoped)`` of the app's routes under ``prefixes`` (whole segments);
    ``scoped`` = the route resolves a profile scope (``read_scope`` or ``write_scope``)."""
    found: set[tuple[str, str, bool]] = set()
    for route in api_routes(app.routes):
        if not _under(route.path, prefixes):
            continue
        scoped = _depends_on(route.dependant, read_scope, set()) or _depends_on(
            route.dependant, write_scope, set()
        )
        for method in route.methods or ():
            if method != "HEAD":
                found.add((method, route.path, scoped))
    return found


def unscoped_writes(app: FastAPI, prefixes: tuple[str, ...]) -> list[str]:
    """Write routes under ``prefixes`` that do not depend on ``write_scope`` (R09.20)."""
    missing: list[str] = []
    for route in api_routes(app.routes):
        writes = sorted({"POST", "PUT", "PATCH", "DELETE"} & set(route.methods or ()))
        if not _under(route.path, prefixes) or not writes:
            continue
        if not _depends_on(route.dependant, write_scope, set()):
            missing.extend(f"{method} {route.path}" for method in writes)
    return missing
