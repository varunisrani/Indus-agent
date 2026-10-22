"""Find and release ``self-chat`` holders a test left behind (wave 42 integration).

Every ``DeliveryRouter`` takes a hold on the process-wide ``self-chat`` cron deliverer
(``indus.service.self_chat.install_self_chat``) and gives it back in ``close()``. A router a test
builds and never closes keeps the deliverer registered for the rest of the run, so a later test
that expects no deliverer outside a scheduler pass fails only when such a test ran first (the
order-dependent ``test_a_cli_pass_can_deliver_to_the_own_bot_chat``: two routers in
``tests/unit/gateway/adapters/test_targets.py`` were never closed). ``tests/conftest.py`` checks
the count around every test with these helpers, fails the test that leaked and releases its holds
so the tests after it start clean.
"""

from __future__ import annotations

import sys


def _loaded() -> bool:
    return "indus.service.self_chat" in sys.modules


def holders() -> int:
    """How many callers hold the ``self-chat`` deliverer now (0 when nothing imported it)."""
    if not _loaded():
        return 0
    from indus.service import self_chat

    return self_chat._users  # pyright: ignore[reportPrivateUsage]


def release_leaked(before: int) -> int:
    """Drop the holds taken since ``before`` (the deliverer goes when none is left); returns how
    many there were."""
    if not _loaded():
        return 0
    from indus.service import self_chat

    with self_chat._lock:  # pyright: ignore[reportPrivateUsage]
        leaked = self_chat._users - before  # pyright: ignore[reportPrivateUsage]
        if leaked <= 0:
            return 0
        self_chat._users = before  # pyright: ignore[reportPrivateUsage]
        undo = self_chat._undo  # pyright: ignore[reportPrivateUsage]
        if before == 0 and undo is not None:
            undo()
            self_chat._undo = None  # pyright: ignore[reportPrivateUsage]
    return leaked
