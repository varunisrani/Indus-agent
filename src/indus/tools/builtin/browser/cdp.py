"""The page supervisor (dialogs, frames, console, reconnects) and the ``browser_cdp`` /
``browser_dialog`` tools.

One ``Supervisor`` listens to each session's page:

- console lines and uncaught page errors go into bounded buffers that ``browser_console`` reads;
- the frame list is kept (up to ``MAX_FRAMES``) and shown in snapshots when the page has more
  than one frame;
- a native dialog (alert, confirm, prompt, beforeunload) is handled by ``browser.dialog_policy``:
  ``must_respond`` (default) keeps it pending for ``browser_dialog`` and dismisses it after
  ``browser.dialog_timeout_s`` (300 s) as a safety net; ``auto_dismiss`` / ``auto_accept`` answer
  at once. Pending dialogs appear in every snapshot; answered ones stay in a short history;
- for a session attached to a browser over CDP, a lost connection is retried with backoff; after
  ``MAX_RECONNECT_FAILURES`` failures in a row the supervisor gives up and the session manager
  replaces the session on its next use.

``browser_cdp`` sends one DevTools Protocol command on the session's own CDP channel (page level;
``Browser.*`` and ``Target.*`` go to the browser-level channel). Its timeout is clamped to
``1..MAX_CDP_TIMEOUT_S``. The answer is redacted except fields the protocol defines as binary
(screenshots, PDFs, response bodies flagged base64). The tool is offered only when a CDP endpoint
is configured and answers (``endpoint_live``).
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit

from indus.foundation.clock import Clock, SystemClock
from indus.foundation.scope import Scope
from indus.safety.redaction import redact_output
from indus.tools.builtin.browser.driver import DialogLike, FrameInfo

__all__ = [
    "CDP_SCHEMA",
    "DIALOG_POLICIES",
    "DIALOG_SCHEMA",
    "MAX_CDP_TIMEOUT_S",
    "MAX_RECONNECT_FAILURES",
    "Supervisor",
    "clamp_cdp_timeout",
    "endpoint_live",
    "normalise_endpoint",
    "redact_cdp",
    "safe_endpoint",
]

log = logging.getLogger(__name__)

DIALOG_POLICIES = ("must_respond", "auto_dismiss", "auto_accept")
DEFAULT_DIALOG_POLICY = "must_respond"
DEFAULT_DIALOG_TIMEOUT_S = 300.0
MAX_RECONNECT_FAILURES = 5
RECONNECT_FIRST_WAIT_S = 0.5
RECONNECT_MAX_WAIT_S = 8.0
MAX_CONSOLE = 500
MAX_ERRORS = 200
MAX_RECENT_DIALOGS = 20
MAX_FRAMES = 30
DEFAULT_CDP_TIMEOUT_S = 30.0
MIN_CDP_TIMEOUT_S = 1.0
MAX_CDP_TIMEOUT_S = 300.0
PROBE_TIMEOUT_S = 1.5
CDP_DOCS = "https://chromedevtools.github.io/devtools-protocol/"
BROWSER_LEVEL_DOMAINS = ("Browser.", "Target.")


# -- the supervisor ---------------------------------------------------------------------------


@dataclass
class PendingDialog:
    dialog_id: str
    kind: str
    message: str
    default_value: str
    opened_at: float
    handle: DialogLike = field(repr=False)

    def to_dict(self, scope: Scope | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.dialog_id,
            "type": self.kind,
            "message": redact_output(self.message, force=True, scope=scope),
        }
        if self.kind == "prompt" and self.default_value:
            out["default_prompt"] = redact_output(self.default_value, force=True, scope=scope)
        return out


Reconnect = Callable[[], Awaitable[None]]


class Supervisor:
    """Listens to one session's page (see the module notes); a ``PageListener``."""

    def __init__(
        self,
        *,
        policy: str = DEFAULT_DIALOG_POLICY,
        dialog_timeout_s: float = DEFAULT_DIALOG_TIMEOUT_S,
        clock: Clock | None = None,
        scope: Scope | None = None,
        reconnect: Reconnect | None = None,
    ) -> None:
        self.policy = policy if policy in DIALOG_POLICIES else DEFAULT_DIALOG_POLICY
        self.dialog_timeout_s = max(1.0, float(dialog_timeout_s))
        self.clock = clock if clock is not None else SystemClock()
        self.scope = scope
        self.reconnect = reconnect
        self.console: deque[tuple[str, str]] = deque(maxlen=MAX_CONSOLE)
        self.errors: deque[str] = deque(maxlen=MAX_ERRORS)
        self.pending: dict[str, PendingDialog] = {}
        self.recent: deque[dict[str, Any]] = deque(maxlen=MAX_RECENT_DIALOGS)
        self.frames: list[FrameInfo] = []
        self.connected = True
        self.gave_up = False
        self.reconnect_failures = 0
        self._numbers = itertools.count(1)
        self._tasks: set[asyncio.Task[Any]] = set()
        self._dialog_event = asyncio.Event()
        self._stopped = False

    # -- listener side ---------------------------------------------------------------------

    def on_console(self, kind: str, text: str) -> None:
        self.console.append((kind, text))

    def on_page_error(self, message: str) -> None:
        self.errors.append(message)

    def on_frames(self, frames: list[FrameInfo]) -> None:
        self.frames = list(frames[:MAX_FRAMES])

    def on_dialog(self, dialog: DialogLike) -> None:
        dialog_id = f"d{next(self._numbers)}"
        record = PendingDialog(
            dialog_id,
            dialog.kind,
            dialog.message,
            dialog.default_value,
            self.clock.monotonic(),
            dialog,
        )
        if self.policy == "must_respond":
            self.pending[dialog_id] = record
            self._dialog_event.set()
            self._spawn(self._safety_dismiss(dialog_id))
            return
        action = "accept" if self.policy == "auto_accept" else "dismiss"
        self._spawn(self._answer(record, action, None, by="policy"))

    def on_disconnected(self) -> None:
        if self._stopped or not self.connected:
            return
        self.connected = False
        if self.reconnect is None:
            self.gave_up = True
            return
        self._spawn(self._reconnect_loop())

    # -- dialogs ---------------------------------------------------------------------------

    def _spawn(self, coro: Awaitable[Any]) -> None:
        try:
            task = asyncio.ensure_future(coro)
        except RuntimeError:  # no running loop (a listener called from elsewhere)
            log.debug("browser supervisor: no event loop for a background step")
            if asyncio.iscoroutine(coro):
                coro.close()
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _answer(
        self, record: PendingDialog, action: str, prompt_text: str | None, *, by: str
    ) -> None:
        self.pending.pop(record.dialog_id, None)
        if not self.pending:
            self._dialog_event.clear()
        try:
            if action == "accept":
                await record.handle.accept(prompt_text if record.kind == "prompt" else None)
            else:
                await record.handle.dismiss()
        finally:
            entry = record.to_dict(self.scope)
            entry["answer"] = action
            entry["by"] = by
            self.recent.append(entry)

    async def _safety_dismiss(self, dialog_id: str) -> None:
        await self.clock.sleep(self.dialog_timeout_s)
        record = self.pending.get(dialog_id)
        if record is not None:
            log.info(
                "dialog %s dismissed after %.0f s without an answer",
                dialog_id,
                self.dialog_timeout_s,
            )
            with contextlib.suppress(Exception):
                await self._answer(record, "dismiss", None, by="timeout")

    async def wait_dialog(self) -> None:
        """Return once a dialog is pending (at once when one already is)."""
        await self._dialog_event.wait()

    def pending_dialogs(self) -> list[dict[str, Any]]:
        return [record.to_dict(self.scope) for record in self.pending.values()]

    async def respond(
        self, action: str, prompt_text: str | None = None, dialog_id: str | None = None
    ) -> dict[str, Any]:
        if action not in ("accept", "dismiss"):
            return {"success": False, "error": "action must be 'accept' or 'dismiss'"}
        if not self.pending:
            return {"success": False, "error": "No dialog is open on this page."}
        if dialog_id:
            record = self.pending.get(dialog_id)
            if record is None:
                known = ", ".join(self.pending)
                return {"success": False, "error": f"No open dialog {dialog_id!r} (open: {known})."}
        elif len(self.pending) > 1:
            known = ", ".join(self.pending)
            return {
                "success": False,
                "error": f"{len(self.pending)} dialogs are open ({known}); pass dialog_id.",
            }
        else:
            record = next(iter(self.pending.values()))
        shown = record.to_dict(self.scope)
        try:
            await self._answer(record, action, prompt_text, by="agent")
        except Exception as exc:
            return {"success": False, "error": f"The dialog could not be answered: {exc}"}
        return {"success": True, "action": action, "dialog": shown}

    # -- views -----------------------------------------------------------------------------

    def view(self) -> dict[str, Any]:
        """What a snapshot shows: open dialogs, recent answers, frames when there are several."""
        out: dict[str, Any] = {}
        if self.pending:
            out["pending_dialogs"] = self.pending_dialogs()
        if self.recent:
            out["recent_dialogs"] = list(self.recent)[-5:]
        if len(self.frames) > 1:
            out["frames"] = [
                {
                    "frame_id": f.frame_id,
                    "url": redact_output(f.url, force=True, scope=self.scope),
                    "name": f.name,
                    "parent_id": f.parent_id,
                }
                for f in self.frames
            ]
        return out

    def drain_console(self, clear: bool) -> tuple[list[tuple[str, str]], list[str]]:
        messages, errors = list(self.console), list(self.errors)
        if clear:
            self.console.clear()
            self.errors.clear()
        return messages, errors

    # -- reconnecting ----------------------------------------------------------------------

    async def _reconnect_loop(self) -> None:
        wait = RECONNECT_FIRST_WAIT_S
        while not self._stopped and self.reconnect is not None:
            try:
                await self.reconnect()
            except Exception as exc:
                self.reconnect_failures += 1
                log.warning(
                    "browser reconnect failed (%d of %d): %s",
                    self.reconnect_failures,
                    MAX_RECONNECT_FAILURES,
                    redact_output(str(exc), force=True, scope=self.scope),
                )
                if self.reconnect_failures >= MAX_RECONNECT_FAILURES:
                    self.gave_up = True
                    return
                await self.clock.sleep(wait)
                wait = min(wait * 2, RECONNECT_MAX_WAIT_S)
                continue
            self.connected = True
            self.reconnect_failures = 0
            return

    async def stop(self) -> None:
        self._stopped = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(BaseException):
                await task


