"""The browser tools against a real headless Chromium and a local static server (P10-13, P10-14).

Marker ``browser``: skips cleanly when the ``browser`` extra (Playwright) or a Chromium is
missing. Pages come from ``tests/fixtures/web/`` over a server on 127.0.0.1 started here, so the
test needs no network; the profile allows private addresses for that reason. When the Chromium
build Playwright expects is not installed, ``$PLAYWRIGHT_BROWSERS_PATH/chromium`` is used if it
exists.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import threading
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
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

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(not playwright_available(), reason="the browser extra is not installed"),
]

WEB = Path(__file__).resolve().parents[2] / "fixtures" / "web"


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib name
        pass


@pytest.fixture
def server() -> Iterator[str]:
    handler = functools.partial(_Quiet, directory=str(WEB))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _executable() -> str:
    folder = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    candidate = Path(folder) / "chromium" if folder else None
    return str(candidate) if candidate is not None and candidate.exists() else ""


@pytest.fixture(autouse=True)
def short_tmp(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A short temp folder inside pytest's own area for Chromium and the Playwright driver (their
    socket paths must stay under the AF_UNIX length limit; nothing goes to the shared /tmp)."""
    folder = tmp_path_factory.mktemp("pw")
    monkeypatch.setenv("TMPDIR", str(folder))
    return folder


def _scope(tmp_path: Path) -> Scope:
    home = tmp_path / "browser-profile"
    home.mkdir(exist_ok=True)
    config = {
        "security": {"allow_private_urls": True},
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


class Browser:
    def __init__(self, scope: Scope, extras: dict[str, Any] | None = None) -> None:
        self.scope = scope
        self.tools = BrowserTools(SessionManager(janitor=False))
        self.registry = ToolRegistry()
        self.tools.register(self.registry)
        self.loop = asyncio.new_event_loop()
        self.extras = extras or {}

    def raw(self, name: str, **args: Any) -> Any:
        ctx = ToolContext(scope=self.scope, task_id="it", extras=self.extras)
        result = self.loop.run_until_complete(
            dispatch(ToolCall(name, args), ctx, registry=self.registry)
        )
        return result.content

    def __call__(self, name: str, **args: Any) -> dict[str, Any]:
        content = self.raw(name, **args)
        assert isinstance(content, str), content
        return json.loads(content)

    def close(self) -> None:
        self.loop.run_until_complete(self.tools.manager.shutdown())
        self.loop.close()


@pytest.fixture
def browser(tmp_path: Path) -> Iterator[Browser]:
    made = Browser(_scope(tmp_path), extras={"native_vision": True})
    try:
        yield made
    finally:
        made.close()


def _open(browser: Browser, url: str) -> dict[str, Any]:
    body = browser("browser_navigate", url=url)
    if not body["success"] and "indus tools install browser" in body["error"]:
        pytest.skip("no Chromium here: " + body["error"])
    return body


def _ref(outline: str, needle: str) -> str:
    for line in outline.splitlines():
        if needle in line and "[ref=" in line:
            return "@" + line.split("[ref=")[1].split("]")[0]
    raise AssertionError(f"no ref for {needle!r} in:\n{outline}")


def test_navigate_snapshot_click_type_back(browser: Browser, server: str) -> None:
    home = _open(browser, f"{server}/index.html")
    assert home["success"] is True, home
    assert home["title"] == "Fixture home"
    assert 'heading "Fixture home"' in home["snapshot"]
    link = _ref(home["snapshot"], "Open the form")

    clicked = browser("browser_click", ref=link)
    assert clicked["success"] is True, clicked
    form = browser("browser_snapshot")
    assert 'heading "Say hello"' in form["snapshot"]
    field = _ref(form["snapshot"], 'textbox "Your name"')
    typed = browser("browser_type", ref=field, text="Ada")
    assert typed["success"] is True, typed
    assert browser("browser_click", ref=_ref(form["snapshot"], 'button "Greet"'))["success"]
    full = browser("browser_snapshot", full=True)
    assert "Hello, Ada!" in full["snapshot"]
    assert 'value="Ada"' in full["snapshot"]  # the field was cleared before typing

    back = browser("browser_back")
    assert back["success"] is True and back["url"].endswith("/index.html")

    images = browser("browser_get_images")
    assert images["count"] == 1 and images["images"][0]["alt"] == "a tiny picture"
    console = browser("browser_console")
    assert {"type": "log", "text": "home loaded"} in console["console_messages"]
    evaluated = browser("browser_console", expression="document.title")
    assert evaluated["result"] == "Fixture home"


def test_dialog_vision_and_cdp(browser: Browser, server: str) -> None:
    form = _open(browser, f"{server}/form.html")
    assert form["success"] is True, form
    blocked = browser("browser_click", ref=_ref(form["snapshot"], 'button "Ask first"'))
    assert blocked["success"] is False
    assert blocked["pending_dialogs"][0]["type"] == "prompt"
    seen = browser("browser_snapshot")
    assert seen["pending_dialogs"][0]["message"] == "What is the word?"
    answered = browser("browser_dialog", action="accept", prompt_text="banana")
    assert answered["success"] is True, answered
    after = browser("browser_snapshot", full=True)
    assert "The word is banana" in after["snapshot"]

    shot = browser.raw("browser_vision", question="What does the page say?", annotate=True)
    assert isinstance(shot, Mapping) and shot["multimodal"] is True
    assert shot["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    path = shot["text_summary"].split("saved at ")[1].split(";")[0]
    assert Path(path).is_file() and Path(path).stat().st_size > 1000

    version = browser("browser_cdp", method="Browser.getVersion")
    assert version["success"] is True and "Chrome" in version["result"]["product"]
    title = browser(
        "browser_cdp",
        method="Runtime.evaluate",
        params={"expression": "document.title", "returnByValue": True},
    )
    assert title["result"]["result"]["value"] == "Fixture form"


def test_missing_executable_gives_the_install_hint(tmp_path: Path) -> None:
    config = {
        "security": {"allow_private_urls": True},
        "browser": {"executable_path": str(tmp_path / "no-chrome")},
    }
    browser = Browser(_scope(tmp_path).with_config(DictConfig(config)))
    try:
        body = browser("browser_navigate", url="http://127.0.0.1:9/")
    finally:
        browser.close()
    assert body["success"] is False
    assert "indus tools install browser" in body["error"]
