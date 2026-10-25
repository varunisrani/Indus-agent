"""Alternative local engines (P24-39): Lightpanda with a Chrome fallback, and Camofox.

``browser.engine`` picks the local engine: ``chromium`` (the P10 default), ``lightpanda`` or
``camofox``. The session manager asks the engine for an ``Opened`` (the handle, the label that
says what served the page, the cleanups to run when the session ends) through the small
``SessionHost`` seam, so this module never imports the manager.

**Lightpanda** is a display-less engine. ``Lightpanda.open`` finds the binary (``indus pm``'s
``tool_path``, then the scope's PATH), starts ``lightpanda serve --host 127.0.0.1 --port <free>``
in its own process group with the scope's credential-free browser environment (R02.22), waits up
to ``READY_TIMEOUT_S`` (10 s on the manager's clock) for its CDP discovery answer and attaches to
it with the request guard. The server is stopped on every way out. Its page is wrapped in
``LightPage``: a screenshot always goes to a ``ChromeFallback`` (a temporary local Chromium on the
same address, guarded, closed at once, its folder removed); an outline, evaluation or click that
fails on Lightpanda, or an outline that comes back empty, is retried the same way; a navigation
Lightpanda cannot load is opened in Chrome and the commands that follow run there until the next
navigation Lightpanda can load. Other commands are never retried. ``LightPage.served_by`` and
``notes`` tell the tools what happened.

**Camofox** is a stealth engine run as its own local server (``browser.camofox_url``, default
``http://127.0.0.1:9377``; optional ``CAMOFOX_API_KEY`` as a bearer key). ``CamofoxPage`` drives
one tab over its REST API under a per-session user id; the engine keeps its records in its own
state folder ``<home>/cache/browser/camofox/`` (``sessions.json``: the user ids this profile
opened, so a later start can close those left behind by a dead process). Camofox loads pages in
its own process, so only navigations can be checked (before and after), not sub-requests.

A missing binary or a server that does not answer is ``EngineMissing`` with an install hint.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import logging
import os
import re
import secrets
import shutil
import signal
import socket
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast

import httpx

from indus.foundation.clock import Clock
from indus.foundation.fsio import atomic_write_text
from indus.foundation.scope import Scope
from indus.platform.locate import which
from indus.tools.builtin.browser.driver import (
    BrowserHandle,
    Driver,
    FrameInfo,
    LaunchSpec,
    PageDriver,
    PageListener,
    RefNotFound,
)
from indus.tools.builtin.browser.guard import RequestGate

__all__ = [
    "CAMOFOX_DEFAULT_URL",
    "LIGHT_FALLBACK_COMMANDS",
    "READY_TIMEOUT_S",
    "AsyncioLauncher",
    "Camofox",
    "CamofoxPage",
    "ChromeFallback",
    "EngineMissing",
    "Lightpanda",
    "LightPage",
    "Opened",
    "ServerProcess",
    "SessionHost",
    "locate_tool",
]

log = logging.getLogger(__name__)

READY_TIMEOUT_S = 10.0
READY_POLL_S = 0.1
STOP_GRACE_S = 5.0
LOG_TAIL_CHARS = 2000
LIGHT_FALLBACK_COMMANDS = frozenset({"open", "snapshot", "screenshot", "eval", "click"})
CAMOFOX_DEFAULT_URL = "http://127.0.0.1:9377"
CAMOFOX_TIMEOUT_S = 30.0
CAMOFOX_NAV_TIMEOUT_S = 60.0
STATE_DIR = Path("cache") / "browser"

LIGHTPANDA_HINT = (
    "browser.engine is 'lightpanda' but no lightpanda program was found. Install it with "
    "`indus pm install lightpanda` (or put `lightpanda` on PATH), or set browser.engine to "
    "chromium."
)
LIGHTPANDA_WINDOWS = (
    "Lightpanda has no Windows build; set browser.engine to chromium (or run Indus under WSL2)."
)
CAMOFOX_HINT = (
    "browser.engine is 'camofox' but no Camofox server answers at {url}. Start it (for example "
    "`docker run -p 9377:9377 jo-inc/camofox-browser`), set browser.camofox_url, or set "
    "browser.engine to chromium."
)


class EngineMissing(RuntimeError):
    """The chosen engine is not installed or not running (the text says how to fix it)."""


# -- the seam the session manager offers -------------------------------------------------------


@dataclass
class Opened:
    """What an alternative backend hands the session manager."""

    handle: BrowserHandle
    engine: str
    cleanups: list[Callable[[], Awaitable[None]]] = field(
        default_factory=list[Callable[[], Awaitable[None]]]
    )
    user_dir: Path | None = None  # a folder the session uses but must never remove
    notice: str | None = None


class SessionHost(Protocol):
    """The parts of ``SessionManager`` the backends use."""

    @property
    def clock(self) -> Clock: ...

    def driver(self) -> Driver: ...

    def launch_spec(self, scope: Scope, folder: Path) -> LaunchSpec: ...

    def new_profile(self, scope: Scope, task_id: str) -> Path: ...

    async def guarded(self, handle: BrowserHandle, scope: Scope) -> BrowserHandle: ...

    def env_for(self, scope: Scope) -> dict[str, str]: ...


# -- finding helper programs ------------------------------------------------------------------


def locate_tool(scope: Scope, name: str) -> Path | None:
    """``name``'s executable: ``indus pm``'s ``tool_path`` when that module offers it (it never
    installs anything), else the scope's PATH."""
    try:
        pm = importlib.import_module("indus.pm.api")
        finder = getattr(pm, "tool_path", None)
        if callable(finder):
            found = finder(scope, name)
            if found:
                return Path(cast(Any, found))
    except Exception:
        log.debug("indus pm could not locate %s", name, exc_info=True)
    path = scope.env.get("PATH") or ""
    found_on_path = which(name, path=path) if path else None
    return Path(found_on_path) if found_on_path else None


