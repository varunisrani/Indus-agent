"""What the browser tools need from a browser, and the Playwright implementation of it.

The session manager and the tools talk only to the small protocols below (``Driver`` starts or
attaches a browser and returns a ``BrowserHandle``; the handle's ``PageDriver`` does the page work
and a ``PageListener`` hears console lines, page errors, dialogs, frame changes and a lost
connection). Unit tests plug in ``tests/fakes/browser.py``; the real implementation is
``PlaywrightDriver``, which imports Playwright only when a browser is first needed, so the
package loads without the ``browser`` extra.

Element refs live in a page attribute chosen per session; the tools hand this module a CSS
selector for it (``ref_selector``).

``BrowserHandle.guard(gate)`` installs the session's request guard (``guard.RequestGuard``: every
request, redirect hop and WebSocket of the page through the P05 URL check) before the handle is
used: on the whole context of a browser we launched (service workers blocked, so none of their
requests slips past the route), on our own tab only when attached to the user's browser. A launch
whose environment names a proxy (``browser.env_passthrough``) passes it to Playwright too, since
the guard's own requests go out through Playwright, not Chromium.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast
from urllib.parse import unquote, urlsplit

from indus.tools.builtin.browser.guard import RequestGate, RequestGuard
from indus.tools.builtin.browser.snapshot import IMAGES_JS, LABELS_JS, OUTLINE_JS, UNLABEL_JS

if TYPE_CHECKING:
    from playwright.async_api import (  # pyright: ignore[reportMissingImports] — optional extra
        Browser,
        BrowserContext,
        CDPSession,
        ConsoleMessage,
        Dialog,
        Frame,
        Page,
        Playwright,
    )

__all__ = [
    "BrowserHandle",
    "BrowserMissing",
    "DialogLike",
    "Driver",
    "FrameInfo",
    "LaunchSpec",
    "PageDriver",
    "PageListener",
    "PlaywrightDriver",
    "PlaywrightMissing",
    "RefNotFound",
    "launch_proxy",
    "playwright_available",
    "ref_selector",
]

log = logging.getLogger(__name__)

INSTALL_HINT = (
    "Chromium for the browser tools is missing. Install it with: indus tools install browser"
)
EXTRA_HINT = (
    "The browser tools need the 'browser' extra (Playwright). Install it with: "
    "indus tools install browser"
)


class BrowserMissing(RuntimeError):
    """No Chromium to launch."""

    def __init__(self, detail: str = "") -> None:
        super().__init__(INSTALL_HINT + (f" ({detail})" if detail else ""))


class PlaywrightMissing(RuntimeError):
    def __init__(self) -> None:
        super().__init__(EXTRA_HINT)


class RefNotFound(LookupError):
    """The ref names no element on the page (it changed since the outline was taken)."""


def ref_selector(attr: str, ref: str) -> str:
    return f'[{attr}="{ref}"]'


def playwright_available() -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec("playwright") is not None
    except (ImportError, ValueError):
        return False


# -- protocols ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FrameInfo:
    frame_id: str
    url: str
    name: str = ""
    parent_id: str | None = None


class DialogLike(Protocol):
    @property
    def kind(self) -> str: ...  # alert | confirm | prompt | beforeunload

    @property
    def message(self) -> str: ...

    @property
    def default_value(self) -> str: ...

    async def accept(self, prompt_text: str | None = None) -> None: ...

    async def dismiss(self) -> None: ...


class PageListener(Protocol):
    def on_console(self, kind: str, text: str) -> None: ...

    def on_page_error(self, message: str) -> None: ...

    def on_dialog(self, dialog: DialogLike) -> None: ...

    def on_frames(self, frames: list[FrameInfo]) -> None: ...

    def on_disconnected(self) -> None: ...


class PageDriver(Protocol):
    def url(self) -> str: ...

    async def title(self) -> str: ...

    async def goto(self, url: str, timeout_s: float) -> str: ...

    async def go_back(self, timeout_s: float) -> str: ...

    async def outline(self, full: bool, attr: str) -> list[dict[str, Any]]: ...

    async def click(self, selector: str, timeout_s: float) -> None: ...

    async def fill(self, selector: str, text: str, timeout_s: float) -> None: ...

    async def scroll(self, dy: int) -> None: ...

    async def press(self, key: str, timeout_s: float) -> None: ...

    async def images(self) -> list[dict[str, Any]]: ...

    async def evaluate(self, expression: str) -> Any: ...

    async def screenshot(self, path: Path, *, label_attr: str | None = None) -> list[str]: ...

    async def cdp(
        self, method: str, params: Mapping[str, Any], *, browser_level: bool = False
    ) -> dict[str, Any]: ...


class BrowserHandle(Protocol):
    @property
    def page(self) -> PageDriver: ...

    def listen(self, listener: PageListener) -> None: ...

    async def guard(self, gate: RequestGate) -> None: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class LaunchSpec:
    """How to start one task's Chromium."""

    profile_dir: Path
    env: Mapping[str, str]
    args: tuple[str, ...] = ()
    sandbox: bool = True
    executable: str | None = None
    headless: bool = True
    timeout_s: float = 60.0
    command_timeout_s: float = 30.0


