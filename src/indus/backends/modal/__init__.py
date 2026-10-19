# pyright: strict
"""The Modal backend, direct mode only (P21-05, P21-06; D-16.16).

The Modal SDK is an optional extra and is imported only by ``load_sdk``, when a Modal sandbox is
actually opened; importing this package never imports it.
"""

from __future__ import annotations

import importlib
from typing import Any

from indus.backends.contract import BackendUnavailable

__all__ = ["EXTRA", "load_sdk"]

EXTRA = "modal"


def load_sdk() -> Any:
    """The ``modal`` module, or ``BackendUnavailable`` naming the extra (never ImportError)."""
    try:
        return importlib.import_module("modal")
    except ImportError as exc:
        raise BackendUnavailable(
            f"the modal backend needs the Modal SDK, and indus was installed without the "
            f"`{EXTRA}` extra (install indus-agent[{EXTRA}])"
        ) from exc