def free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# -- server processes -------------------------------------------------------------------------


class ServerProcess(Protocol):
    @property
    def pid(self) -> int: ...

    def poll(self) -> int | None: ...

    async def stop(self) -> None: ...


class Launcher(Protocol):
    async def start(
        self, argv: list[str], env: Mapping[str, str], log_path: Path
    ) -> ServerProcess: ...


@dataclass
class _AsyncioProcess:
    proc: asyncio.subprocess.Process

    @property
    def pid(self) -> int:
        return self.proc.pid

    def poll(self) -> int | None:
        return self.proc.returncode

    def _signal(self, sig: int) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            if sys.platform != "win32":
                os.killpg(self.proc.pid, sig)  # its own group: helpers die with it
            else:
                self.proc.send_signal(sig)

    async def stop(self) -> None:
        if self.proc.returncode is not None:
            return
        self._signal(signal.SIGTERM)
        try:
            await asyncio.wait_for(self.proc.wait(), STOP_GRACE_S)
        except TimeoutError:
            self._signal(getattr(signal, "SIGKILL", signal.SIGTERM))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.proc.wait(), STOP_GRACE_S)


class AsyncioLauncher:
    """Starts a server in its own session; stderr goes to a log file, never a pipe."""

    async def start(self, argv: list[str], env: Mapping[str, str], log_path: Path) -> ServerProcess:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "wb") as sink:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=sink,
                env=dict(env),
                start_new_session=sys.platform != "win32",
            )
        return _AsyncioProcess(proc)


async def cdp_answers(url: str) -> bool:
    """True when ``url``'s CDP discovery document answers (loopback only, no proxy)."""
    try:
        async with httpx.AsyncClient(timeout=1.0, trust_env=False) as client:
            response = await client.get(f"{url}/json/version")
    except httpx.HTTPError:
        return False
    return response.status_code == 200


def _log_tail(path: Path) -> str:
    try:
        raw = path.read_bytes()[-LOG_TAIL_CHARS:].decode("utf-8", errors="replace")
    except OSError:
        return ""
    lines = [line for line in raw.strip().splitlines() if line.strip()]
    return lines[-1] if lines else ""


# -- the Chrome fallback ----------------------------------------------------------------------


