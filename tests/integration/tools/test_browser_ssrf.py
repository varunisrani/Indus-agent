"""The browser's request guard against a real headless Chromium (SSRF, P10-12 security fix).

Two local servers: the *site* (reached as ``localhost``, which the test's resolver answers with
a public address, so the P05 check lets it through with ``security.allow_private_urls`` off) and
the *private* target on the literal ``127.0.0.1``, which the check refuses. The site's page embeds
the private address directly, through redirects (image, frame, ``fetch``), opens a WebSocket to
it, and a navigation redirects into it. The private server must never see a request.

Marker ``browser``: skips cleanly without the ``browser`` extra or a Chromium.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope, make_scope
from indus.tools.builtin.browser import BrowserTools
from indus.tools.builtin.browser.driver import playwright_available
from indus.tools.builtin.browser.session import SessionManager
from indus.tools.calls import ToolCall, ToolContext
from indus.tools.dispatch import dispatch
from indus.tools.registry import ToolRegistry
from tests.fakes.agent import DictConfig
from tests.fakes.clock import FakeClock
from tests.fakes.resolver import FakeResolver

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(not playwright_available(), reason="the browser extra is not installed"),
]


class _Server:
    """A tiny HTTP server on 127.0.0.1 that records every path and answers from ``routes``."""

    def __init__(self, routes: dict[str, tuple[int, dict[str, str], str]]) -> None:
        self.hits: list[str] = []
        self.routes = routes
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib name
                pass

            def _answer(self) -> None:
                outer.hits.append(f"{self.command} {self.path}")
                status, headers, body = outer.routes.get(
                    self.path.split("?")[0], (200, {"Content-Type": "text/plain"}, "ok")
                )
                data = body.encode()
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)

            do_GET = do_HEAD = do_POST = _answer  # noqa: N815 - stdlib handler names

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> _Server:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def _moved(to: str) -> tuple[int, dict[str, str], str]:
    return (302, {"Location": to}, "")


@pytest.fixture
def servers() -> Iterator[tuple[_Server, _Server]]:
    with _Server({}) as private:
        inside = f"http://127.0.0.1:{private.port}"
        page = f"""<!doctype html><html><head><title>Guarded page</title></head><body>
<h1>Guarded page</h1>
<img src="{inside}/embedded.png" alt="direct">
<img src="/redirect-image" alt="redirected">
<img src="/public.png" alt="public">
<iframe src="/redirect-frame"></iframe>
<script>
  fetch("/redirect-fetch").catch(() => {{}});
  fetch("{inside}/fetched").catch(() => {{}});
  try {{ new WebSocket("ws://127.0.0.1:{private.port}/socket"); }} catch (e) {{}}
  window.addEventListener("load", () => {{ document.title = "Guarded page loaded"; }});
</script>
</body></html>"""
        site_routes = {
            "/page": (200, {"Content-Type": "text/html"}, page),
            "/public.png": (200, {"Content-Type": "image/svg+xml"}, "<svg/>"),
            "/redirect-image": _moved(f"{inside}/via-image"),
            "/redirect-frame": _moved(f"{inside}/via-frame"),
            "/redirect-fetch": _moved(f"{inside}/via-fetch"),
            "/hop": _moved("/hop-two"),
            "/hop-two": _moved(f"{inside}/via-navigation"),
        }
        with _Server(site_routes) as site:
            yield site, private


def _executable() -> str:
    folder = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    candidate = Path(folder) / "chromium" if folder else None
    return str(candidate) if candidate is not None and candidate.exists() else ""


@pytest.fixture(autouse=True)
def short_tmp(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Chromium's socket paths must stay short; everything stays inside pytest's own area."""
    folder = tmp_path_factory.mktemp("pw")
    monkeypatch.setenv("TMPDIR", str(folder))
    return folder


def _scope(tmp_path: Path) -> Scope:
    home = tmp_path / "browser-profile"
    home.mkdir(exist_ok=True)
    config = {
        "security": {"allow_private_urls": False},
        "browser": {"executable_path": _executable()},
    }
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "TMPDIR": os.environ.get("TMPDIR", str(tmp_path)),
    }
    clock = FakeClock(datetime(2026, 3, 1, tzinfo=UTC))
    return make_scope(home, env=env, clock=clock).with_config(DictConfig(config))


def _call(
    tools: BrowserTools, scope: Scope, loop: asyncio.AbstractEventLoop, name: str, **args: Any
) -> dict[str, Any]:
    registry = ToolRegistry()
    tools.register(registry)
    ctx = ToolContext(scope=scope, task_id="ssrf", extras={})
    result = loop.run_until_complete(dispatch(ToolCall(name, args), ctx, registry=registry))
    assert isinstance(result.content, str), result.content
    return json.loads(result.content)


def test_page_requests_and_redirect_hops_never_reach_a_refused_address(
    tmp_path: Path, servers: tuple[_Server, _Server]
) -> None:
    site, private = servers
    # The check sees ``localhost`` as a public name (the test's resolver); Chromium and Playwright
    # still dial 127.0.0.1. The literal 127.0.0.1 is refused: allow_private_urls is off.
    resolver = FakeResolver({"localhost": ["93.184.216.34"]})
    tools = BrowserTools(SessionManager(janitor=False), resolver=resolver)
    scope = _scope(tmp_path)
    loop = asyncio.new_event_loop()
    base = f"http://localhost:{site.port}"
    try:
        opened = _call(tools, scope, loop, "browser_navigate", url=f"{base}/page")
        if not opened["success"] and "indus tools install browser" in opened["error"]:
            pytest.skip("no Chromium here: " + opened["error"])
        assert opened["success"] is True, opened
        # The page waits a second for late sub-resources, the fetches and the socket.
        settle = "new Promise((done) => setTimeout(done, 1000)).then(() => document.title)"
        page = _call(tools, scope, loop, "browser_console", expression=settle)
        assert page["result"] == "Guarded page loaded"

        chained = _call(tools, scope, loop, "browser_navigate", url=f"{base}/hop")
        assert chained["success"] is False

        direct = _call(
            tools, scope, loop, "browser_navigate", url=f"http://127.0.0.1:{private.port}/x"
        )
        assert direct["success"] is False and "Blocked" in direct["error"]
    finally:
        loop.run_until_complete(tools.manager.shutdown())
        loop.close()

    assert private.hits == []
    assert "GET /page" in site.hits
    assert "GET /public.png" in site.hits  # allowed requests still load
    assert "GET /redirect-image" in site.hits and "GET /redirect-frame" in site.hits
    assert "GET /hop" in site.hits and "HEAD /hop-two" in site.hits
