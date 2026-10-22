"""Parity scenario files: loading and validation (P25-05, P25-07).

A browser scenario file *is* REA's ``capture_browser_scenario`` input; ``rea_browser_problems``
checks it against that contract as pinned in ``docs/notes/P25/rea-version.md`` (top level,
actions, locators, values, capture and environment blocks; unknown keys refused, as REA refuses
them). Terminal, stdio and platform scenarios use our own step list (``terminal_problems``).
Browser locators must rest on our own test hooks (``data-ui`` or ``data-testid``); the hooks
are resolved against the app sources, and the ones a scenario needs before the product carries
them are listed in ``OWED_HOOKS`` with a reason.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.parity.terminal_driver import key_bytes

ROOT = Path(__file__).resolve().parents[2]
SCENARIO_DIR = ROOT / "parity"
SCENARIO_SUFFIX = ".indus.json"
TERMINAL_FORMAT = "indus-terminal-scenario/1"
STEP_ID = re.compile(r"^[A-Za-z][A-Za-z0-9._-]*$")
HOOK_ATTRIBUTES = ("data-ui", "data-testid")


# -- scenario files and their validation -----------------------------------------------------------


def scenario_path(flow: str, directory: Path = SCENARIO_DIR) -> Path:
    return directory / f"{flow}{SCENARIO_SUFFIX}"


def load_scenario(flow: str, directory: Path = SCENARIO_DIR) -> dict[str, Any]:
    data = json.loads(scenario_path(flow, directory).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("a scenario file holds one JSON object")
    return data  # pyright: ignore[reportUnknownVariableType]


def is_browser_scenario(data: Mapping[str, Any]) -> bool:
    return data.get("format") is None and "start_url" in data


def steps_of(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    actions = data.get("actions")
    return [a for a in actions if isinstance(a, dict)] if isinstance(actions, list) else []  # pyright: ignore[reportUnknownVariableType]


def step_ids(data: Mapping[str, Any]) -> list[str]:
    return [str(s.get("step_id")) for s in steps_of(data)]


# REA capture_browser_scenario contract as pinned (docs/notes/P25/rea-version.md): top level,
# actions, locators, values, capture and environment blocks. Unknown keys are refused, as REA
# refuses them.
_REA_TOP = {"browser", "start_url", "actions", "capture", "environment", "secrets", "storage"}
_REA_ACTIONS: dict[str, tuple[set[str], set[str]]] = {
    # action: (required keys besides step_id/action, optional keys)
    "goto": ({"destination", "wait_until"}, {"timeout_ms"}),
    "click": ({"locator"}, {"button", "click_count", "timeout_ms"}),
    "dblclick": ({"locator"}, {"timeout_ms"}),
    "fill": ({"locator", "value"}, {"timeout_ms"}),
    "press": ({"locator", "key"}, {"timeout_ms"}),
    "select_option": ({"locator", "value"}, {"timeout_ms"}),
    "check": ({"locator"}, {"timeout_ms"}),
    "uncheck": ({"locator"}, {"timeout_ms"}),
    "wait_for": ({"locator", "state"}, {"timeout_ms"}),
    "wait_for_timeout": ({"duration_ms"}, set()),
}
_REA_ROLES = {
    "button",
    "checkbox",
    "combobox",
    "dialog",
    "link",
    "listbox",
    "menuitem",
    "option",
    "radio",
    "slider",
    "spinbutton",
    "switch",
    "tab",
    "textbox",
}
_REA_ARTIFACTS = {"screenshot", "dom", "accessibility", "url", "history", "storage"}
_REA_EVENTS = {
    "console",
    "page-errors",
    "network",
    "websockets",
    "frames",
    "workers",
    "popups",
    "downloads",
}
_REA_ENV = {
    "viewport",
    "locale",
    "timezone",
    "color_scheme",
    "reduced_motion",
    "service_workers",
}


def _keys(where: str, value: Any, required: set[str], allowed: set[str]) -> list[str]:
    if not isinstance(value, dict):
        return [f"{where}: expected an object"]
    keys = set(value)  # pyright: ignore[reportUnknownArgumentType]
    out = [f"{where}: missing {k!r}" for k in sorted(required - keys)]
    out += [f"{where}: unknown key {k!r}" for k in sorted(keys - required - allowed)]
    return out


def _value_problems(where: str, value: Any) -> list[str]:
    if not isinstance(value, dict):
        return [f"{where}: a value is {{source: literal|secret, ...}}"]
    source: Any = value.get("source")  # pyright: ignore[reportUnknownMemberType]
    if source == "literal":
        out = _keys(where, value, {"source", "value"}, set())
        if not isinstance(value.get("value"), str):  # pyright: ignore[reportUnknownMemberType]
            out.append(f"{where}: a literal value is a string")
        return out
    if source == "secret":
        out = _keys(where, value, {"source", "secret_id"}, set())
        if not STEP_ID.match(str(value.get("secret_id", ""))):  # pyright: ignore[reportUnknownMemberType]
            out.append(f"{where}: bad secret_id")
        return out
    return [f"{where}: source must be 'literal' or 'secret'"]


def _locator_problems(where: str, locator: Any) -> list[str]:
    if not isinstance(locator, dict):
        return [f"{where}: expected a locator object"]
    kind: Any = locator.get("kind")  # pyright: ignore[reportUnknownMemberType]
    if kind == "test_id":
        out = _keys(where, locator, {"kind", "value"}, set())
    elif kind == "role":
        out = _keys(where, locator, {"kind", "role", "name"}, set())
        if locator.get("role") not in _REA_ROLES:  # pyright: ignore[reportUnknownMemberType]
            out.append(f"{where}: unknown role {locator.get('role')!r}")  # pyright: ignore[reportUnknownMemberType]
    elif kind == "css":
        out = _keys(where, locator, {"kind", "selector"}, set())
    else:
        return [f"{where}: locator kind must be test_id, role or css"]
    for key, item in locator.items():  # pyright: ignore[reportUnknownVariableType]
        if key != "kind" and (not isinstance(item, str) or not item):
            out.append(f"{where}: {key} must be a non-empty string")
    return out


def _url_problems(where: str, value: Any) -> list[str]:
    out = _keys(where, value, {"url"}, {"query"})
    if out:
        return out
    if not str(value.get("url", "")).startswith(("http://", "https://")):
        out.append(f"{where}: an http(s) URL is required")
    for index, item in enumerate(value.get("query") or []):
        out += _keys(f"{where}.query[{index}]", item, {"name", "value"}, set())
        if isinstance(item, dict):
            out += _value_problems(f"{where}.query[{index}].value", item.get("value"))  # pyright: ignore[reportUnknownMemberType]
    return out


def rea_browser_problems(data: Mapping[str, Any]) -> list[str]:
    """Everything in ``data`` REA's ``capture_browser_scenario`` contract would refuse."""
    out = _keys("scenario", dict(data), {"browser", "start_url", "actions"}, _REA_TOP)
    browser: Any = data.get("browser")
    if isinstance(browser, dict):
        mode: Any = browser.get("mode")  # pyright: ignore[reportUnknownMemberType]
        if mode == "launch":
            out += _keys("browser", browser, {"mode", "executable_path"}, {"headless"})
            if not str(browser.get("executable_path", "")).startswith("/"):  # pyright: ignore[reportUnknownMemberType]
                out.append("browser: executable_path must be absolute")
        elif mode == "connect":
            out += _keys("browser", browser, {"mode", "cdp_endpoint", "target_id"}, set())
        else:
            out.append("browser: mode must be 'launch' or 'connect'")
    out += _url_problems("start_url", data.get("start_url"))
    actions: Any = data.get("actions")
    if not isinstance(actions, list) or not actions:
        out.append("actions: a non-empty list is required")
        actions = []
    for index, step in enumerate(actions):  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
        where = f"actions[{index}]"
        if not isinstance(step, dict):
            out.append(f"{where}: expected an object")
            continue
        action: Any = step.get("action")  # pyright: ignore[reportUnknownMemberType]
        if action not in _REA_ACTIONS:
            out.append(f"{where}: unknown action {action!r}")
            continue
        required, optional = _REA_ACTIONS[action]
        out += _keys(where, step, {"step_id", "action", *required}, optional)
        if not STEP_ID.match(str(step.get("step_id", ""))):  # pyright: ignore[reportUnknownMemberType]
            out.append(f"{where}: step_id must match {STEP_ID.pattern}")
        if "locator" in step:
            out += _locator_problems(f"{where}.locator", step["locator"])
        if "value" in step:
            out += _value_problems(f"{where}.value", step["value"])
        if action == "goto":
            out += _url_problems(f"{where}.destination", step.get("destination"))  # pyright: ignore[reportUnknownMemberType]
            if step.get("wait_until") not in ("commit", "domcontentloaded", "load"):  # pyright: ignore[reportUnknownMemberType]
                out.append(f"{where}: wait_until must be commit, domcontentloaded or load")
        if action == "wait_for" and step.get("state") not in (  # pyright: ignore[reportUnknownMemberType]
            "attached",
            "detached",
            "visible",
            "hidden",
        ):
            out.append(f"{where}: state must be attached, detached, visible or hidden")
        for key in ("timeout_ms", "duration_ms", "click_count"):
            if key in step and (not isinstance(step[key], int) or step[key] < 1):
                out.append(f"{where}: {key} must be a positive integer")
    capture: Any = data.get("capture")
    if capture is not None:
        out += _keys(
            "capture", capture, set(), {"after_each_step", "at_end", "events", "limits", "network"}
        )
        if isinstance(capture, dict):
            for key, allowed in (
                ("after_each_step", _REA_ARTIFACTS),
                ("at_end", _REA_ARTIFACTS),
                ("events", _REA_EVENTS),
            ):
                for item in capture.get(key) or []:  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
                    if item not in allowed:
                        out.append(f"capture.{key}: unknown entry {item!r}")
    environment: Any = data.get("environment")
    if environment is not None:
        out += _keys("environment", environment, set(), _REA_ENV)
    for index, secret in enumerate(data.get("secrets") or []):
        out += _keys(f"secrets[{index}]", secret, {"secret_id", "environment_variable"}, set())
    return out