class ChromeFallback:
    """Runs one page command in a temporary local Chromium opened on ``url``."""

    def __init__(self, host: SessionHost, scope: Scope, task_id: str) -> None:
        self.host = host
        self.scope = scope
        self.task_id = task_id
        self.runs = 0

    async def run[R](
        self, url: str, op: Callable[[PageDriver], Awaitable[R]], *, timeout_s: float = 60.0
    ) -> R:
        self.runs += 1
        folder = self.host.new_profile(self.scope, f"{self.task_id}-chrome-fallback")
        handle: BrowserHandle | None = None
        try:
            handle = await self.host.driver().launch(self.host.launch_spec(self.scope, folder))
            handle = await self.host.guarded(handle, self.scope)
            if url and url != "about:blank":
                await handle.page.goto(url, timeout_s)
            return await op(handle.page)
        finally:
            if handle is not None:
                with contextlib.suppress(Exception):
                    await handle.close()
            shutil.rmtree(folder, ignore_errors=True)


_SELECTOR = re.compile(r'^\[(?P<attr>[^=\]]+)="(?P<ref>[^"]+)"\]$')


def _attr_of(selector: str) -> str | None:
    found = _SELECTOR.match(selector)
    return found["attr"] if found else None


class LightPage:
    """A Lightpanda page with the Chrome fallback (see the module notes)."""

    def __init__(self, inner: PageDriver, fallback: ChromeFallback) -> None:
        self.inner = inner
        self.fallback = fallback
        self.served_by = "lightpanda"
        self.notes: list[str] = []
        self._chrome_url: str | None = None  # Lightpanda could not load this page

    def take_notes(self) -> list[str]:
        notes, self.notes = self.notes, []
        return notes

    def _note(self, command: str, why: str) -> None:
        self.served_by = "chrome-fallback"
        self.notes.append(f"Lightpanda could not serve '{command}' ({why}); Chrome was used.")

    def _here(self) -> str:
        return self._chrome_url or self.inner.url()

    async def _chrome[R](
        self, command: str, why: str, op: Callable[[PageDriver], Awaitable[R]]
    ) -> R:
        self._note(command, why)
        return await self.fallback.run(self._here(), op)

    def url(self) -> str:
        return self._chrome_url or self.inner.url()

    async def title(self) -> str:
        if self._chrome_url is not None:
            return await self._chrome("title", "the page did not load in Lightpanda", _title)
        return await self.inner.title()

    async def goto(self, url: str, timeout_s: float) -> str:
        try:
            landed = await self.inner.goto(url, timeout_s)
        except Exception as exc:
            self._chrome_url = url
            self._note("open", type(exc).__name__)
            return await self.fallback.run(url, _current_url, timeout_s=timeout_s)
        self._chrome_url = None
        self.served_by = "lightpanda"
        return landed

    async def go_back(self, timeout_s: float) -> str:
        self._chrome_url = None
        return await self.inner.go_back(timeout_s)

    async def outline(self, full: bool, attr: str) -> list[dict[str, Any]]:
        async def on_chrome(page: PageDriver) -> list[dict[str, Any]]:
            return await page.outline(full, attr)

        if self._chrome_url is not None:
            return await self._chrome("snapshot", "the page did not load in Lightpanda", on_chrome)
        try:
            rows = await self.inner.outline(full, attr)
        except Exception as exc:
            return await self._chrome("snapshot", type(exc).__name__, on_chrome)
        if not rows:
            return await self._chrome("snapshot", "the outline came back empty", on_chrome)
        self.served_by = "lightpanda"
        return rows

    async def click(self, selector: str, timeout_s: float) -> None:
        async def on_chrome(page: PageDriver) -> None:
            attr = _attr_of(selector)
            if attr is not None:
                await page.outline(False, attr)  # stamp the refs in the fresh page first
            await page.click(selector, timeout_s)

        if self._chrome_url is not None:
            await self._chrome("click", "the page did not load in Lightpanda", on_chrome)
            return
        try:
            await self.inner.click(selector, timeout_s)
        except RefNotFound:
            raise
        except Exception as exc:
            await self._chrome("click", type(exc).__name__, on_chrome)

    async def evaluate(self, expression: str) -> Any:
        async def on_chrome(page: PageDriver) -> Any:
            return await page.evaluate(expression)

        if self._chrome_url is not None:
            return await self._chrome("eval", "the page did not load in Lightpanda", on_chrome)
        try:
            return await self.inner.evaluate(expression)
        except Exception as exc:
            return await self._chrome("eval", type(exc).__name__, on_chrome)

    async def screenshot(self, path: Path, *, label_attr: str | None = None) -> list[str]:
        async def on_chrome(page: PageDriver) -> list[str]:
            if label_attr:
                await page.outline(False, label_attr)
            return await page.screenshot(path, label_attr=label_attr)

        return await self._chrome("screenshot", "it has no renderer", on_chrome)

    # Commands outside the fixed fallback set go to Lightpanda only.

    async def fill(self, selector: str, text: str, timeout_s: float) -> None:
        await self.inner.fill(selector, text, timeout_s)

    async def scroll(self, dy: int) -> None:
        await self.inner.scroll(dy)

    async def press(self, key: str, timeout_s: float) -> None:
        await self.inner.press(key, timeout_s)

    async def images(self) -> list[dict[str, Any]]:
        return await self.inner.images()

    async def cdp(
        self, method: str, params: Mapping[str, Any], *, browser_level: bool = False
    ) -> dict[str, Any]:
        return await self.inner.cdp(method, params, browser_level=browser_level)