# -- endpoints ----------------------------------------------------------------------------------


def normalise_endpoint(text: str) -> str | None:
    """A CDP address as typed (``host:port``, ``http://…``, ``ws://…``) in a form Playwright takes,
    or ``None`` when it cannot be one. A ``/devtools/browser/<id>`` path is kept."""
    raw = text.strip()
    if not raw:
        return None
    parts = urlsplit(raw if "://" in raw else f"http://{raw}")
    if parts.scheme not in ("http", "https", "ws", "wss") or not parts.hostname:
        return None
    try:
        _ = parts.port
    except ValueError:
        return None
    if parts.path.startswith("/devtools/browser/"):
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def safe_endpoint(url: str, scope: Scope | None = None) -> str:
    """The endpoint for display: no user info, no query, secrets masked."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    netloc = f"{host}:{parts.port}" if parts.port else host
    shown = urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    return redact_output(shown, force=True, scope=scope)


def _version_url(endpoint: str) -> str:
    parts = urlsplit(endpoint)
    scheme = "https" if parts.scheme in ("https", "wss") else "http"
    return urlunsplit((scheme, parts.netloc, "/json/version", "", ""))


def endpoint_live(endpoint: str, timeout_s: float = PROBE_TIMEOUT_S) -> bool:
    """True when a DevTools endpoint answers at ``endpoint`` (an HTTP check of
    ``/json/version``; a ``ws://…/devtools/browser/…`` address is checked on its host)."""
    import httpx

    try:
        response = httpx.get(_version_url(endpoint), timeout=timeout_s, trust_env=False)
    except httpx.HTTPError:
        return False
    if response.status_code != 200:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    return isinstance(body, dict) and "webSocketDebuggerUrl" in body


# -- browser_cdp -----------------------------------------------------------------------------------

# Results the protocol defines as base64 data: left untouched (masking would corrupt the bytes).
ALWAYS_BINARY: Mapping[str, tuple[str, ...]] = {
    "Page.captureScreenshot": ("data",),
    "Page.printToPDF": ("data",),
    "Network.streamResourceContent": ("bufferedData",),
    "HeadlessExperimental.beginFrame": ("screenshotData",),
}
# Fields that are base64 only when the same object says ``base64Encoded: true``.
FLAGGED_BINARY: Mapping[str, tuple[str, ...]] = {
    "Network.getResponseBody": ("body",),
    "Fetch.getResponseBody": ("body",),
    "IO.read": ("data",),
    "Network.getRequestPostData": ("postData",),
}


def clamp_cdp_timeout(value: object) -> float:
    if value is None or isinstance(value, bool):
        return DEFAULT_CDP_TIMEOUT_S
    try:
        number = float(cast(Any, value))
    except (TypeError, ValueError):
        return DEFAULT_CDP_TIMEOUT_S
    if number != number:  # NaN
        return DEFAULT_CDP_TIMEOUT_S
    return max(MIN_CDP_TIMEOUT_S, min(number, MAX_CDP_TIMEOUT_S))


def redact_cdp(
    method: str, result: Mapping[str, Any], scope: Scope | None = None
) -> dict[str, Any]:
    """``result`` with text redacted, top-level binary fields of ``method`` kept as sent."""
    from indus.tools.builtin.browser.snapshot import redact_value

    always = ALWAYS_BINARY.get(method, ())
    flagged = FLAGGED_BINARY.get(method, ()) if result.get("base64Encoded") is True else ()
    out: dict[str, Any] = {}
    for key, value in result.items():
        if key in always or key in flagged:
            out[key] = value
        else:
            out[str(redact_value(key, scope))] = redact_value(value, scope)
    return out


def browser_level(method: str) -> bool:
    return method.startswith(BROWSER_LEVEL_DOMAINS)


def cdp_error(message: str, method: str = "") -> str:
    body: dict[str, Any] = {"success": False, "error": message, "cdp_docs": CDP_DOCS}
    if method:
        body["method"] = method
    return json.dumps(body, ensure_ascii=False)


CDP_SCHEMA: dict[str, Any] = {
    "name": "browser_cdp",
    "description": (
        "Send one raw Chrome DevTools Protocol command to the browser session, for things the "
        "other browser tools cannot do (cookies, network conditions, emulation, performance "
        "data). Page-level domains (Page, Runtime, DOM, Network, Emulation …) run on the "
        "current tab; Browser.* and Target.* run at browser level. Each call is independent: "
        "event subscriptions do not carry over. Method names and parameters follow the "
        f"protocol reference at {CDP_DOCS}. Prefer the dedicated browser tools when they can "
        "do the job."
    ),
    "parameters": {
        "type": "object",
        "required": ["method"],
        "properties": {
            "method": {
                "type": "string",
                "description": "Protocol method, e.g. 'Network.getCookies' or 'Runtime.evaluate'.",
            },
            "params": {
                "type": "object",
                "description": "The method's parameters; leave out when it takes none.",
                "additionalProperties": True,
            },
            "timeout": {
                "type": "number",
                "description": (
                    f"Seconds to wait for the answer (default {DEFAULT_CDP_TIMEOUT_S:g}, "
                    f"at most {MAX_CDP_TIMEOUT_S:g})."
                ),
                "default": DEFAULT_CDP_TIMEOUT_S,
            },
        },
    },
}

DIALOG_SCHEMA: dict[str, Any] = {
    "name": "browser_dialog",
    "description": (
        "Answer a JavaScript dialog (alert, confirm, prompt or beforeunload) that is holding up "
        "the page. Open dialogs are listed under pending_dialogs in browser_snapshot results "
        "and in the error of any browser call they block. 'accept' presses OK (for prompt, "
        "with prompt_text as the answer; for beforeunload, leaves the page); 'dismiss' presses "
        "Cancel. Pass dialog_id only when more than one dialog is open."
    ),
    "parameters": {
        "type": "object",
        "required": ["action"],
        "properties": {
            "action": {"type": "string", "enum": ["accept", "dismiss"]},
            "prompt_text": {
                "type": "string",
                "description": "The answer typed into a prompt() dialog; ignored for others.",
            },
            "dialog_id": {
                "type": "string",
                "description": "Which open dialog to answer (from pending_dialogs[].id).",
            },
        },
    },
}
