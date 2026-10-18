"""``@aio``: run an ``async def`` test on a fresh event loop (no async pytest plugin here)."""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable, Coroutine
from typing import Any


def aio[**P](fn: Callable[P, Coroutine[Any, Any, None]]) -> Callable[P, None]:
    @functools.wraps(fn)
    def run(*args: P.args, **kwargs: P.kwargs) -> None:
        asyncio.run(fn(*args, **kwargs))

    return run
