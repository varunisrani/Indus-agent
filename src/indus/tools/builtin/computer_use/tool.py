"""The ``computer_use`` tool: look at and operate desktop apps through a ``DesktopDriver``.

One tool, one ``action`` per call; each action maps to one driver method. The order of a call:

1. check the arguments (unknown action, missing text / keys / target → error, nothing asked);
2. hard refusals that no approval can lift: system shortcuts that log out, lock the screen or
   delete without the bin, and typed text our command detector rates hardline;
3. approval for every action except ``capture``, ``list_apps`` and ``wait``, with the P05
   building blocks: auto-approve or ``approvals.mode: off`` → go; nobody to ask (cron, one-shot,
   unattended, no broker) → that run's ``approvals.*_mode`` (deny by default); approved for this
   session already (pattern ``computer_use:<action>``) → go; ``smart`` with a judge → the judge may
   approve; otherwise the person is asked (once / session / deny). A refusal never reaches the
   driver, which is not even started;
4. the driver for this (profile, session) is started on first use and reused; one that crashes or
   hangs is dropped after a bounded error and the next call starts a fresh one.

Results are JSON text. An image capture goes back as the multimodal envelope when the main route
takes images in tool results (host extra ``native_vision``); otherwise the auxiliary ``vision`` task
describes the screenshot, and failing that the element list comes back alone. The element list is
capped (``MAX_ELEMENTS`` entries, labels ``MAX_LABEL_CHARS``); when cut, the whole list is written
to a private file. Screenshots are kept privately under ``<home>/cache/computer_use/`` (newest
``KEEP_FILES``) so one can be shown to the user as ``MEDIA:<path>``.

Host seams: ``DEFAULT_TOOL.release(session_id, scope)`` when a session closes and
``await DEFAULT_TOOL.shutdown()`` at exit stop the session's driver program.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import re
import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from indus.foundation.fsio import atomic_write_bytes
from indus.foundation.home import secure_dir
from indus.foundation.scope import Scope
from indus.protocol.requests_port import ServerRequestPort
from indus.protocol.server_requests import ApprovalChoice
from indus.safety.approvals import ApprovalContext, Detection, detect, session_key
from indus.safety.approvals.decision import RunContext, approval_mode, setting
from indus.safety.approvals.human import ask_human
from indus.safety.approvals.policy import auto_approve_active
from indus.safety.approvals.smart import ask_judge
from indus.safety.redaction import redact_output
from indus.tools.builtin.computer_use import driver as drv
from indus.tools.builtin.media import imaging
from indus.tools.registry import ToolRegistry, ToolTraits, scope_key

__all__ = [
    "ACTIONS",
    "DEFAULT_TOOL",
    "PATTERN_PREFIX",
    "READ_ONLY_ACTIONS",
    "SCHEMA",
    "TOOLSET",
    "TOOL_NAME",
    "ComputerUseTool",
    "blocked_combo",
]

log = logging.getLogger(__name__)

TOOL_NAME = "computer_use"
TOOLSET = "computer_use"
PATTERN_PREFIX = "computer_use:"
MAX_ELEMENTS = 100
MAX_LABEL_CHARS = 120
MAX_SUMMARY_ELEMENTS = 40
MAX_TREE_CHARS = 60_000
MAX_RESULT_CHARS = 100_000
KEEP_FILES = 20
MIN_IMAGE_SIDE = 8  # providers refuse smaller images
CACHE_DIR = Path("cache") / "computer_use"
VISION_TASK = "vision"
VISION_MAX_TOKENS = 1_200

# Looking first, then the pointer, the keyboard and the app that gets input.
ACTIONS: tuple[str, ...] = (
    "capture", "list_apps", "wait",
    "click", "double_click", "drag", "scroll",
    "type", "key", "set_value", "focus_app",
)  # fmt: skip
# Names models reach for that are not actions: named in the error, never run in their place.
_NEAREST: dict[str, str] = {
    "screenshot": "capture", "look": "capture", "snapshot": "capture",
    "left_click": "click", "right_click": "click with button='right'", "tap": "click",
    "middle_click": "click with button='middle'", "type_text": "type", "write": "type",
    "press": "key", "hotkey": "key", "shortcut": "key", "keypress": "key",
    "select": "set_value", "fill": "set_value", "sleep": "wait", "pause": "wait",
    "open_app": "focus_app", "switch_app": "focus_app", "apps": "list_apps",
}  # fmt: skip
READ_ONLY_ACTIONS: frozenset[str] = frozenset({"capture", "list_apps", "wait"})
BUTTONS = ("left", "right", "middle")
DIRECTIONS = ("up", "down", "left", "right")

_POLICY_KEYS: dict[str, str] = {
    "cron": "approvals.cron_mode",
    "single_query": "approvals.single_query_mode",
    "unattended": "approvals.unattended_mode",
}
_RUN_CONTEXTS = frozenset({"interactive", "cron", "single_query", "unattended"})

_VISION_PROMPT = (
    "An agent is operating a desktop application and took this screenshot. Describe what it "
    "shows that matters for working the app: the app and window, the layout, labelled buttons, "
    "menus and fields, dialogs, and any text the user would need. Report only what is visible. "
    "The numbered controls the agent can act on are listed below.\n\n"
)


# -- hard refusals --------------------------------------------------------------------------------

_KEY_NAMES: dict[str, str] = {
    "command": "cmd", "⌘": "cmd", "control": "ctrl", "ctl": "ctrl", "^": "ctrl",
    "option": "alt", "opt": "alt", "⌥": "alt", "windows": "win", "super": "win", "meta": "win",
    "del": "delete", "bksp": "backspace",
}  # fmt: skip
# Shortcuts that end or lock the person's session, or delete without the bin. Never sent.
_BLOCKED_COMBOS: tuple[frozenset[str], ...] = tuple(
    frozenset(combo)
    for combo in (
        ("cmd", "shift", "q"),  # log out
        ("cmd", "alt", "shift", "q"),  # log out without asking
        ("cmd", "ctrl", "q"),  # lock the screen
        ("cmd", "shift", "backspace"),  # empty the bin
        ("cmd", "shift", "delete"),
        ("cmd", "alt", "backspace"),  # delete at once, bypassing the bin
        ("cmd", "alt", "delete"),
        ("win", "l"),  # lock the screen
        ("ctrl", "alt", "delete"),
        ("ctrl", "alt", "backspace"),  # kills the X session on some setups
        ("alt", "f4"),  # closes the focused window (or offers shutdown on the desktop)
        ("shift", "delete"),  # delete without the bin
    )
)


def _combo_keys(combo: str) -> frozenset[str]:
    parts = re.split(r"\s*[+\-]\s*|\s+", combo.strip().lower())
    return frozenset(_KEY_NAMES.get(p, p) for p in parts if p)


def blocked_combo(combo: str) -> frozenset[str] | None:
    """The refused shortcut contained in ``combo``, or None."""
    keys = _combo_keys(combo)
    for blocked in _BLOCKED_COMBOS:
        if blocked <= keys:
            return blocked
    return None


# -- result helpers -------------------------------------------------------------------------------


def _json(body: Mapping[str, Any]) -> str:
    return json.dumps(body, ensure_ascii=False, default=str)


def _error(message: str, action: str | None = None, **extra: Any) -> str:
    body: dict[str, Any] = {"error": message}
    if action:
        body["action"] = action
    body.update(extra)
    return _json(body)


def _int_arg(args: Mapping[str, Any], name: str) -> int | None:
    value = args.get(name)
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _text_arg(args: Mapping[str, Any], name: str) -> str:
    value = args.get(name)
    return value if isinstance(value, str) else ""


def _target(args: Mapping[str, Any], prefix: str = "") -> drv.Target | None:
    element = _int_arg(args, f"{prefix}element")
    x, y = _int_arg(args, f"{prefix}x"), _int_arg(args, f"{prefix}y")
    if element is None and (x is None or y is None):
        return None
    return drv.Target(element, x if y is not None else None, y if x is not None else None)


def _where(target: drv.Target | None) -> str:
    if target is None:
        return ""
    if target.element is not None:
        return f" control [{target.element}]"
    return f" at ({target.x}, {target.y})"


def _button(args: Mapping[str, Any]) -> str:
    value = _text_arg(args, "button").strip().lower()
    return value if value in BUTTONS else "left"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# -- the action table -----------------------------------------------------------------------------

Runner = Callable[[drv.DesktopDriver, Mapping[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class ActionSpec:
    run: Runner
    describe: Callable[[Mapping[str, Any]], str]
    check: Callable[[Mapping[str, Any]], str | None] = lambda args: None


def _need_target(args: Mapping[str, Any]) -> str | None:
    if _target(args) is None:
        return "give `element` (a number from the last capture) or both `x` and `y`"
    return None


def _check_drag(args: Mapping[str, Any]) -> str | None:
    if _target(args) is None:
        return "drag needs a start: `element`, or `x` and `y`"
    if _target(args, "to_") is None:
        return "drag needs an end: `to_element`, or `to_x` and `to_y`"
    return None


def _check_scroll(args: Mapping[str, Any]) -> str | None:
    direction = _text_arg(args, "direction").strip().lower()
    if direction and direction not in DIRECTIONS:
        return f"direction must be one of {', '.join(DIRECTIONS)}"
    return None


def _check_text(name: str, action: str) -> Callable[[Mapping[str, Any]], str | None]:
    def check(args: Mapping[str, Any]) -> str | None:
        return None if _text_arg(args, name) else f"{action} needs `{name}`"

    return check


def _check_set_value(args: Mapping[str, Any]) -> str | None:
    if _int_arg(args, "element") is None:
        return "set_value needs `element` (a number from the last capture)"
    if args.get("value") is None:
        return "set_value needs `value`"
    return None


def _check_capture(args: Mapping[str, Any]) -> str | None:
    mode = _text_arg(args, "mode").strip().lower() or "som"
    return None if mode in drv.CAPTURE_MODES else "mode must be som, vision or ax"


def _amount(args: Mapping[str, Any]) -> int:
    value = _int_arg(args, "amount")
    return max(1, min(value, 50)) if value is not None else 3


async def _run_capture(driver: drv.DesktopDriver, args: Mapping[str, Any]) -> drv.Capture:
    mode = cast(drv.CaptureMode, _text_arg(args, "mode").strip().lower() or "som")
    return await driver.capture(mode, app=_text_arg(args, "app").strip() or None)


async def _run_click(driver: drv.DesktopDriver, args: Mapping[str, Any]) -> drv.Outcome:
    return await driver.click(cast(drv.Target, _target(args)), button=_button(args))


async def _run_double(driver: drv.DesktopDriver, args: Mapping[str, Any]) -> drv.Outcome:
    return await driver.double_click(cast(drv.Target, _target(args)), button=_button(args))


async def _run_drag(driver: drv.DesktopDriver, args: Mapping[str, Any]) -> drv.Outcome:
    start, end = cast(drv.Target, _target(args)), cast(drv.Target, _target(args, "to_"))
    return await driver.drag(start, end, button=_button(args))


async def _run_scroll(driver: drv.DesktopDriver, args: Mapping[str, Any]) -> drv.Outcome:
    direction = _text_arg(args, "direction").strip().lower() or "down"
    target = _target(args) or drv.Target()
    return await driver.scroll(target, direction=direction, amount=_amount(args))


async def _run_type(driver: drv.DesktopDriver, args: Mapping[str, Any]) -> drv.Outcome:
    return await driver.type_text(_text_arg(args, "text"))


async def _run_key(driver: drv.DesktopDriver, args: Mapping[str, Any]) -> drv.Outcome:
    return await driver.key(_text_arg(args, "keys").strip())


async def _run_set_value(driver: drv.DesktopDriver, args: Mapping[str, Any]) -> drv.Outcome:
    return await driver.set_value(cast(int, _int_arg(args, "element")), str(args.get("value")))


async def _run_wait(driver: drv.DesktopDriver, args: Mapping[str, Any]) -> drv.Outcome:
    ms = _int_arg(args, "ms")
    return await driver.wait(1000 if ms is None else max(0, min(ms, drv.MAX_WAIT_MS)))


async def _run_list_apps(driver: drv.DesktopDriver, args: Mapping[str, Any]) -> list[Any]:
    return await driver.list_apps()


async def _run_focus(driver: drv.DesktopDriver, args: Mapping[str, Any]) -> drv.Outcome:
    return await driver.focus_app(_text_arg(args, "app").strip())


def _describe_typing(args: Mapping[str, Any]) -> str:
    return f"type {_clip(_text_arg(args, 'text'), 80)!r}"


SPECS: dict[str, ActionSpec] = {
    "capture": ActionSpec(_run_capture, lambda a: "capture the screen", _check_capture),
    "click": ActionSpec(
        _run_click, lambda a: f"{_button(a)} click{_where(_target(a))}", _need_target
    ),
    "double_click": ActionSpec(
        _run_double, lambda a: f"{_button(a)} double-click{_where(_target(a))}", _need_target
    ),
    "drag": ActionSpec(
        _run_drag,
        lambda a: f"drag from{_where(_target(a))} to{_where(_target(a, 'to_'))}",
        _check_drag,
    ),
    "scroll": ActionSpec(
        _run_scroll,
        lambda a: (
            f"scroll {_text_arg(a, 'direction') or 'down'} by {_amount(a)}{_where(_target(a))}"
        ),
        _check_scroll,
    ),
    "type": ActionSpec(_run_type, _describe_typing, _check_text("text", "type")),
    "key": ActionSpec(
        _run_key, lambda a: f"press {_text_arg(a, 'keys').strip()!r}", _check_text("keys", "key")
    ),
    "set_value": ActionSpec(
        _run_set_value,
        lambda a: f"set control [{_int_arg(a, 'element')}] to {_clip(str(a.get('value')), 80)!r}",
        _check_set_value,
    ),
    "wait": ActionSpec(_run_wait, lambda a: "wait"),
    "list_apps": ActionSpec(_run_list_apps, lambda a: "list running apps"),
    "focus_app": ActionSpec(
        _run_focus,
        lambda a: f"send input to {_text_arg(a, 'app').strip()!r}",
        _check_text("app", "focus_app"),
    ),
}


# -- the tool -------------------------------------------------------------------------------------


@dataclass
class _Slot:
    driver: drv.DesktopDriver
    loop: asyncio.AbstractEventLoop
    lock: asyncio.Lock


class ComputerUseTool:
    """The tool's shared state in one process: one driver per (profile, session)."""

    def __init__(self) -> None:
        self._slots: dict[tuple[str, str], _Slot] = {}

    # -- availability -----------------------------------------------------------------------------

    def ready(self, scope: Scope) -> bool:
        provider = drv.provider_for(scope)
        return provider is not None and provider.ready(scope).ready

    # -- drivers ----------------------------------------------------------------------------------

    async def _driver(self, scope: Scope, session_id: str | None) -> _Slot:
        key = (scope_key(scope), session_id or "")
        loop = asyncio.get_running_loop()
        slot = self._slots.get(key)
        if slot is not None and slot.loop is not loop:
            self._forget(key)  # started on a loop that is gone; it cannot be awaited here
            slot = None
        if slot is None:
            provider = drv.provider_for(scope)
            if provider is None:
                raise drv.DriverError(
                    f"no desktop driver named {drv.selected_name(scope)!r} is installed"
                )
            created = provider.create(scope, session_id)
            slot = _Slot(created, loop, asyncio.Lock())
            self._slots[key] = slot
            try:
                await created.start()
            except BaseException:
                self._slots.pop(key, None)
                with contextlib.suppress(Exception):
                    await created.close()
                raise
        return slot

    def _forget(self, key: tuple[str, str]) -> None:
        slot = self._slots.pop(key, None)
        if slot is not None and isinstance(slot.driver, drv.StdioDriver):
            slot.driver.kill_now()

    async def _drop(self, key: tuple[str, str], slot: _Slot) -> None:
        if self._slots.get(key) is slot:
            del self._slots[key]
        with contextlib.suppress(Exception):
            await slot.driver.close()

    async def release(self, session_id: str | None, scope: Scope | None = None) -> bool:
        """Stop the driver of one session (every profile's, without ``scope``)."""
        keys = [
            k
            for k in self._slots
            if k[1] == (session_id or "") and (scope is None or k[0] == scope_key(scope))
        ]
        for key in keys:
            slot = self._slots.get(key)
            if slot is not None:
                await self._drop(key, slot)
        return bool(keys)

    async def shutdown(self) -> None:
        for key, slot in list(self._slots.items()):
            if slot.loop is asyncio.get_running_loop():
                await self._drop(key, slot)
            else:
                self._forget(key)

    def driver_count(self) -> int:
        return len(self._slots)

    # -- the handler ------------------------------------------------------------------------------

    async def handle(
        self,
        args: Mapping[str, Any],
        *,
        scope: Scope | None = None,
        session_id: str | None = None,
        turn_id: str | None = None,
        broker: ServerRequestPort | None = None,
        run_context: str | None = None,
        approval_judge: Any = None,
        native_vision: bool = False,
        aux: Any = None,
    ) -> str | dict[str, Any]:
        action = _text_arg(args, "action").strip().lower()
        if not action:
            return _error("`action` is required: one of " + ", ".join(ACTIONS))
        spec = SPECS.get(action)
        if spec is None:
            hint = _NEAREST.get(action)
            near = f" (did you mean {hint}?)" if hint else ""
            return _error(f"unknown action {action!r}{near}; use one of " + ", ".join(ACTIONS))
        if scope is None:
            return _error("desktop control needs a profile scope", action)
        problem = spec.check(args)
        if problem is not None:
            return _error(problem, action)
        refused = self._refusal(action, args)
        if refused is not None:
            return refused
        if action not in READ_ONLY_ACTIONS:
            denied = await self._approve(
                action,
                args,
                spec,
                scope=scope,
                session_id=session_id,
                turn_id=turn_id,
                broker=broker,
                run_context=run_context,
                judge=approval_judge,
            )
            if denied is not None:
                return denied
        return await self._run(action, args, spec, scope, session_id, native_vision, aux)

    @staticmethod
    def _refusal(action: str, args: Mapping[str, Any]) -> str | None:
        if action == "key":
            blocked = blocked_combo(_text_arg(args, "keys"))
            if blocked is not None:
                return _error(
                    f"refused: {'+'.join(sorted(blocked))} would end, lock or delete outside the "
                    "user's control; this shortcut is never sent",
                    action,
                )
        if action in ("type", "set_value"):
            text = _text_arg(args, "text") if action == "type" else str(args.get("value"))
            found = detect(text)
            if found.hardline:
                return _error(
                    f"refused: the text is a catastrophic shell command ({found.description}); "
                    "it is never typed",
                    action,
                )
        return None

    async def _approve(
        self,
        action: str,
        args: Mapping[str, Any],
        spec: ActionSpec,
        *,
        scope: Scope,
        session_id: str | None,
        turn_id: str | None,
        broker: ServerRequestPort | None,
        run_context: str | None,
        judge: Any,
    ) -> str | None:
        """None when the action may run; otherwise the error text for the model."""
        try:
            return await self._decide(
                action, args, spec, scope, session_id, turn_id, broker, run_context, judge
            )
        except Exception as exc:
            log.error("computer_use approval could not run: %s: %s", type(exc).__name__, exc)
            return _error("blocked: the approval check could not run, so nothing was done", action)

    async def _decide(
        self,
        action: str,
        args: Mapping[str, Any],
        spec: ActionSpec,
        scope: Scope,
        session_id: str | None,
        turn_id: str | None,
        broker: ServerRequestPort | None,
        run_context: str | None,
        judge: Any,
    ) -> str | None:
        kind = cast(RunContext, run_context if run_context in _RUN_CONTEXTS else "interactive")
        ctx = ApprovalContext(
            scope=scope,
            session_id=session_id,
            turn_id=turn_id,
            run_context=kind,
            broker=broker,
            judge=judge,
        )
        mode = approval_mode(scope)
        if auto_approve_active(ctx, scope) or mode == "off":
            return None
        if kind != "interactive" or broker is None:
            key = _POLICY_KEYS["unattended" if kind == "interactive" else kind]
            if str(setting(scope, key)).strip().lower() == "approve":
                return None
            return _error(
                f"blocked: computer_use {action} needs the user's approval and nobody here can "
                f"give it, so nothing was done. The user can set {key}: approve to allow it in "
                "this kind of run.",
                action,
            )
        pattern = PATTERN_PREFIX + action
        if ctx.approvals.pattern_approved(session_key(scope, session_id), pattern):
            return None
        shown = f"computer_use: {spec.describe(args)}"
        description = "desktop control: this changes what is on the user's screen"
        if action == "type":
            found = detect(_text_arg(args, "text"))
            if found.dangerous and found.description:
                description += f"; the text reads like a risky command ({found.description})"
        if mode == "smart" and judge is not None:
            verdict = await ask_judge(judge, shown, description, scope=scope)
            if verdict == "approve":
                return None
        decision = await ask_human(
            shown,
            Detection(True, False, pattern, description),
            ctx,
            scope=scope,
            mode=mode,
            choices=(ApprovalChoice.ONCE, ApprovalChoice.SESSION),
        )
        if decision.approved:
            return None
        message = decision.message or "blocked: the action was not approved, so nothing was done"
        return _error(redact_output(message, force=True, scope=scope), action)

    async def _run(
        self,
        action: str,
        args: Mapping[str, Any],
        spec: ActionSpec,
        scope: Scope,
        session_id: str | None,
        native_vision: bool,
        aux: Any,
    ) -> str | dict[str, Any]:
        key = (scope_key(scope), session_id or "")
        try:
            slot = await self._driver(scope, session_id)
        except drv.DriverError as exc:
            return _error(f"the desktop driver could not start: {exc}", action)
        try:
            async with slot.lock:
                result = await spec.run(slot.driver, args)
        except drv.DriverCrashed as exc:
            await self._drop(key, slot)
            return _error(
                f"the desktop driver stopped: {exc}. Nothing more is known about whether the "
                "action happened; capture again before retrying. The driver is started afresh "
                "on the next call.",
                action,
            )
        except drv.DriverError as exc:
            return _error(redact_output(str(exc), force=True, scope=scope), action)
        except Exception as exc:
            await self._drop(key, slot)
            log.warning("computer_use %s failed", action, exc_info=True)
            return _error(f"{action} failed: {type(exc).__name__}: {exc}", action)
        if isinstance(result, drv.Capture):
            return await self._capture_result(result, scope, native_vision, aux)
        if isinstance(result, drv.Outcome):
            return self._outcome_result(result)
        apps = cast(list[Any], result)
        return _json({"ok": True, "action": action, "apps": apps, "count": len(apps)})

    @staticmethod
    def _outcome_result(outcome: drv.Outcome) -> str:
        if not outcome.ok:
            body: dict[str, Any] = {
                "error": outcome.message or f"{outcome.action} did not succeed",
                "action": outcome.action,
                "ok": False,
            }
            body.update({k: v for k, v in outcome.data.items() if k not in body})
            return _json(body)
        body = {"ok": True, "action": outcome.action}
        if outcome.message:
            body["message"] = outcome.message
        body.update({k: v for k, v in outcome.data.items() if k not in body})
        return _json(body)

    # -- captures ---------------------------------------------------------------------------------

    async def _capture_result(
        self, cap: drv.Capture, scope: Scope, native_vision: bool, aux: Any
    ) -> str | dict[str, Any]:
        shown = cap.elements[:MAX_ELEMENTS]
        cut = len(cap.elements) - len(shown)
        long_labels = any(len(e.label) > MAX_LABEL_CHARS for e in shown)
        folder = _cache_dir(scope)
        elements_file = _spill(folder, cap) if (cut or long_labels) and folder else None
        image, omitted = _screen_image(cap)
        screenshot = _save_image(folder, image) if image is not None and folder else None
        note = " ".join(n for n in (cap.note, omitted) if n)
        summary = "\n".join(_summary_lines(cap, shown, cut, elements_file, screenshot, note))
        if image is not None and native_vision:
            return {
                "multimodal": True,
                "content": [
                    {"type": "text", "text": summary},
                    {"type": "image_url", "image_url": {"url": image.data_url}},
                ],
                "text_summary": summary,
            }
        body: dict[str, Any] = {
            "ok": True,
            "action": "capture",
            "mode": cap.mode,
            "width": cap.width,
            "height": cap.height,
            "app": cap.app,
            "window": cap.window,
            "elements": [_element_json(e) for e in shown],
            "total_elements": len(cap.elements),
        }
        if cap.mode == "ax" and cap.tree:
            body["tree"] = _clip(cap.tree, MAX_TREE_CHARS)
        if cut:
            body["truncated_elements"] = cut
        if elements_file:
            body["elements_file"] = elements_file
        if screenshot:
            body["screenshot_path"] = screenshot
        if image is not None:
            analysis = await _describe(aux, image, summary) if aux is not None else None
            if analysis:
                body["vision_analysis"] = redact_output(analysis, force=True, scope=scope)
            else:
                note = (note + " " if note else "") + (
                    "The screenshot could not be looked at here; act on the numbered controls."
                )
        if note:
            body["note"] = note
        body["summary"] = summary
        return _json(body)