class Driver(Protocol):
    async def launch(self, spec: LaunchSpec) -> BrowserHandle: ...

    async def attach(
        self, cdp_url: str, *, timeout_s: float, command_timeout_s: float
    ) -> BrowserHandle: ...

    async def stop(self) -> None: ...


# -- Playwright -----------------------------------------------------------------------------------


@dataclass
class _PlaywrightDialog:
    raw: Dialog

    @property
    def kind(self) -> str:
        return self.raw.type

    @property
    def message(self) -> str:
        return self.raw.message

    @property
    def default_value(self) -> str:
        return self.raw.default_value

    async def accept(self, prompt_text: str | None = None) -> None:
        if prompt_text is None:
            await self.raw.accept()
        else:
            await self.raw.accept(prompt_text)

    async def dismiss(self) -> None:
        await self.raw.dismiss()


@dataclass
class _PlaywrightPage:
    page: Page
    context: BrowserContext
    browser: Browser | None
    _page_cdp: CDPSession | None = None
    _browser_cdp: CDPSession | None = None

    def url(self) -> str:
        return self.page.url

    async def title(self) -> str:
        return await self.page.title()

    async def goto(self, url: str, timeout_s: float) -> str:
        await self.page.goto(url, timeout=timeout_s * 1000, wait_until="load")
        return self.page.url

    async def go_back(self, timeout_s: float) -> str:
        await self.page.go_back(timeout=timeout_s * 1000, wait_until="load")
        return self.page.url

    async def outline(self, full: bool, attr: str) -> list[dict[str, Any]]:
        rows = await self.page.evaluate(OUTLINE_JS, {"full": full, "attr": attr})
        return cast(list[dict[str, Any]], rows if isinstance(rows, list) else [])

    async def _one(self, selector: str):  # noqa: ANN202 - Playwright Locator
        found = self.page.locator(selector)
        if await found.count() == 0:
            raise RefNotFound(selector)
        return found.first

    async def click(self, selector: str, timeout_s: float) -> None:
        await (await self._one(selector)).click(timeout=timeout_s * 1000)

    async def fill(self, selector: str, text: str, timeout_s: float) -> None:
        await (await self._one(selector)).fill(text, timeout=timeout_s * 1000)

    async def scroll(self, dy: int) -> None:
        await self.page.evaluate("(dy) => window.scrollBy(0, dy)", dy)

    async def press(self, key: str, timeout_s: float) -> None:
        await self.page.keyboard.press(key)

    async def images(self) -> list[dict[str, Any]]:
        found = await self.page.evaluate(IMAGES_JS)
        return cast(list[dict[str, Any]], found if isinstance(found, list) else [])

    async def evaluate(self, expression: str) -> Any:
        return await self.page.evaluate(expression)

    async def screenshot(self, path: Path, *, label_attr: str | None = None) -> list[str]:
        marks: list[str] = []
        if label_attr:
            marks = cast(list[str], await self.page.evaluate(LABELS_JS, label_attr) or [])
        try:
            await self.page.screenshot(path=str(path), full_page=False)
        finally:
            if label_attr:
                await self.page.evaluate(UNLABEL_JS)
        return marks

    async def cdp(
        self, method: str, params: Mapping[str, Any], *, browser_level: bool = False
    ) -> dict[str, Any]:
        if browser_level and self.browser is not None:
            if self._browser_cdp is None:
                self._browser_cdp = await self.browser.new_browser_cdp_session()
            session = self._browser_cdp
        else:
            if self._page_cdp is None:
                self._page_cdp = await self.context.new_cdp_session(self.page)
            session = self._page_cdp
        assert session is not None
        answer = await session.send(method, dict(params))
        return dict(answer) if isinstance(answer, Mapping) else {"value": answer}


