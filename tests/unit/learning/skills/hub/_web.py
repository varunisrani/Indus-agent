"""Fake web for the hub tests: routes served through httpx.MockTransport behind the real
SSRF-safe transport, a local HTTP server, and scope helpers. Nothing reaches the internet."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

from indus.foundation.scope import Scope
from indus.learning.skills.hub.sources import HubHttp, SourceSet, build_sources
from indus.safety.url_policy import PROXY_VARS
from tests.unit.learning.skills.helpers import FakeConfig

FIXTURES = Path(__file__).resolve().parents[4] / "fixtures" / "hub"
REPO = FIXTURES / "repo"
PUBLIC_IP = "93.184.216.34"
API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
SKILLS_SH = "https://skills.sh"
TREE_SHA = "5f1e0c7"

type Route = bytes | Callable[[httpx.Request], httpx.Response]


def clean_scope(scope: Scope, config: Mapping[str, Any] | None = None) -> Scope:
    """``scope`` without proxy or GitHub token variables, with ``config`` as its settings."""
    names = [*PROXY_VARS, "GITHUB_TOKEN", "GH_TOKEN", "NO_PROXY", "no_proxy"]
    stripped = scope.with_env(scope.env.with_overlay(dict.fromkeys(names)))
    return stripped.with_config(FakeConfig(dict(config or {})))


@dataclass
class FakeWeb:
    """``url -> body`` (or a handler); unknown URLs answer 404. ``hits`` lists every URL asked."""

    routes: dict[str, Route] = field(default_factory=dict[str, Route])
    hits: list[str] = field(default_factory=list[str])

    def add(self, url: str, body: Route | str | Mapping[str, Any] | list[Any]) -> None:
        if isinstance(body, str):
            body = body.encode("utf-8")
        elif isinstance(body, Mapping | list):
            body = json.dumps(body).encode("utf-8")
        self.routes[url] = body

    def add_file(self, url: str, path: Path) -> None:
        self.routes[url] = path.read_bytes()

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.headers.get("host") or request.url.host
        url = f"{request.url.scheme}://{host}{request.url.path}"
        self.hits.append(url + (f"?{request.url.query.decode()}" if request.url.query else ""))
        route = self.routes.get(url)
        if route is None:
            return httpx.Response(404, content=b"not found")
        if callable(route):
            return route(request)
        return httpx.Response(200, content=route)

    def http(self, scope: Scope) -> HubHttp:
        return HubHttp(
            scope,
            resolver=lambda _host, _port: [PUBLIC_IP],
            inner_factory=lambda _proxy: httpx.MockTransport(self.handler),
        )

    def sources(self, scope: Scope, *, official: Path | None = None) -> SourceSet:
        catalogue = official or Path("/nonexistent")
        return build_sources(scope, http=self.http(scope), official=catalogue)


def github_repo(web: FakeWeb, repo: str = "acme/agent-skills", root: Path = REPO) -> None:
    """Serve ``root`` as the GitHub repo ``repo`` (API answers from the committed fixtures)."""
    web.add(f"{API}/repos/{repo}", (FIXTURES / "github_repo.json").read_bytes())
    web.add(f"{API}/repos/{repo}/git/trees/main", (FIXTURES / "github_tree.json").read_bytes())
    for path in root.rglob("*"):
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            web.add_file(f"{RAW}/{repo}/{TREE_SHA}/{rel}", path)


def well_known_site(web: FakeWeb, site: str = "https://docs.example.com") -> str:
    base = f"{site}/.well-known/skills"
    for path in (FIXTURES / "well_known").rglob("*"):
        if path.is_file():
            web.add_file(f"{base}/{path.relative_to(FIXTURES / 'well_known').as_posix()}", path)
    return base


# -- a real local server ------------------------------------------------------------------------


class _Table(BaseHTTPRequestHandler):
    """Answers GETs from the server's ``table`` (read per request)."""

    def do_GET(self) -> None:  # noqa: N802 (http.server's name)
        table: dict[str, bytes] = getattr(self.server, "table", {})
        body = table.get(self.path.split("?", 1)[0])
        payload = b"not found" if body is None else body
        self.send_response(404 if body is None else 200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return


@contextmanager
def local_server(files: dict[str, bytes]) -> Iterator[str]:
    """Serve ``{path: bytes}`` on 127.0.0.1; yields the base URL (``http://127.0.0.1:<port>``).

    The table is read on every request, so a test may change it while serving."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Table)
    httpd.table = files  # type: ignore[attr-defined]
    worker = threading.Thread(target=httpd.serve_forever, args=(0.02,), daemon=True)
    worker.start()
    host, port = httpd.server_address[:2]
    try:
        yield f"http://{host!s}:{port}"
    finally:
        httpd.shutdown()
        worker.join(timeout=5)
        httpd.server_close()


def local_scope(scope: Scope) -> Scope:
    """A scope whose URL policy lets the hub reach the local server."""
    return clean_scope(scope, {"security": {"allow_private_urls": True}})


def github_table(prefix: str = "", repo: str = "acme/agent-skills") -> dict[str, bytes]:
    """The fixture repo as local-server paths: ``<prefix>/api/...`` and ``<prefix>/raw/...``."""
    table: dict[str, bytes] = {
        f"{prefix}/api/repos/{repo}": (FIXTURES / "github_repo.json").read_bytes(),
        f"{prefix}/api/repos/{repo}/git/trees/main": (FIXTURES / "github_tree.json").read_bytes(),
    }
    for path in REPO.rglob("*"):
        if path.is_file():
            rel = path.relative_to(REPO).as_posix()
            table[f"{prefix}/raw/{repo}/{TREE_SHA}/{rel}"] = path.read_bytes()
    return table