@dataclass(frozen=True)
class _Image:
    data: bytes
    mime: str
    b64: str

    @property
    def data_url(self) -> str:
        return f"data:{self.mime};base64,{self.b64}"


def _screen_image(cap: drv.Capture) -> tuple[_Image | None, str]:
    """The capture's screenshot when it can be sent, else None and why it was left out."""
    if cap.image_b64 is None or cap.mode == "ax":
        return None, ""
    try:
        data = base64.b64decode(cap.image_b64, validate=False)
        info = imaging.inspect(data)
    except (ValueError, imaging.BadImage) as exc:
        return None, f"The driver's screenshot was left out: {exc}."
    if min(info.width, info.height) < MIN_IMAGE_SIDE:
        return None, (
            f"The screenshot ({info.width}x{info.height}) was left out: models do not take "
            f"images smaller than {MIN_IMAGE_SIDE}x{MIN_IMAGE_SIDE}."
        )
    b64 = base64.b64encode(data).decode("ascii")
    return _Image(data, info.mime, b64), ""


def _element_json(e: drv.Element) -> dict[str, Any]:
    out: dict[str, Any] = {
        "index": e.index,
        "role": e.role,
        "label": _clip(e.label, MAX_LABEL_CHARS),
        "bounds": list(e.bounds) if e.bounds is not None else None,
    }
    if e.app:
        out["app"] = e.app
    return out


