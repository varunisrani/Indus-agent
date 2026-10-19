"""The page tools: navigate, snapshot, click, type, scroll, back, press, images and console.

Every URL the browser is sent to passes the P05 gate first (``url_policy.check_url``: scheme,
credentials in the URL, the website blocklist, cloud metadata, private addresses unless
``security.allow_private_urls``). The address a page ends up on is checked again after a
navigation, a click, a key press, going back and a JavaScript evaluation; a page that landed on
a refused address is moved to ``about:blank`` and the call fails without returning its content.
JavaScript given to ``browser_console`` is refused when an ``http(s)://`` address written in it
would be refused.

Results are JSON text. Text that comes from the page is redacted here; the tool round fences
every ``browser_*`` result as untrusted data (P04 ``untrusted.py``).
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

from indus.foundation.scope import Scope, scope_or_none
from indus.safety.redaction import redact_output
from indus.safety.url_policy import Blocked, Resolver, check_url_async, normalize_url
from indus.tools.builtin.browser import snapshot
from indus.tools.builtin.browser.driver import (
    BrowserMissing,
    PageDriver,
    PlaywrightMissing,
    RefNotFound,
    ref_selector,
)
from indus.tools.builtin.browser.session import (
    BrowserSession,
    CommandTimeout,
    DialogBlocking,
    SessionManager,
    setting,
)

__all__ = ["SCHEMAS", "PageTools", "as_json", "fail", "js_url_literals", "parse_ref"]

log = logging.getLogger(__name__)

SCROLL_STEP_PX = 500
MAX_IMAGES = 200
WEDGE_GRACE_S = 5.0
BOT_TITLE_WORDS = (
    "access denied",
    "are you a robot",
    "attention required",
    "bot detected",
    "captcha",
    "checking your browser",
    "ddos protection",
    "just a moment",
    "please verify",
    "verify you are human",
    "verification required",
)
_REF = re.compile(r"^e\d{1,6}$")
_JS_URL = re.compile(r"""https?://[^\s'"`<>()\[\]{}\\]+""", re.IGNORECASE)

type Handler = Callable[..., Awaitable[str]]


def as_json(body: Mapping[str, Any]) -> str:
    return json.dumps(body, ensure_ascii=False, default=str)


def fail(message: str, **extra: Any) -> str:
    return as_json({"success": False, "error": message, **extra})


def parse_ref(raw: object) -> str | None:
    """``e5`` from ``e5``, ``@e5``, ``[e5]``, ``ref=e5``; ``None`` when it is not a ref."""
    if not isinstance(raw, str):
        return None
    text = raw.strip().strip("[]").strip()
    if text.lower().startswith("ref="):
        text = text[4:]
    text = text.lstrip("@").strip()
    return text if _REF.match(text) else None


def js_url_literals(code: str) -> list[str]:
    return [m.rstrip(".,;:") for m in _JS_URL.findall(code)]


def _fetchable(url: str) -> bool:
    try:
        return urlsplit(url).scheme.lower() in ("http", "https")
    except ValueError:
        return False


def _bot_warning(title: str) -> str | None:
    lowered = title.lower()
    if any(word in lowered for word in BOT_TITLE_WORDS):
        return (
            f"The page title {title!r} looks like a bot check or a block page. The site may "
            "have refused the browser; try again later, go through another page first, or use "
            "another source."
        )
    return None