async def _title(page: PageDriver) -> str:
    return await page.title()


async def _current_url(page: PageDriver) -> str:
    return page.url()


class LightHandle:
    """An attached Lightpanda browser whose page has the Chrome fallback."""

    def __init__(self, inner: BrowserHandle, fallback: ChromeFallback) -> None:
        self.inner = inner
        self._page = LightPage(inner.page, fallback)

    @property
    def page(self) -> LightPage:
        return self._page

    def listen(self, listener: PageListener) -> None:
        self.inner.listen(listener)

    async def guard(self, gate: RequestGate) -> None:
        await self.inner.guard(gate)

    async def close(self) -> None:
        await self.inner.close()


# -- Lightpanda --------------------------------------------------------------------------------


type Probe = Callable[[str], Awaitable[bool]]


class Lightpanda:
    """Starts one ``lightpanda serve`` per session (see the module notes)."""

    def __init__(
        self,
        *,
        launcher: Launcher | None = None,
        probe: Probe = cdp_answers,
        locate: Callable[[Scope, str], Path | None] = locate_tool,
        platform: str | None = None,
    ) -> None:
        self.launcher: Launcher = launcher if launcher is not None else AsyncioLauncher()
        self.probe = probe
        self.locate = locate
        self.platform = platform or sys.platform

    def state_dir(self, scope: Scope) -> Path:
        return scope.home / STATE_DIR / "lightpanda"

    async def _wait_ready(
        self, host: SessionHost, proc: ServerProcess, url: str, log: Path
    ) -> None:
        clock = host.clock
        deadline = clock.monotonic() + READY_TIMEOUT_S
        while True:
            if await self.probe(url):
                return
            code = proc.poll()
            if code is not None:
                tail = _log_tail(log)
                raise EngineMissing(
                    f"lightpanda serve exited with code {code} before it answered"
                    + (f": {tail}" if tail else ".")
                )
            if clock.monotonic() >= deadline:
                await proc.stop()
                raise EngineMissing(
                    f"lightpanda serve did not answer within {READY_TIMEOUT_S:g} s; it was stopped."
                )
            await clock.sleep(READY_POLL_S)

    async def open(self, host: SessionHost, scope: Scope, task_id: str) -> Opened:
        if self.platform == "win32":
            raise EngineMissing(LIGHTPANDA_WINDOWS)
        binary = self.locate(scope, "lightpanda")
        if binary is None:
            raise EngineMissing(LIGHTPANDA_HINT)
        port = free_loopback_port()
        url = f"http://127.0.0.1:{port}"
        argv = [str(binary), "serve", "--host", "127.0.0.1", "--port", str(port)]
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", task_id)[:60] or "task"
        log_path = self.state_dir(scope) / f"{safe}-{secrets.token_hex(3)}.log"
        proc = await self.launcher.start(argv, host.env_for(scope), log_path)

        async def stop() -> None:
            with contextlib.suppress(Exception):
                await proc.stop()
            log_path.unlink(missing_ok=True)

        try:
            await self._wait_ready(host, proc, url, log_path)
            handle = await host.driver().attach(url, timeout_s=60.0, command_timeout_s=30.0)
            handle = await host.guarded(handle, scope)
        except BaseException:
            await stop()
            raise
        light = LightHandle(handle, ChromeFallback(host, scope, task_id))
        return Opened(light, "lightpanda", cleanups=[stop])