def _summary_lines(
    cap: drv.Capture,
    shown: tuple[drv.Element, ...],
    cut: int,
    elements_file: str | None,
    screenshot: str | None,
    note: str,
) -> list[str]:
    head = f"capture {cap.mode} {cap.width}x{cap.height}"
    if cap.app:
        head += f" app={cap.app}"
    if cap.window:
        head += f" window={cap.window!r}"
    lines = [head, f"{len(cap.elements)} control(s):"]
    for e in shown[:MAX_SUMMARY_ELEMENTS]:
        where = f"at {list(e.bounds)}" if e.bounds is not None else "(position unknown)"
        label = _clip(e.label.replace("\n", " "), 60)
        lines.append(f"  [{e.index}] {e.role} {label!r} {where}")
    rest = len(cap.elements) - min(len(shown), MAX_SUMMARY_ELEMENTS)
    if rest > 0:
        lines.append(f"  … {rest} more (capture with app= to narrow)")
    if cut and elements_file:
        lines.append(f"  (the full list is in {elements_file})")
    if screenshot:
        lines.append(f"  (screenshot saved at {screenshot}; share it as MEDIA:{screenshot})")
    if note:
        lines.append(f"  ({note})")
    return lines


def _cache_dir(scope: Scope) -> Path | None:
    try:
        return secure_dir(scope.home / CACHE_DIR, home=scope.home, env=scope.env)
    except OSError:
        log.debug("computer_use cache folder unavailable", exc_info=True)
        return None