class PageTools:
    """The page tool handlers over one ``SessionManager``."""

    def __init__(self, manager: SessionManager, *, resolver: Resolver | None = None) -> None:
        self.manager = manager
        self.resolver = resolver

    # -- shared steps -------------------------------------------------------------------------

    @staticmethod
    def task_key(task_id: str | None) -> str:
        return task_id or "default"

    async def open_session(self, task_id: str | None, scope: Scope | None) -> BrowserSession | str:
        """The task's session, or the error result text."""
        resolved = scope if scope is not None else scope_or_none()
        if resolved is None:
            return fail("The browser tools need a profile scope; none is active here.")
        try:
            return await self.manager.session(self.task_key(task_id), resolved)
        except (PlaywrightMissing, BrowserMissing) as exc:
            return fail(str(exc))
        except Exception as exc:
            log.warning("browser session could not start: %s", exc, exc_info=True)
            text = redact_output(f"{type(exc).__name__}: {exc}", force=True, scope=resolved)
            return fail(f"The browser could not be started: {text}")

    async def refused(self, url: str, scope: Scope) -> Blocked | None:
        verdict = await check_url_async(url, scope, resolver=self.resolver)
        return verdict if isinstance(verdict, Blocked) else None

    async def run[R](
        self,
        session: BrowserSession,
        op: Callable[[PageDriver], Awaitable[R]],
        *,
        label: str,
        timeout_s: float | None = None,
        dialog_aware: bool = True,
    ) -> R:
        limit = (timeout_s if timeout_s is not None else session.timeouts.command) + WEDGE_GRACE_S
        return await self.manager.run(
            session, op, timeout_s=limit, label=label, dialog_aware=dialog_aware
        )

    def failure(self, exc: BaseException, session: BrowserSession, what: str) -> str:
        """The error result for a failed page command."""
        if isinstance(exc, DialogBlocking):
            return fail(str(exc), pending_dialogs=exc.dialogs)
        if isinstance(exc, CommandTimeout):
            return fail(str(exc))
        if isinstance(exc, RefNotFound):
            return fail(f"{what}: that element is not on the page any more; take a new snapshot.")
        text = redact_output(f"{type(exc).__name__}: {exc}", force=True, scope=session.scope)
        return fail(f"{what} failed: {text}")

    async def landed_badly(self, session: BrowserSession, why: str) -> str | None:
        """When the page now sits on a refused address: blank it and return the error result."""
        url = session.page.url()
        if not _fetchable(url):
            return None
        blocked = await self.refused(url, session.scope)
        if blocked is None:
            return None
        try:
            await self.run(
                session, lambda p: p.goto("about:blank", 10.0), label="blank", timeout_s=10.0
            )
        except Exception:
            log.debug("could not blank a refused page", exc_info=True)
        shown = redact_output(url, force=True, scope=session.scope)
        return fail(f"Blocked: {why} {shown} ({blocked.reason}); the page was closed.")

    async def outline(
        self, session: BrowserSession, *, full: bool, aux: Any = None
    ) -> dict[str, Any]:
        rows = await self.run(
            session, lambda p: p.outline(full, session.ref_attr), label="snapshot"
        )
        text, refs = snapshot.render(rows)
        text = redact_output(text, force=True, scope=session.scope)
        fitted = await snapshot.fit(
            text,
            refs,
            threshold=snapshot.clamp_threshold(
                setting(session.scope, "browser.snapshot_threshold")
            ),
            home=session.scope.home,
            scope=session.scope,
            aux=aux,
            summarise=bool(setting(session.scope, "browser.snapshot_summary")),
        )
        out: dict[str, Any] = {"snapshot": fitted.text, "element_count": fitted.refs}
        if fitted.truncated:
            out["truncated"] = True
        if fitted.saved_path:
            out["full_snapshot_path"] = fitted.saved_path
        if fitted.summarised:
            out["summarised"] = True
        return out

    # -- handlers -----------------------------------------------------------------------------

    async def navigate(
        self,
        args: Mapping[str, Any],
        *,
        task_id: str | None = None,
        scope: Scope | None = None,
        aux: Any = None,
    ) -> str:
        raw = args.get("url")
        if not isinstance(raw, str) or not raw.strip():
            return fail("url is required, e.g. https://example.com")
        url = normalize_url(raw)
        resolved = scope if scope is not None else scope_or_none()
        if resolved is not None:  # refused before any browser starts
            blocked = await self.refused(url, resolved)
            if blocked is not None:
                return fail(f"Blocked: {blocked.reason}", blocked=blocked.kind)
        session = await self.open_session(task_id, resolved)
        if isinstance(session, str):
            return session
        first = session.first_open
        limit = session.timeouts.navigation(first)
        try:
            await self.run(session, lambda p: p.goto(url, limit), label="navigate", timeout_s=limit)
        except Exception as exc:
            return self.failure(exc, session, "Opening the page")
        session.first_open = False
        bad = await self.landed_badly(session, "the page redirected to")
        if bad is not None:
            return bad
        try:
            title = await self.run(session, lambda p: p.title(), label="title")
        except Exception as exc:
            return self.failure(exc, session, "Reading the page title")
        body: dict[str, Any] = {
            "success": True,
            "url": redact_output(session.page.url(), force=True, scope=session.scope),
            "title": redact_output(title, force=True, scope=session.scope),
        }
        warning = _bot_warning(title)
        if warning:
            body["bot_detection_warning"] = warning
        try:
            body.update(await self.outline(session, full=False, aux=aux))
        except DialogBlocking as exc:
            body["pending_dialogs"] = exc.dialogs
        except Exception as exc:
            log.debug("outline after navigate failed: %s", exc)
        body.update(session.supervisor.view())
        return as_json(body)

    async def snapshot(
        self,
        args: Mapping[str, Any],
        *,
        task_id: str | None = None,
        scope: Scope | None = None,
        aux: Any = None,
    ) -> str:
        session = await self.open_session(task_id, scope)
        if isinstance(session, str):
            return session
        bad = await self.landed_badly(session, "the page is on")
        if bad is not None:
            return bad
        body: dict[str, Any] = {"success": True}
        try:
            body.update(await self.outline(session, full=bool(args.get("full")), aux=aux))
        except DialogBlocking as exc:
            body["note"] = str(exc)
        except Exception as exc:
            return self.failure(exc, session, "Reading the page")
        body.update(session.supervisor.view())
        return as_json(body)

    async def _on_ref(
        self,
        args: Mapping[str, Any],
        task_id: str | None,
        scope: Scope | None,
        action: str,
        act: Callable[[PageDriver, str, float], Awaitable[None]],
    ) -> tuple[BrowserSession, str] | str:
        ref = parse_ref(args.get("ref"))
        if ref is None:
            return fail("ref must name an element from the snapshot, e.g. @e5")
        session = await self.open_session(task_id, scope)
        if isinstance(session, str):
            return session
        selector = ref_selector(session.ref_attr, ref)
        limit = session.timeouts.command
        try:
            await self.run(session, lambda p: act(p, selector, limit), label=action)
        except Exception as exc:
            return self.failure(exc, session, f"{action.capitalize()} @{ref}")
        bad = await self.landed_badly(session, f"the {action} went to")
        if bad is not None:
            return bad
        return session, ref

    async def click(
        self, args: Mapping[str, Any], *, task_id: str | None = None, scope: Scope | None = None
    ) -> str:
        done = await self._on_ref(args, task_id, scope, "click", lambda p, s, t: p.click(s, t))
        if isinstance(done, str):
            return done
        session, ref = done
        return as_json({"success": True, "clicked": f"@{ref}", **session.supervisor.view()})

    async def type_text(
        self, args: Mapping[str, Any], *, task_id: str | None = None, scope: Scope | None = None
    ) -> str:
        text = args.get("text")
        if not isinstance(text, str):
            return fail("text is required")
        done = await self._on_ref(args, task_id, scope, "type", lambda p, s, t: p.fill(s, text, t))
        if isinstance(done, str):
            return done
        session, ref = done
        shown = redact_output(text, force=True, scope=session.scope)
        return as_json({"success": True, "typed": shown, "element": f"@{ref}"})

    async def scroll(
        self, args: Mapping[str, Any], *, task_id: str | None = None, scope: Scope | None = None
    ) -> str:
        direction = str(args.get("direction") or "").strip().lower()
        if direction not in ("up", "down"):
            return fail("direction must be 'up' or 'down'")
        session = await self.open_session(task_id, scope)
        if isinstance(session, str):
            return session
        step = SCROLL_STEP_PX if direction == "down" else -SCROLL_STEP_PX
        try:
            await self.run(session, lambda p: p.scroll(step), label="scroll")
        except Exception as exc:
            return self.failure(exc, session, "Scrolling")
        return as_json({"success": True, "scrolled": direction})

    async def back(
        self, args: Mapping[str, Any], *, task_id: str | None = None, scope: Scope | None = None
    ) -> str:
        session = await self.open_session(task_id, scope)
        if isinstance(session, str):
            return session
        limit = session.timeouts.open
        try:
            await self.run(session, lambda p: p.go_back(limit), label="back", timeout_s=limit)
        except Exception as exc:
            return self.failure(exc, session, "Going back")
        bad = await self.landed_badly(session, "going back landed on")
        if bad is not None:
            return bad
        shown = redact_output(session.page.url(), force=True, scope=session.scope)
        return as_json({"success": True, "url": shown})

    async def press(
        self, args: Mapping[str, Any], *, task_id: str | None = None, scope: Scope | None = None
    ) -> str:
        key = args.get("key")
        if not isinstance(key, str) or not key.strip():
            return fail("key is required, e.g. Enter, Tab, Escape, ArrowDown")
        session = await self.open_session(task_id, scope)
        if isinstance(session, str):
            return session
        limit = session.timeouts.command
        try:
            await self.run(session, lambda p: p.press(key.strip(), limit), label="press")
        except Exception as exc:
            return self.failure(exc, session, f"Pressing {key}")
        bad = await self.landed_badly(session, "the key press went to")
        if bad is not None:
            return bad
        return as_json({"success": True, "pressed": key.strip()})

    async def images(
        self, args: Mapping[str, Any], *, task_id: str | None = None, scope: Scope | None = None
    ) -> str:
        session = await self.open_session(task_id, scope)
        if isinstance(session, str):
            return session
        bad = await self.landed_badly(session, "the page is on")
        if bad is not None:
            return bad
        try:
            found = await self.run(session, lambda p: p.images(), label="images")
        except Exception as exc:
            return self.failure(exc, session, "Listing images")
        kept: list[dict[str, Any]] = []
        for item in found:
            src = str(item.get("src") or "")
            if not src or src.lower().startswith("data:"):
                continue
            kept.append(
                {
                    "src": src,
                    "alt": str(item.get("alt") or ""),
                    "width": item.get("width"),
                    "height": item.get("height"),
                }
            )
        shown = snapshot.redact_value(kept[:MAX_IMAGES], session.scope)
        body: dict[str, Any] = {"success": True, "images": shown, "count": len(kept)}
        if len(kept) > MAX_IMAGES:
            body["note"] = f"Only the first {MAX_IMAGES} of {len(kept)} images are listed."
        return as_json(body)

    async def eval_refusal(self, code: str, scope: Scope) -> str | None:
        for literal in js_url_literals(code):
            blocked = await self.refused(literal, scope)
            if blocked is not None:
                shown = redact_output(literal, force=True, scope=scope)
                return f"Blocked: the code reaches {shown}, which is refused ({blocked.reason})."
        return None

    async def console(
        self, args: Mapping[str, Any], *, task_id: str | None = None, scope: Scope | None = None
    ) -> str:
        expression = args.get("expression")
        session = await self.open_session(task_id, scope)
        if isinstance(session, str):
            return session
        if isinstance(expression, str) and expression.strip():
            return await self._evaluate(session, expression)
        clear = bool(args.get("clear"))
        messages, errors = session.supervisor.drain_console(clear)
        bad = await self.landed_badly(session, "the page is on")
        if bad is not None:
            return bad
        body = {
            "success": True,
            "console_messages": [
                {"type": kind, "text": redact_output(text, force=True, scope=session.scope)}
                for kind, text in messages
            ],
            "js_errors": [
                {"message": redact_output(text, force=True, scope=session.scope)} for text in errors
            ],
        }
        body["total_messages"] = len(messages)
        body["total_errors"] = len(errors)
        return as_json(body)

    async def _evaluate(self, session: BrowserSession, expression: str) -> str:
        refusal = await self.eval_refusal(expression, session.scope)
        if refusal is not None:
            return fail(refusal)
        try:
            value = await self.run(session, lambda p: p.evaluate(expression), label="evaluate")
        except Exception as exc:
            return self.failure(exc, session, "Evaluating the expression")
        bad = await self.landed_badly(session, "the code moved the page to")
        if bad is not None:
            return bad
        if isinstance(value, str):
            with contextlib.suppress(ValueError):
                value = json.loads(value)
        return as_json(
            {
                "success": True,
                "result": snapshot.redact_value(value, session.scope),
                "result_type": type(value).__name__,
            }
        )