_SCRIPT_LOCATORS = {
    "terminal": {"text", "card", "prompt", "file"},
    "stdio": {"message", "response", "file"},
    "platform": {"platform", "file"},
}
_SCRIPT_ACTIONS: dict[str, dict[str, tuple[set[str], set[str]]]] = {
    "terminal": {
        "launch": ({"argv"}, set()),
        "wait_for": ({"locator"}, set()),
        "type": ({"text"}, set()),
        "press": ({"key"}, set()),
        "wait_exit": (set(), {"code"}),
    },
    "stdio": {
        "launch": ({"argv"}, set()),
        "wait_for": ({"locator"}, set()),
        "send": ({"message"}, set()),
        "close_input": (set(), set()),
        "wait_exit": (set(), {"code"}),
    },
    "platform": {
        "launch": ({"argv"}, set()),
        "wait_for": ({"locator"}, set()),
        "inbound": ({"message"}, set()),
        "press_button": ({"button"}, set()),
        "stop": (set(), set()),
        "wait_exit": (set(), {"code"}),
    },
}


def terminal_problems(data: Mapping[str, Any]) -> list[str]:
    """Problems with one of our terminal/stdio scenario files."""
    out = _keys(
        "scenario", dict(data), {"format", "driver", "terminal", "environment", "actions"}, set()
    )
    if data.get("format") != TERMINAL_FORMAT:
        out.append(f"format must be {TERMINAL_FORMAT!r}")
    driver = str(data.get("driver"))
    if driver not in _SCRIPT_ACTIONS:
        out.append(f"driver must be one of {sorted(_SCRIPT_ACTIONS)}")
        return out
    allowed_actions = _SCRIPT_ACTIONS[driver]
    actions = steps_of(data)
    if not actions:
        out.append("actions: a non-empty list is required")
    for index, step in enumerate(actions):
        where = f"actions[{index}]"
        action = step.get("action")
        if action not in allowed_actions:
            out.append(f"{where}: unknown action {action!r} for the {driver} driver")
            continue
        required, optional = allowed_actions[str(action)]
        out += _keys(where, step, {"step_id", "action", "timeout_ms", *required}, optional)
        if not STEP_ID.match(str(step.get("step_id", ""))):
            out.append(f"{where}: step_id must match {STEP_ID.pattern}")
        if action == "launch" and (not step.get("argv") or step["argv"][0] != "indus"):
            out.append(f"{where}: argv starts with 'indus' (the product command)")
        if action == "press":
            try:
                key_bytes(str(step.get("key")))
            except ValueError as exc:
                out.append(f"{where}: {exc}")
        if action == "wait_for":
            locator = step.get("locator")
            kinds = _SCRIPT_LOCATORS[driver]
            if not isinstance(locator, dict) or locator.get("kind") not in kinds:  # pyright: ignore[reportUnknownMemberType]
                out.append(f"{where}: locator kind must be one of {sorted(kinds)}")
    return out