def _prune(folder: Path, pattern: str) -> None:
    with contextlib.suppress(OSError):
        files = sorted(folder.glob(pattern), key=lambda p: p.stat().st_mtime)
        for old in files[: max(0, len(files) - (KEEP_FILES - 1))]:
            old.unlink(missing_ok=True)


def _save_image(folder: Path, image: _Image) -> str | None:
    suffix = "." + image.mime.split("/")[-1].replace("jpeg", "jpg")
    try:
        _prune(folder, "screen-*")
        path = folder / f"screen-{secrets.token_hex(6)}{suffix}"
        atomic_write_bytes(path, image.data, mode=0o600)
    except OSError:
        log.debug("screenshot not saved", exc_info=True)
        return None
    return str(path)


def _spill(folder: Path, cap: drv.Capture) -> str | None:
    payload = {
        "app": cap.app,
        "window": cap.window,
        "total_elements": len(cap.elements),
        "elements": [
            {
                "index": e.index,
                "role": e.role,
                "label": e.label,
                "bounds": list(e.bounds) if e.bounds is not None else None,
                "app": e.app,
            }
            for e in cap.elements
        ],
    }
    try:
        _prune(folder, "elements-*")
        path = folder / f"elements-{secrets.token_hex(6)}.json"
        atomic_write_bytes(path, json.dumps(payload, ensure_ascii=False).encode(), mode=0o600)
    except OSError:
        log.debug("element list not saved", exc_info=True)
        return None
    return str(path)