# -- schemas --------------------------------------------------------------------------------------


def _schema(name: str, description: str, required: list[str], **props: Any) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "parameters": {"type": "object", "required": required, "properties": props},
    }


_AFTER_NAVIGATE = "Open a page with browser_navigate first."

SCHEMAS: dict[str, dict[str, Any]] = {
    "browser_navigate": _schema(
        "browser_navigate",
        "Open a web page in the agent's browser (a headless Chromium kept for this task). Use it "
        "for pages that need interaction or run scripts: forms, logins, single-page apps. For "
        "plain reading prefer web_extract, and for raw files (.json, .md, .txt, APIs) a fetch: "
        "both are faster. The result gives the final address, the title and an outline of the "
        "page in which every interactive element has a ref such as @e3, so you can click or type "
        "next without a separate snapshot. Internal network addresses and addresses that carry "
        "credentials are refused.",
        ["url"],
        url={
            "type": "string",
            "description": "The address to open, e.g. https://example.com/login.",
        },
    ),
    "browser_snapshot": _schema(
        "browser_snapshot",
        "Read the current page as an outline. Interactive elements carry refs (@e1, @e2 …) for "
        "browser_click and browser_type; refs change when the page changes, so take a new "
        "snapshot after actions that alter it. full=false (default) lists interactive "
        "elements and headings; full=true adds all visible text. A long outline is cut, and the "
        "result names the saved file and the read_file call that shows the rest. Open "
        "JavaScript dialogs are listed under pending_dialogs. " + _AFTER_NAVIGATE,
        [],
        full={
            "type": "boolean",
            "default": False,
            "description": "true: include all visible text, not only interactive elements.",
        },
    ),
    "browser_click": _schema(
        "browser_click",
        "Click the element with the given ref from the latest snapshot. " + _AFTER_NAVIGATE,
        ["ref"],
        ref={"type": "string", "description": "The element's ref, e.g. @e5."},
    ),
    "browser_type": _schema(
        "browser_type",
        "Put text into a field (input, text area or editable element) given by its ref. The "
        "field is cleared first. Press Enter afterwards with browser_press when a form should "
        "be sent. " + _AFTER_NAVIGATE,
        ["ref", "text"],
        ref={"type": "string", "description": "The field's ref, e.g. @e3."},
        text={"type": "string", "description": "The text the field should hold."},
    ),
    "browser_scroll": _schema(
        "browser_scroll",
        "Scroll the page by about half a screen to reach content above or below the visible "
        "part. " + _AFTER_NAVIGATE,
        ["direction"],
        direction={"type": "string", "enum": ["up", "down"], "description": "Which way to scroll."},
    ),
    "browser_back": _schema(
        "browser_back",
        "Go back to the previous page in the browser's history. " + _AFTER_NAVIGATE,
        [],
    ),
    "browser_press": _schema(
        "browser_press",
        "Press one key on the page, for example Enter to send a form, Tab to move on, Escape "
        "to close a popup, or ArrowDown in a list. " + _AFTER_NAVIGATE,
        ["key"],
        key={"type": "string", "description": "Key name: Enter, Tab, Escape, ArrowDown, a, …"},
    ),
    "browser_get_images": _schema(
        "browser_get_images",
        "List the images on the current page: address, alt text and natural size (inline data "
        "images are left out). Useful to pick an image for the vision tools. " + _AFTER_NAVIGATE,
        [],
    ),
    "browser_console": _schema(
        "browser_console",
        "Read the page's console messages (log, warning, error …) and uncaught JavaScript "
        "errors, for example to find out why a page misbehaves. With expression, run that "
        "JavaScript in the page instead (like the developer tools console) and get its value "
        "back as JSON; code that reaches a refused address is not run. " + _AFTER_NAVIGATE,
        [],
        clear={
            "type": "boolean",
            "default": False,
            "description": "Empty the message lists after reading them.",
        },
        expression={
            "type": "string",
            "description": "JavaScript to evaluate in the page, e.g. document.title.",
        },
    ),
}