# -- Camofox ----------------------------------------------------------------------------------

_SNAPSHOT_LINE = re.compile(
    r"^(?P<indent>\s*)-\s+(?P<role>[A-Za-z][\w-]*)(?:\s+\"(?P<name>[^\"]*)\")?(?P<rest>.*)$"
)
_REF = re.compile(r"\[(?:ref=)?(?P<ref>e\d+)\]")
_LEVEL = re.compile(r"\[level=(?P<level>\d)\]")
_URL_LINE = re.compile(r"/url:\s*(?P<url>\S+)")


def parse_snapshot(text: str) -> list[dict[str, Any]]:
    """Outline rows from a Camofox accessibility snapshot (``- role "name" [e3]`` lines)."""
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        found = _SNAPSHOT_LINE.match(line)
        if found is None:
            continue
        row: dict[str, Any] = {
            "depth": len(found["indent"]) // 2,
            "role": found["role"],
            "name": found["name"] or "",
        }
        ref = _REF.search(found["rest"])
        if ref is not None:
            row["ref"] = ref["ref"]
        level = _LEVEL.search(found["rest"])
        if level is not None:
            row["level"] = int(level["level"])
        rows.append(row)
    return rows


def snapshot_images(text: str) -> list[dict[str, Any]]:
    lines = text.splitlines()
    images: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        found = _SNAPSHOT_LINE.match(line)
        if found is None or found["role"] != "img":
            continue
        following = lines[index + 1] if index + 1 < len(lines) else ""
        src = _URL_LINE.search(following)
        images.append({"src": src["url"] if src else "", "alt": found["name"] or ""})
    return images


class CamofoxError(RuntimeError):
    pass


type CamofoxClientFactory = Callable[[float], httpx.AsyncClient]


def _camofox_client(timeout_s: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout_s, trust_env=False, follow_redirects=False)