async def _describe(aux: Any, image: _Image, summary: str) -> str | None:
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": _VISION_PROMPT + summary},
                {"type": "image_url", "image_url": {"url": image.data_url}},
            ],
        }
    ]
    try:
        result = await aux.call(VISION_TASK, messages, max_tokens=VISION_MAX_TOKENS)
    except Exception as exc:
        log.info("computer_use: the vision side task failed: %s", exc)
        return None
    text = getattr(result, "text", result)
    return (text.strip() or None) if isinstance(text, str) else None


# -- schema ---------------------------------------------------------------------------------------


def _int_prop(description: str) -> dict[str, Any]:
    return {"type": "integer", "description": description}


def _choice(values: tuple[str, ...], description: str) -> dict[str, Any]:
    return {"description": description, "type": "string", "enum": list(values)}


SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Look at and operate desktop applications on this computer through a background desktop "
        "driver: the user's pointer and keyboard focus are left alone. Start with "
        "action='capture' (mode 'som' numbers every control on a screenshot), then act on a "
        "control by its number with `element`; use pixel `x`/`y` only when no number fits. "
        "Capture again after anything that changes the screen, and check the result before "
        "repeating an action. Every action except capture, list_apps and wait asks the user "
        "first. Screenshots are saved; share one as MEDIA:<path> only when the user wants to "
        "see it."
    ),
    "parameters": {
        "type": "object",
        "required": ["action"],
        "properties": {
            "action": _choice(
                ACTIONS,
                "What to do. capture, list_apps and wait only look or pause; the others "
                "change the screen and need approval. set_value fills a field or picks a "
                "list option directly, without opening menus.",
            ),
            "mode": _choice(
                tuple(drv.CAPTURE_MODES),
                "capture only. som (default): screenshot with numbered marks plus the list of "
                "controls. vision: the screenshot alone. ax: the accessibility tree as text, no "
                "image (for models without vision).",
            ),
            "app": {
                "type": "string",
                "description": (
                    "capture: the app whose window to look at (name or bundle id; default the "
                    "front window). focus_app: the app that should receive the next input."
                ),
            },
            "element": _int_prop(
                "Number of a control from the most recent som capture; the preferred target."
            ),
            "x": _int_prop("Horizontal pixel in the last screenshot (use with y)."),
            "y": _int_prop("Vertical pixel in the last screenshot (use with x)."),
            "button": _choice(
                BUTTONS, "Mouse button for click, double_click and drag (default left)."
            ),
            "to_element": _int_prop("drag: number of the control to drop on."),
            "to_x": _int_prop("drag: horizontal pixel to drop at (use with to_y)."),
            "to_y": _int_prop("drag: vertical pixel to drop at (use with to_x)."),
            "direction": _choice(DIRECTIONS, "scroll: which way (default down)."),
            "amount": _int_prop("scroll: wheel steps, 1-50 (default 3)."),
            "text": {"type": "string", "description": "type: the text to enter."},
            "keys": {
                "type": "string",
                "description": "key: a key or shortcut such as 'enter', 'tab' or 'ctrl+s'.",
            },
            "value": {
                "type": "string",
                "description": "set_value: the new value, or the visible label of the option.",
            },
            "ms": _int_prop("wait: milliseconds to pause, up to 30000 (default 1000)."),
        },
    },
}


DEFAULT_TOOL = ComputerUseTool()


def register(registry: ToolRegistry, tool: ComputerUseTool | None = None) -> None:
    chosen = tool if tool is not None else DEFAULT_TOOL
    registry.register(
        TOOL_NAME,
        TOOLSET,
        SCHEMA,
        chosen.handle,
        probe=chosen.ready,
        is_async=True,
        max_result_chars=MAX_RESULT_CHARS,
        traits=ToolTraits(mutating=True),
    )