@dataclass
class _PlaywrightHandle:
    context: BrowserContext
    browser: Browser | None
    raw_page: Page
    owns_browser: bool
    _page: _PlaywrightPage = field(init=False)
    _frame_ids: dict[int, str] = field(default_factory=dict[int, str])
    _numbers: itertools.count[int] = field(default_factory=lambda: itertools.count(1))
    _listeners: list[PageListener] = field(default_factory=list[PageListener])
    _guard: RequestGuard | None = None

    def __post_init__(self) -> None:
        self._page = _PlaywrightPage(self.raw_page, self.context, self.browser)

    @property
    def page(self) -> PageDriver:
        return self._page

    def _frame_id(self, frame: Frame) -> str:
        key = id(frame)
        if key not in self._frame_ids:
            self._frame_ids[key] = f"f{next(self._numbers)}"
        return self._frame_ids[key]

    def _frames(self) -> list[FrameInfo]:
        out: list[FrameInfo] = []
        for frame in self.raw_page.frames:
            parent = frame.parent_frame
            out.append(
                FrameInfo(
                    self._frame_id(frame),
                    frame.url,
                    frame.name,
                    self._frame_id(parent) if parent is not None else None,
                )
            )
        return out

    def listen(self, listener: PageListener) -> None:
        self._listeners.append(listener)
        page = self.raw_page

        def console(message: ConsoleMessage) -> None:
            listener.on_console(message.type, message.text)

        def page_error(error: Any) -> None:
            listener.on_page_error(str(error))

        def dialog(raw: Dialog) -> None:
            listener.on_dialog(_PlaywrightDialog(raw))

        def frames(_frame: Frame) -> None:
            listener.on_frames(self._frames())

        def gone(*_args: Any) -> None:
            listener.on_disconnected()

        page.on("console", console)
        page.on("pageerror", page_error)
        page.on("dialog", dialog)
        page.on("frameattached", frames)
        page.on("framedetached", frames)
        page.on("framenavigated", frames)
        if self.browser is not None and not self.owns_browser:
            self.browser.on("disconnected", gone)
        else:
            self.context.on("close", gone)

    async def guard(self, gate: RequestGate) -> None:
        guard = self._guard = RequestGuard(gate)
        target = self.context if self.owns_browser else self.raw_page
        await target.route(_every, guard.route)
        await target.route_web_socket(_every, guard.socket)

    async def close(self) -> None:
        if self.owns_browser:
            await self.context.close()
            return
        try:
            await self.raw_page.close()
        finally:
            if self.browser is not None:
                await self.browser.close()  # attached: disconnects, the user's browser stays


def _every(_url: str) -> bool:
    return True


PROXY_NAMES = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")


def launch_proxy(env: Mapping[str, str]) -> dict[str, str] | None:
    """Playwright's ``proxy`` option from a launch environment that names a proxy, else None."""
    raw = next((env[n].strip() for n in PROXY_NAMES if env.get(n, "").strip()), "")
    if not raw:
        return None
    parts = urlsplit(raw if "://" in raw else f"http://{raw}")
    host = parts.hostname or ""
    if not host:
        return None
    server = f"{parts.scheme}://{host}" + (f":{parts.port}" if parts.port else "")
    proxy = {"server": server}
    if parts.username:
        proxy["username"] = unquote(parts.username)
        proxy["password"] = unquote(parts.password or "")
    bypass = env.get("NO_PROXY", "") or env.get("no_proxy", "")
    if bypass.strip():
        proxy["bypass"] = bypass.strip()
    return proxy


def _missing_executable(exc: BaseException) -> bool:
    text = str(exc)
    return "Executable doesn't exist" in text or "executable doesn't exist" in text.lower()


class PlaywrightDriver:
    """Playwright on the event loop that first asks for a browser (one driver per loop)."""

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._lock = asyncio.Lock()

    async def _ensure(self) -> Playwright:
        async with self._lock:
            if self._playwright is None:
                try:
                    from playwright.async_api import (  # pyright: ignore[reportMissingImports]
                        async_playwright,
                    )
                except ImportError as exc:
                    raise PlaywrightMissing() from exc
                self._playwright = await async_playwright().start()
            return self._playwright

    async def launch(self, spec: LaunchSpec) -> BrowserHandle:
        playwright = await self._ensure()
        try:
            context = await playwright.chromium.launch_persistent_context(
                str(spec.profile_dir),
                headless=spec.headless,
                executable_path=spec.executable or None,
                args=list(spec.args),
                env=dict(spec.env),
                chromium_sandbox=spec.sandbox,
                timeout=spec.timeout_s * 1000,
                service_workers="block",
                proxy=cast(Any, launch_proxy(spec.env)),
            )
        except Exception as exc:
            if _missing_executable(exc):
                raise BrowserMissing() from exc
            raise
        context.set_default_timeout(spec.command_timeout_s * 1000)
        page = context.pages[0] if context.pages else await context.new_page()
        return _PlaywrightHandle(context, context.browser, page, owns_browser=True)

    async def attach(
        self, cdp_url: str, *, timeout_s: float, command_timeout_s: float
    ) -> BrowserHandle:
        playwright = await self._ensure()
        browser = await playwright.chromium.connect_over_cdp(cdp_url, timeout=timeout_s * 1000)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        context.set_default_timeout(command_timeout_s * 1000)
        page = await context.new_page()  # our own tab: the user's tabs are left alone
        return _PlaywrightHandle(context, browser, page, owns_browser=False)

    async def stop(self) -> None:
        async with self._lock:
            playwright, self._playwright = self._playwright, None
        if playwright is not None:
            await playwright.stop()
