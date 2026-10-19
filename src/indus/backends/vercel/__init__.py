# pyright: strict
"""The Vercel Sandbox backend (P21-08, P21-09).

The sandbox SDK is an optional extra and is imported only by ``load_sdk``, when a sandbox is
actually opened; importing this package never imports it.
"""

from __future__ import annotations

import importlib
from typing import Any

from indus.backends.contract import BackendUnavailable

__all__ = ["EXTRA", "SDK_MODULE", "load_sdk"]

EXTRA = "vercel"
SDK_MODULE = "vercel.sandbox.sync"


def load_sdk() -> Any:
    """The synchronous sandbox SDK module, or ``BackendUnavailable`` naming the extra."""
    try:
        return importlib.import_module(SDK_MODULE)
    except ImportError as exc:
        raise BackendUnavailable(
            f"the vercel_sandbox backend needs the Vercel Sandbox SDK, and indus was installed "
            f"without the `{EXTRA}` extra (install indus-agent[{EXTRA}])"
        ) from exc
