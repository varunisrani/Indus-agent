"""The browser tools (toolset ``browser``): a headless Chromium per task, driven by Playwright.

``BrowserTools`` holds what the handlers share in a process: the ``SessionManager`` (sessions,
janitor, orphan reaper, the per-profile ``/browser`` choice), the page tools, ``browser_vision``
and the ``browser_cdp`` / ``browser_dialog`` handlers. ``register_tools`` registers the module's
default instance; tests build their own over a fake driver.

Availability: the toolset needs the ``browser`` extra (Playwright); ``browser_cdp`` is offered
only when the profile's browser choice is a CDP endpoint that answers when the tool array is
built. Every browser tool is a barrier for the parallel planner (side effects on one page).

Host seams: ``DEFAULT_TOOLS.manager.release(task_id)`` closes a task's browser (child teardown,
P10-06); ``await DEFAULT_TOOLS.manager.shutdown()`` at exit; the tool round may offer ``aux``
(the bound auxiliary client) and ``native_vision`` (the route takes images) as handler extras.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from indus.foundation.scope import Scope
from indus.safety.redaction import redact_output
from indus.safety.url_policy import Resolver
from indus.tools.builtin.browser import cdp as cdp_mod
from indus.tools.builtin.browser.driver import playwright_available
from indus.tools.builtin.browser.session import CommandTimeout, SessionManager
from indus.tools.builtin.browser.tools import SCHEMAS, PageTools, as_json, fail
from indus.tools.builtin.browser.vision import SCHEMA as VISION_SCHEMA
from indus.tools.builtin.browser.vision import VisionTool
from indus.tools.registry import ToolRegistry, ToolTraits
from indus.tools.toolsets import BROWSER_TOOLS

__all__ = [
    "DEFAULT_TOOLS",
    "EXTRA_TOOL_NAMES",
    "TOOLSET",
    "TOOL_NAMES",
    "BrowserTools",
    "register_tools",
]

TOOLSET = "browser"
MAX_RESULT_CHARS = 100_000
EXTRA_TOOL_NAMES: tuple[str, ...] = ("browser_exec", "browser_vault")
"""Registered by their own modules (P24-39, P24-40), not by ``BrowserTools``."""
TOOL_NAMES: tuple[str, ...] = tuple(n for n in BROWSER_TOOLS if n not in EXTRA_TOOL_NAMES)
"""The session tools ``BrowserTools`` registers."""
ACTS = ToolTraits(mutating=True)
_METHOD = re.compile(r"^[A-Z][A-Za-z]*\.[a-zA-Z][A-Za-z0-9]*$")


class BrowserTools:
    """The browser tools' shared state in one process (see the module notes)."""

    def __init__(
        self, manager: SessionManager | None = None, *, resolver: Resolver | None = None
    ) -> None:
        self.manager = manager if manager is not None else SessionManager(resolver=resolver)
        if resolver is not None and self.manager.resolver is None:
            self.manager.resolver = resolver  # the request guard checks with the same resolver
        self.pages = PageTools(self.manager, resolver=resolver)
        self.vision = VisionTool(self.pages)

    # -- probes ---------------------------------------------------------------------------------

    def ready(self, scope: Scope) -> bool:
        return playwright_available()

    def cdp_ready(self, scope: Scope) -> bool:
        if not self.ready(scope):
            return False
        choice = self.manager.choice_for(scope)
        return (
            choice.mode == "cdp"
            and bool(choice.cdp_url)
            and cdp_mod.endpoint_live(choice.cdp_url or "")
        )

    # -- browser_cdp / browser_dialog -----------------------------------------------------------

    async def cdp(
        self, args: Mapping[str, Any], *, task_id: str | None = None, scope: Scope | None = None
    ) -> str:
        method = args.get("method")
        if not isinstance(method, str) or not _METHOD.match(method.strip()):
            return cdp_mod.cdp_error("method must be a protocol method such as 'Page.reload'")
        method = method.strip()
        params = args.get("params")
        if params is None:
            params = {}
        if not isinstance(params, Mapping):
            return cdp_mod.cdp_error(
                f"params must be an object, not {type(params).__name__}", method
            )
        timeout = cdp_mod.clamp_cdp_timeout(args.get("timeout"))
        session = await self.pages.open_session(task_id, scope)
        if isinstance(session, str):
            return session
        refusal = await self._cdp_refusal(method, params, session.scope)
        if refusal is not None:
            return cdp_mod.cdp_error(refusal, method)
        level = cdp_mod.browser_level(method)
        call_params: dict[str, Any] = dict(params)  # pyright: ignore[reportUnknownArgumentType]
        try:
            result = await self.manager.run(
                session,
                lambda p: p.cdp(method, call_params, browser_level=level),
                timeout_s=timeout,
                label=f"cdp {method}",
                dialog_aware=False,
                recycle_on_timeout=False,
            )
        except CommandTimeout:
            return cdp_mod.cdp_error(f"No answer within {timeout:g} s.", method)
        except Exception as exc:
            text = redact_output(str(exc), force=True, scope=session.scope)
            return cdp_mod.cdp_error(f"CDP error: {text}", method)
        bad = await self.pages.landed_badly(session, "the command moved the page to")
        if bad is not None:
            return bad
        body = {
            "success": True,
            "method": method,
            "result": cdp_mod.redact_cdp(method, result, session.scope),
        }
        return json.dumps(body, ensure_ascii=False, default=str)

    async def _cdp_refusal(
        self, method: str, params: Mapping[str, Any], scope: Scope
    ) -> str | None:
        if method in ("Page.navigate", "Target.createTarget"):
            url = params.get("url")
            if isinstance(url, str) and url.strip() and url.strip() != "about:blank":
                blocked = await self.pages.refused(url.strip(), scope)
                if blocked is not None:
                    return f"Blocked: {blocked.reason}"
        if method in ("Runtime.evaluate", "Runtime.callFunctionOn"):
            code = params.get("expression") or params.get("functionDeclaration")
            if isinstance(code, str):
                return await self.pages.eval_refusal(code, scope)
        return None

    async def dialog(
        self, args: Mapping[str, Any], *, task_id: str | None = None, scope: Scope | None = None
    ) -> str:
        session = self.manager.current(self.pages.task_key(task_id))
        if session is None:
            return fail("No browser page is open for this task; nothing to answer.")
        action = str(args.get("action") or "").strip().lower()
        prompt_text = args.get("prompt_text")
        dialog_id = args.get("dialog_id")
        answer = await session.supervisor.respond(
            action,
            prompt_text if isinstance(prompt_text, str) else None,
            dialog_id if isinstance(dialog_id, str) and dialog_id.strip() else None,
        )
        return as_json(answer)

    # -- registration ---------------------------------------------------------------------------

    def register(self, registry: ToolRegistry) -> None:
        pages = self.pages
        handlers = {
            "browser_navigate": (pages.navigate, ACTS),
            "browser_snapshot": (pages.snapshot, ToolTraits()),
            "browser_click": (pages.click, ACTS),
            "browser_type": (pages.type_text, ACTS),
            "browser_scroll": (pages.scroll, ToolTraits()),
            "browser_back": (pages.back, ACTS),
            "browser_press": (pages.press, ACTS),
            "browser_get_images": (pages.images, ToolTraits()),
            "browser_console": (pages.console, ToolTraits()),
        }
        for name, (handler, traits) in handlers.items():
            registry.register(
                name,
                TOOLSET,
                SCHEMAS[name],
                handler,
                probe=self.ready,
                max_result_chars=MAX_RESULT_CHARS,
                traits=traits,
            )
        registry.register(
            "browser_vision",
            TOOLSET,
            VISION_SCHEMA,
            self.vision.__call__,
            probe=self.ready,
            is_async=True,
            max_result_chars=MAX_RESULT_CHARS,
        )
        registry.register(
            "browser_cdp",
            TOOLSET,
            cdp_mod.CDP_SCHEMA,
            self.cdp,
            probe=self.cdp_ready,
            max_result_chars=MAX_RESULT_CHARS,
            traits=ACTS,
        )
        registry.register(
            "browser_dialog",
            TOOLSET,
            cdp_mod.DIALOG_SCHEMA,
            self.dialog,
            probe=self.ready,
            traits=ACTS,
        )


DEFAULT_TOOLS = BrowserTools()


def register_tools(registry: ToolRegistry) -> None:
    DEFAULT_TOOLS.register(registry)
    # P24-39 / P24-40: the scripted CLI runner (offered when the CLI is found) and the vault
    # autofill (offered when a vault source is configured), over the same session manager.
    from indus.tools.builtin.browser.exec_cli import register_exec_tool
    from indus.tools.builtin.browser.vault import register_vault_tool

    register_exec_tool(registry)
    register_vault_tool(registry, DEFAULT_TOOLS.manager)