class CamofoxPage:
    """One Camofox tab driven over REST (see the module notes)."""

    def __init__(
        self,
        base_url: str,
        user_id: str,
        session_key: str,
        headers: Mapping[str, str],
        client_factory: CamofoxClientFactory = _camofox_client,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.user_id = user_id
        self.session_key = session_key
        self.headers = dict(headers)
        self.client_factory = client_factory
        self.tab_id: str | None = None
        self.current = "about:blank"
        self.last_title = ""
        self.gate: RequestGate | None = None
        self._snapshot = ""

    async def _call(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        params: Mapping[str, str] | None = None,
        timeout_s: float = CAMOFOX_TIMEOUT_S,
    ) -> httpx.Response:
        async with self.client_factory(timeout_s) as client:
            response = await client.request(
                method,
                f"{self.base_url}{path}",
                headers=self.headers,
                json=dict(body) if body is not None else None,
                params=dict(params) if params is not None else None,
            )
        return response

    async def _json(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        response = await self._call(method, path, **kw)
        if not response.is_success:
            raise CamofoxError(f"Camofox answered HTTP {response.status_code} for {path}")
        try:
            data = response.json()
        except ValueError:
            return {}
        return cast(dict[str, Any], data) if isinstance(data, dict) else {}

    async def _open_tab(self, url: str) -> None:
        data = await self._json(
            "POST",
            "/tabs",
            body={"userId": self.user_id, "listItemId": self.session_key, "url": url},
            timeout_s=CAMOFOX_NAV_TIMEOUT_S,
        )
        tab = data.get("tabId")
        if not isinstance(tab, str) or not tab:
            raise CamofoxError("Camofox did not open a tab")
        self.tab_id = tab

    def _tab(self, suffix: str) -> str:
        if self.tab_id is None:
            raise CamofoxError("No Camofox tab is open; navigate first.")
        return f"/tabs/{self.tab_id}/{suffix}"

    async def _act(self, suffix: str, body: Mapping[str, Any]) -> dict[str, Any]:
        data = await self._json("POST", self._tab(suffix), body={"userId": self.user_id, **body})
        landed = data.get("url")
        if isinstance(landed, str) and landed:
            self.current = landed
        return data

    def url(self) -> str:
        return self.current

    async def title(self) -> str:
        return self.last_title

    async def goto(self, url: str, timeout_s: float) -> str:
        if self.gate is not None:
            reason = await self.gate(url)
            if reason is not None:
                raise CamofoxError(f"Blocked: {reason}")
        data: dict[str, Any] = {}
        if self.tab_id is not None:
            response = await self._call(
                "POST",
                self._tab("navigate"),
                body={"userId": self.user_id, "url": url},
                timeout_s=CAMOFOX_NAV_TIMEOUT_S,
            )
            if response.status_code == 404:  # the server collected the tab
                log.info("Camofox tab %s was collected; opening a new one", self.tab_id)
                self.tab_id = None
            elif not response.is_success:
                raise CamofoxError(f"Camofox answered HTTP {response.status_code} for navigate")
            else:
                with contextlib.suppress(ValueError):
                    loaded = response.json()
                    data = cast(dict[str, Any], loaded) if isinstance(loaded, dict) else {}
        if self.tab_id is None:
            await self._open_tab(url)
        landed = data.get("url")
        self.current = landed if isinstance(landed, str) and landed else url
        title = data.get("title")
        self.last_title = title if isinstance(title, str) else ""
        return self.current

    async def go_back(self, timeout_s: float) -> str:
        await self._act("back", {})
        return self.current

    async def _snapshot_text(self) -> str:
        data = await self._json("GET", self._tab("snapshot"), params={"userId": self.user_id})
        text = data.get("snapshot")
        self._snapshot = text if isinstance(text, str) else ""
        return self._snapshot

    async def outline(self, full: bool, attr: str) -> list[dict[str, Any]]:
        rows = parse_snapshot(await self._snapshot_text())
        if full:
            return rows
        return [row for row in rows if "ref" in row or row["role"] == "heading"]

    @staticmethod
    def _ref(selector: str) -> str:
        found = _SELECTOR.match(selector)
        if found is None:
            raise RefNotFound(selector)
        return found["ref"]

    async def click(self, selector: str, timeout_s: float) -> None:
        await self._act("click", {"ref": self._ref(selector)})

    async def fill(self, selector: str, text: str, timeout_s: float) -> None:
        await self._act("type", {"ref": self._ref(selector), "text": text})

    async def scroll(self, dy: int) -> None:
        await self._act("scroll", {"direction": "down" if dy >= 0 else "up"})

    async def press(self, key: str, timeout_s: float) -> None:
        await self._act("press", {"key": key})

    async def images(self) -> list[dict[str, Any]]:
        return snapshot_images(self._snapshot or await self._snapshot_text())

    async def evaluate(self, expression: str) -> Any:
        raise CamofoxError("JavaScript evaluation is not available on the Camofox engine.")

    async def screenshot(self, path: Path, *, label_attr: str | None = None) -> list[str]:
        response = await self._call("GET", self._tab("screenshot"), params={"userId": self.user_id})
        if not response.is_success:
            raise CamofoxError(f"Camofox answered HTTP {response.status_code} for screenshot")
        path.write_bytes(response.content)
        return []

    async def cdp(
        self, method: str, params: Mapping[str, Any], *, browser_level: bool = False
    ) -> dict[str, Any]:
        raise CamofoxError("Raw CDP is not available on the Camofox engine.")

    async def end(self) -> None:
        response = await self._call("DELETE", f"/sessions/{self.user_id}")
        if not response.is_success and response.status_code != 404:
            log.warning(
                "Camofox did not close session %s: HTTP %s", self.user_id, response.status_code
            )


class CamofoxHandle:
    def __init__(self, page: CamofoxPage) -> None:
        self._page = page
        self.listeners: list[PageListener] = []

    @property
    def page(self) -> CamofoxPage:
        return self._page

    def listen(self, listener: PageListener) -> None:
        self.listeners.append(listener)
        listener.on_frames([FrameInfo("f1", self._page.current)])

    async def guard(self, gate: RequestGate) -> None:
        self._page.gate = gate

    async def close(self) -> None:
        await self._page.end()


class Camofox:
    """Opens one Camofox tab per session (see the module notes)."""

    label = "Camofox"
    secret_names: tuple[str, ...] = ("CAMOFOX_API_KEY",)
    """Optional bearer key (W60-L2-1: listed on the desktop's Tools & Keys)."""

    def __init__(self, client_factory: CamofoxClientFactory = _camofox_client) -> None:
        self.client_factory = client_factory

    def state_dir(self, scope: Scope) -> Path:
        return scope.home / STATE_DIR / "camofox"

    @staticmethod
    def base_url(scope: Scope) -> str:
        configured: object = None
        if scope.config is not None:
            with contextlib.suppress(Exception):
                configured = scope.config.get("browser.camofox_url")
        text = configured.strip() if isinstance(configured, str) else ""
        return (text or CAMOFOX_DEFAULT_URL).rstrip("/")

    @staticmethod
    def headers(scope: Scope) -> dict[str, str]:
        from indus.tools.builtin.browser.cloud import credential

        key = credential(scope, "CAMOFOX_API_KEY")
        return {"Authorization": f"Bearer {key}"} if key else {}

    def _records(self, scope: Scope) -> dict[str, Any]:
        path = self.state_dir(scope) / "sessions.json"
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return cast(dict[str, Any], loaded) if isinstance(loaded, dict) else {}

    def _save_records(self, scope: Scope, records: Mapping[str, Any]) -> None:
        folder = self.state_dir(scope)
        folder.mkdir(parents=True, exist_ok=True)
        atomic_write_text(folder / "sessions.json", json.dumps(dict(records), sort_keys=True))

    async def _healthy(self, url: str, headers: Mapping[str, str]) -> bool:
        try:
            async with self.client_factory(5.0) as client:
                response = await client.get(f"{url}/health", headers=dict(headers))
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    async def _close_left_behind(
        self, scope: Scope, url: str, headers: Mapping[str, str], alive: Callable[[int], bool]
    ) -> None:
        records = self._records(scope)
        kept: dict[str, Any] = {}
        for user_id, record in records.items():
            pid = record.get("pid") if isinstance(record, dict) else None
            if isinstance(pid, int) and alive(pid):
                kept[user_id] = record
                continue
            page = CamofoxPage(url, user_id, "", headers, self.client_factory)
            with contextlib.suppress(Exception):
                await page.end()
        if kept != records:
            self._save_records(scope, kept)

    async def open(
        self,
        host: SessionHost,
        scope: Scope,
        task_id: str,
        *,
        alive: Callable[[int], bool] | None = None,
    ) -> Opened:
        url = self.base_url(scope)
        headers = self.headers(scope)
        if not await self._healthy(url, headers):
            raise EngineMissing(CAMOFOX_HINT.format(url=url))
        await self._close_left_behind(scope, url, headers, alive or _pid_alive)
        user_id = f"indus-{secrets.token_hex(5)}"
        page = CamofoxPage(url, user_id, f"task-{task_id[:16]}", headers, self.client_factory)
        records = self._records(scope)
        records[user_id] = {"pid": os.getpid(), "task": task_id}
        self._save_records(scope, records)
        handle = CamofoxHandle(page)
        handle = await host.guarded(handle, scope)

        async def forget() -> None:
            current = self._records(scope)
            if current.pop(user_id, None) is not None:
                self._save_records(scope, current)

        return Opened(handle, "camofox", cleanups=[forget])


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if Path("/proc").is_dir():
        return Path("/proc", str(pid)).exists()
    try:
        os.kill(pid, 0)  # signal 0 only asks whether it exists
    except PermissionError:
        return True
    except OSError:
        return False
    return True