@dataclass(frozen=True)
class Hook:
    attribute: str
    value: str
    prefix: bool  # ``[attr^="value"]``: rows that carry an id after the prefix


def hook_of(locator: Mapping[str, Any]) -> Hook | None:
    """The test hook a browser locator rests on, or None."""
    if locator.get("kind") == "test_id":
        return Hook("data-testid", str(locator.get("value")), False)
    if locator.get("kind") == "css":
        attrs = "|".join(re.escape(a) for a in HOOK_ATTRIBUTES)
        found = re.search(rf"\[({attrs})(\^?)=\"([^\"]+)\"\]", str(locator.get("selector")))
        if found:
            return Hook(found.group(1), found.group(3), found.group(2) == "^")
    return None


# Where each browser surface's hooks live in our source.
APP_SOURCES: dict[str, tuple[str, ...]] = {
    "dashboard": ("apps/web/src",),
    "desktop": ("apps/desktop/src",),
}
# Hooks the scenarios need that the product does not carry yet, each with its reason. The scenario
# self-test fails when one of these appears in the source (drop it here then) and when a locator
# uses a hook that neither exists nor is listed.
OWED_HOOKS: dict[str, str] = {}  # W46 (RM21): the nine desktop hooks landed; none owed now


def hook_in_source(hook: Hook, roots: Sequence[Path]) -> bool:
    """Whether some source file under ``roots`` carries ``hook`` (a static value, or a template
    that starts with the prefix)."""
    static = f'{hook.attribute}="{hook.value}"'
    template = f"{hook.attribute}={{`{hook.value}"
    for root in roots:
        for path in root.rglob("*.tsx"):
            if ".test." in path.name:
                continue
            text = path.read_text(encoding="utf-8")
            if static in text or (hook.prefix and template in text):
                return True
    return False


