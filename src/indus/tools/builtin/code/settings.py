# pyright: strict
"""Limits and settings of ``execute_code``, read from the call's scope on every call.

The numbers the plan fixes live here as constants; the ones a user may change are read from the
``code_execution`` config section (defaults in ``foundation.config.schema``). A value that is not a
positive whole number falls back to its default with a warning, so a typo never removes a limit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from indus.foundation.config.schema import default_for
from indus.foundation.scope import Scope

log = logging.getLogger(__name__)

TOOL_NAME = "execute_code"
TOOLSET = "code_execution"
MODULE_NAME = "indus_tools"

STDOUT_INLINE_BYTES = 50_000
STDOUT_HEAD_SHARE = 0.4  # the rest of the inline budget goes to the tail
STDERR_BYTES = 10_000
SPILL_MAX_BYTES = 5_000_000
CELL_CAPTURE_BYTES = 1_000_000
KILL_GRACE_S = 5.0
REMOTE_POLL_S = 0.5
RPC_CALL_WAIT_S = 300.0  # how long the in-script stub waits for one answer
TOOL_ERRORS_SHOWN = 5
MAX_RESULT_CHARS = 100_000

type Mode = Literal["project", "strict"]
type KernelSetting = Literal["auto", "off"]

MODES: tuple[Mode, ...] = ("project", "strict")


@dataclass(frozen=True)
class CodeSettings:
    timeout_s: float = 300.0
    max_tool_calls: int = 50
    mode: Mode = "project"
    kernel: KernelSetting = "auto"
    kernel_idle_s: float = 1800.0
    max_kernels: int = 4

    @classmethod
    def from_scope(cls, scope: Scope | None) -> CodeSettings:
        return cls(
            timeout_s=float(_positive_int(scope, "timeout")),
            max_tool_calls=_positive_int(scope, "max_tool_calls"),
            mode=_mode(scope),
            kernel="off" if _text(scope, "kernel") == "off" else "auto",
            kernel_idle_s=float(_positive_int(scope, "kernel_idle_timeout")),
            max_kernels=_positive_int(scope, "max_session_kernels"),
        )


def _raw(scope: Scope | None, name: str) -> object:
    key = f"code_execution.{name}"
    value: object = None
    if scope is not None and scope.config is not None:
        try:
            value = scope.config.get(key)
        except Exception:
            log.debug("could not read %s", key, exc_info=True)
    return default_for(key) if value is None else value


def _text(scope: Scope | None, name: str) -> str:
    return str(_raw(scope, name)).strip().lower()


def _positive_int(scope: Scope | None, name: str) -> int:
    raw = _raw(scope, name)
    fallback = int(default_for(f"code_execution.{name}"))
    if isinstance(raw, bool):
        return fallback
    try:
        value = int(raw)  # pyright: ignore[reportArgumentType]
    except (TypeError, ValueError):
        log.warning("code_execution.%s=%r is not a whole number; using %d", name, raw, fallback)
        return fallback
    if value < 1:
        log.warning("code_execution.%s=%r must be at least 1; using %d", name, raw, fallback)
        return fallback
    return value


def _mode(scope: Scope | None) -> Mode:
    value = _text(scope, "mode")
    if value == "strict":
        return "strict"
    if value != "project":
        log.warning("code_execution.mode=%r is not project or strict; using project", value)
    return "project"
