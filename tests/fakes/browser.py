"""A fake browser driver for the browser tool tests (P10-12..14): no Chromium, no Playwright.

``FakeDriver(sites)`` serves ``FakeSite`` pages by URL. A site may redirect, show a title, list
elements (``el(role, name, …)``; interactive roles get refs ``e1``, ``e2`` … when an outline is
taken), images, console lines and a dialog on load. An element's ``goes_to`` makes a click
navigate; ``opens_dialog`` makes it raise a dialog. ``hang_on`` names page operations that never
finish (timeouts). Every launch spec, attach URL, page call and CDP call is recorded.

Every handle takes the session's request gate (``guard``); a site's ``embeds`` are the addresses
its page asks for on load, each passed through the gate first: ``driver.sent`` records the ones
let through, ``driver.blocked`` the refused ones with the reason. ``guard_failures`` makes the next
installs fail.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from indus.tools.builtin.browser.driver import (
    BrowserMissing,
    FrameInfo,
    LaunchSpec,
    PageListener,
    RefNotFound,
)

ACTIVE_ROLES = frozenset({"button", "link", "textbox", "checkbox", "combobox", "searchbox"})
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@dataclass
class FakeElement:
    role: str
    name: str
    value: str = ""
    goes_to: str | None = None
    opens_dialog: tuple[str, str] | None = None
    depth: int = 0


def el(role: str, name: str, **kw: Any) -> FakeElement:
    return FakeElement(role, name, **kw)


@dataclass
class FakeSite:
    title: str = "A page"
    elements: list[FakeElement] = field(default_factory=list[FakeElement])
    redirect_to: str | None = None
    images: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    console: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    errors: list[str] = field(default_factory=list[str])
    dialog_on_load: tuple[str, str] | None = None
    frames: int = 1
    embeds: list[str] = field(default_factory=list[str])


class FakeDialog:
    def __init__(self, kind: str, message: str, default_value: str = "") -> None:
        self._kind, self._message, self._default = kind, message, default_value
        self.answer: str | None = None
        self.prompt_text: str | None = None
        self.answered = asyncio.Event()

    @property
    def kind(self) -> str:
        return self._kind

    @property
    def message(self) -> str:
        return self._message

    @property
    def default_value(self) -> str:
        return self._default

    async def accept(self, prompt_text: str | None = None) -> None:
        self.answer, self.prompt_text = "accept", prompt_text
        self.answered.set()

    async def dismiss(self) -> None:
        self.answer = "dismiss"
        self.answered.set()


class FakePage:
    def __init__(self, driver: FakeDriver, handle: FakeHandle) -> None:
        self.driver = driver
        self.handle = handle
        self.current = "about:blank"
        self.history: list[str] = []
        self.refs: dict[str, FakeElement] = {}
        self.values: dict[str, str] = {}
        self.calls: list[tuple[str, Any]] = []
        self.scrolled = 0
        self.attrs: list[str] = []
        self.evaluated: list[str] = []

    async def _maybe_hang(self, name: str) -> None:
        if name in self.driver.hang_on:
            await asyncio.Event().wait()

    def url(self) -> str:
        return self.current

    async def title(self) -> str:
        site = self.driver.sites.get(self.current)
        return site.title if site is not None else ""

    async def _load(self, url: str) -> None:
        seen = 0
        site = self.driver.sites.get(url)
        while site is not None and site.redirect_to and seen < 10:
            url, seen = site.redirect_to, seen + 1
            site = self.driver.sites.get(url)
        if self.current != "about:blank":
            self.history.append(self.current)
        self.current = url
        self.refs = {}
        for listener in self.handle.listeners:
            listener.on_frames(
                [
                    FrameInfo(f"f{i + 1}", url, "", None if i == 0 else "f1")
                    for i in range(site.frames if site else 1)
                ]
            )
        if site is None:
            return
        for embed in site.embeds:
            await self.handle.request(embed)
        for kind, text in site.console:
            for listener in self.handle.listeners:
                listener.on_console(kind, text)
        for message in site.errors:
            for listener in self.handle.listeners:
                listener.on_page_error(message)
        if site.dialog_on_load is not None:
            await self._dialog(*site.dialog_on_load)

    async def _dialog(self, kind: str, message: str) -> None:
        dialog = FakeDialog(kind, message, "default" if kind == "prompt" else "")
        self.driver.dialogs.append(dialog)
        for listener in self.handle.listeners:
            listener.on_dialog(dialog)
        await dialog.answered.wait()

    async def goto(self, url: str, timeout_s: float) -> str:
        self.calls.append(("goto", (url, timeout_s)))
        await self._maybe_hang("goto")
        await self._load(url)
        return self.current

    async def go_back(self, timeout_s: float) -> str:
        self.calls.append(("back", timeout_s))
        await self._maybe_hang("back")
        if self.history:
            self.current = self.history.pop()
        return self.current

    async def outline(self, full: bool, attr: str) -> list[dict[str, Any]]:
        self.calls.append(("outline", full))
        self.attrs.append(attr)
        await self._maybe_hang("outline")
        site = self.driver.sites.get(self.current)
        rows: list[dict[str, Any]] = []
        self.refs = {}
        for element in site.elements if site is not None else []:
            if element.role == "text" and not full:
                continue
            row: dict[str, Any] = {
                "depth": element.depth,
                "role": element.role,
                "name": element.name,
            }
            if element.role in ACTIVE_ROLES:
                ref = f"e{len(self.refs) + 1}"
                self.refs[ref] = element
                row["ref"] = ref
                value = self.values.get(ref, element.value)
                if value:
                    row["value"] = value
            rows.append(row)
        return rows

    def _ref(self, selector: str) -> str:
        found = re.fullmatch(r'\[(?P<attr>[^=]+)="(?P<ref>[^"]+)"\]', selector)
        if found is None or found["attr"] != (self.attrs[-1] if self.attrs else None):
            raise RefNotFound(selector)
        if found["ref"] not in self.refs:
            raise RefNotFound(selector)
        return found["ref"]

    async def click(self, selector: str, timeout_s: float) -> None:
        self.calls.append(("click", selector))
        await self._maybe_hang("click")
        element = self.refs[self._ref(selector)]
        if element.opens_dialog is not None:
            await self._dialog(*element.opens_dialog)
        if element.goes_to is not None:
            await self._load(element.goes_to)

    async def fill(self, selector: str, text: str, timeout_s: float) -> None:
        self.calls.append(("fill", (selector, text)))
        self.values[self._ref(selector)] = text

    async def scroll(self, dy: int) -> None:
        self.calls.append(("scroll", dy))
        self.scrolled += dy

    async def press(self, key: str, timeout_s: float) -> None:
        self.calls.append(("press", key))
        target = self.driver.key_targets.get(key)
        if target is not None:
            await self._load(target)

    async def images(self) -> list[dict[str, Any]]:
        site = self.driver.sites.get(self.current)
        return list(site.images) if site is not None else []

    async def evaluate(self, expression: str) -> Any:
        self.evaluated.append(expression)
        await self._maybe_hang("evaluate")
        if expression not in self.driver.eval_answers and self.driver.eval_hook is not None:
            return await self.driver.eval_hook(self, expression)
        answer = self.driver.eval_answers.get(expression)
        if callable(answer):
            return await cast(Callable[[FakePage], Awaitable[Any]], answer)(self)
        return answer

    async def screenshot(self, path: Path, *, label_attr: str | None = None) -> list[str]:
        self.calls.append(("screenshot", label_attr))
        path.write_bytes(PNG_BYTES)
        return sorted(self.refs, key=lambda r: int(r[1:])) if label_attr else []

    async def cdp(
        self, method: str, params: Mapping[str, Any], *, browser_level: bool = False
    ) -> dict[str, Any]:
        self.driver.cdp_calls.append((method, dict(params), browser_level))
        await self._maybe_hang("cdp")
        answer = self.driver.cdp_answers.get(method)
        if isinstance(answer, BaseException):
            raise answer
        if callable(answer):
            return cast(Callable[[Mapping[str, Any]], dict[str, Any]], answer)(params)
        return dict(answer) if isinstance(answer, Mapping) else {}


class FakeHandle:
    def __init__(self, driver: FakeDriver, how: str) -> None:
        self.driver = driver
        self.how = how
        self.listeners: list[PageListener] = []
        self.closed = False
        self.gate: Callable[[str], Awaitable[str | None]] | None = None
        self._page = FakePage(driver, self)

    @property
    def page(self) -> FakePage:
        return self._page

    def listen(self, listener: PageListener) -> None:
        self.listeners.append(listener)

    async def guard(self, gate: Callable[[str], Awaitable[str | None]]) -> None:
        if self.driver.guard_failures > 0:
            self.driver.guard_failures -= 1
            raise RuntimeError("the route could not be installed")
        self.gate = gate

    async def request(self, url: str) -> None:
        """The page asks for ``url`` (an embed): through the gate, like the real route."""
        reason = await self.gate(url) if self.gate is not None else None
        if reason is None:
            self.driver.sent.append(url)
        else:
            self.driver.blocked.append((url, reason))

    async def close(self) -> None:
        if self.driver.close_failures > 0:
            self.driver.close_failures -= 1
            raise RuntimeError("the browser did not answer the close")
        self.closed = True
        self.driver.closed.append(self)

    def drop_connection(self) -> None:
        for listener in self.listeners:
            listener.on_disconnected()


class FakeDriver:
    def __init__(self, sites: Mapping[str, FakeSite] | None = None) -> None:
        self.sites: dict[str, FakeSite] = dict(sites or {})
        self.launches: list[LaunchSpec] = []
        self.attaches: list[str] = []
        self.handles: list[FakeHandle] = []
        self.closed: list[FakeHandle] = []
        self.dialogs: list[FakeDialog] = []
        self.cdp_calls: list[tuple[str, dict[str, Any], bool]] = []
        self.cdp_answers: dict[str, Any] = {}
        self.eval_answers: dict[str, Any] = {}
        # Scripted pages (P24-40): answers expressions not in ``eval_answers``.
        self.eval_hook: Callable[[FakePage, str], Awaitable[Any]] | None = None
        self.key_targets: dict[str, str] = {}
        self.hang_on: set[str] = set()
        self.close_failures = 0
        self.attach_failures = 0
        self.guard_failures = 0
        self.sent: list[str] = []
        self.blocked: list[tuple[str, str]] = []
        self.missing_browser = False
        self.stopped = False
        self.on_launch: Callable[[LaunchSpec], None] | None = None

    async def launch(self, spec: LaunchSpec) -> FakeHandle:
        if self.missing_browser:
            raise BrowserMissing("no executable at /nowhere/chrome")
        self.launches.append(spec)
        if self.on_launch is not None:
            self.on_launch(spec)
        handle = FakeHandle(self, "launch")
        self.handles.append(handle)
        return handle

    async def attach(
        self, cdp_url: str, *, timeout_s: float, command_timeout_s: float
    ) -> FakeHandle:
        self.attaches.append(cdp_url)
        if self.attach_failures > 0:
            self.attach_failures -= 1
            raise ConnectionError(f"nothing answers at {cdp_url}")
        handle = FakeHandle(self, "attach")
        self.handles.append(handle)
        return handle

    async def stop(self) -> None:
        self.stopped = True


class FakeProcesses:
    """A process table: ``alive`` pids, and pids found per ``--user-data-dir`` marker."""

    def __init__(self, alive: set[int] | None = None) -> None:
        self.live: set[int] = set(alive or ())
        self.by_marker: dict[str, list[int]] = {}
        self.killed: list[int] = []

    def alive(self, pid: int) -> bool:
        return pid in self.live

    def find(self, marker: str) -> list[int]:
        return list(self.by_marker.get(marker, []))

    def kill(self, pid: int) -> None:
        self.killed.append(pid)
        self.live.discard(pid)