def hook_problems(surface: str, data: Mapping[str, Any], root: Path = ROOT) -> list[str]:
    """Hooks a browser scenario uses that the source lacks and that are not listed as owed."""
    roots = [root / p for p in APP_SOURCES.get(surface, ())]
    out: list[str] = []
    for step in steps_of(data):
        locator = step.get("locator")
        hook = hook_of(locator) if isinstance(locator, dict) else None  # pyright: ignore[reportUnknownArgumentType]
        if hook is None:
            continue
        present = hook_in_source(hook, roots)
        if not present and hook.value not in OWED_HOOKS:
            out.append(
                f"{step.get('step_id')}: hook {hook.attribute}={hook.value!r} "
                f"is not in the {surface} source"
            )
        if present and hook.value in OWED_HOOKS:
            out.append(
                f"{step.get('step_id')}: hook {hook.value!r} exists now; drop it from OWED_HOOKS"
            )
    return out


def secret_problems(data: Mapping[str, Any]) -> list[str]:
    """Secret references must be declared in ``secrets`` (and declared ones must be used)."""
    declared = {str(s.get("secret_id")) for s in data.get("secrets") or [] if isinstance(s, dict)}
    used = set(re.findall(r'"secret_id":\s*"([^"]+)"', json.dumps(data.get("actions", []))))
    used |= set(re.findall(r'"secret_id":\s*"([^"]+)"', json.dumps(data.get("start_url", {}))))
    out = [f"secret {name!r} is used but not declared" for name in sorted(used - declared)]
    out += [f"secret {name!r} is declared but never used" for name in sorted(declared - used)]
    return out
