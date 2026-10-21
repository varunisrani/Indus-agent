"""The ``self-chat`` cron delivery target (P24-07, H11): a job's output is added to its own
profile's canonical bot chat as an incoming (user-role) message; no turn runs.

``install_self_chat`` registers ``deliver_self_chat`` in cron's deliverer table while at least
one caller holds it (one per delivery router, the CLI scheduler pass; several may live in one
process) and returns that caller's undo. It lives in the service layer so the gateway and the CLI
share it (layer rule D-16.246).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Final

from indus.cron import delivery as cron_delivery
from indus.cron.delivery import DeliveryReceipt, DeliveryRequest
from indus.foundation.blocking import run_blocking
from indus.state import registry
from indus.state.bot_chat import inject_incoming

__all__ = ["SELF_CHAT", "deliver_self_chat", "install_self_chat"]

SELF_CHAT: Final = "self-chat"


async def deliver_self_chat(request: DeliveryRequest) -> DeliveryReceipt:
    """Add the job's output to its own profile's canonical chat as an incoming message."""
    scope = request.scope
    chat = request.target.chat_id
    if chat not in (None, "", scope.profile_id):
        return DeliveryReceipt(False, error="self-chat reaches only this profile's own chat")

    def inject() -> tuple[str, int]:
        with registry.shared(scope) as store:
            return inject_incoming(store, request.text)

    session_id, row = await run_blocking(inject, label="self-chat")
    return DeliveryReceipt(True, message_id=f"{session_id}:{row}")


_lock = threading.Lock()
_users = 0
_undo: Callable[[], None] | None = None


def install_self_chat() -> Callable[[], None]:
    """Register the deliverer while held (see the module notes); returns this caller's undo."""
    global _users, _undo
    with _lock:
        _users += 1
        if _undo is None:
            _undo = cron_delivery.register_deliverer(SELF_CHAT, deliver_self_chat)
    released = False

    def undo() -> None:
        global _users, _undo
        nonlocal released
        with _lock:
            if released:
                return
            released = True
            _users -= 1
            if _users == 0 and _undo is not None:
                _undo()
                _undo = None

    return undo
