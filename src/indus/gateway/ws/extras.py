"""The P24 method groups on the daemon's WebSocket (integration of P24-06, 14, 19, 20, 26, 31).

``WsExtras(scope, instance_id=…)`` adds to an endpoint's method table:

- ``projects.*`` (P24-14), ``pet.*`` (P24-19), ``capabilities.summary`` (P24-26) and ``voice.*``
  (P24-35, the desktop composer's dictation and read-aloud): their service handlers, which only
  need the connection's scope;
- ``wake.*`` (P24-20): one ``WakeRpc`` per daemon; the connection that armed the listener owns it,
  a detection reaches that connection as a ``wake.detected`` notification (sent from the audio
  thread through the loop), and a closing connection releases its lease (``connection_closed``);
- ``bots.*`` (P24-31): ``bots_rpc.for_root(scope)`` — every profile under the root, the canonical
  chat of each through ``indus.state.bot_chat`` (P24-07), their cron jobs;
- ``groups.*`` (P24-06): ``GroupsRpc`` over ``<root>/rooms.db`` and its task driver, opened on the
  first call and closed with the endpoint. The room loop that runs members' turns is not built
  (D-16.785): rooms, messages, stops and authority work; members do not answer yet.

``bots.*`` and ``groups.*`` answer plain JSON from their tables; it is validated here against the
result models in ``indus.protocol`` (a mismatch is an internal error, never a malformed frame).
Their blocking store work runs off the loop.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any, Protocol, cast

from indus.foundation.blocking import run_blocking
from indus.foundation.scope import Scope
from indus.protocol.base import ProtocolModel
from indus.protocol.bots import BOT_METHODS
from indus.protocol.groups import GROUP_METHODS
from indus.service import capabilities_rpc, pets_rpc, projects_rpc, voice_rpc
from indus.service.wake_rpc import WakeRpc

if TYPE_CHECKING:
    from indus.gateway.rooms.rpc import GroupsRpc
    from indus.service.bots_rpc import BotsRpc
    from indus.state.rooms import RoomStore

__all__ = ["WsExtras"]

log = logging.getLogger(__name__)

type _Handler = Callable[[Any, Any], Awaitable[ProtocolModel]]


class _Notifiable(Protocol):
    def notify(self, method: str, params: Mapping[str, Any]) -> None: ...


class _Table(Protocol):
    def add(self, name: str, handler: _Handler) -> None: ...


def _plain_handler(
    name: str,
    result: type[ProtocolModel],
    table: Callable[[], Callable[[str, Mapping[str, Any]], dict[str, Any]]],
) -> _Handler:
    """A dispatcher handler over a plain-JSON table's ``call(name, params)``."""

    async def handle(_ctx: Any, params: ProtocolModel) -> ProtocolModel:
        call = table()
        wire = params.model_dump(mode="json", exclude_none=True)
        answer = await run_blocking(call, name, wire, label=name)
        return result.model_validate(answer)

    return handle


class WsExtras:
    """See the module notes."""

    def __init__(self, scope: Scope, *, instance_id: str) -> None:
        self.scope = scope
        self.instance_id = instance_id
        self._loop: asyncio.AbstractEventLoop | None = None
        self.wake = WakeRpc(scope, emit=self._emit)
        self._bots: BotsRpc | None = None
        self._rooms: RoomStore | None = None
        self._groups: GroupsRpc | None = None
        self._guard = threading.Lock()

    # -- the table --------------------------------------------------------------------------------

    def register(self, table: _Table) -> None:
        projects_rpc.register(table)
        pets_rpc.register(table)
        capabilities_rpc.register(table)
        voice_rpc.register(table)
        self.wake.register(table)
        for name, _params, result, _errors in BOT_METHODS:
            table.add(name, _plain_handler(name, result, self._bots_call))
        for name, _params, result, _errors in GROUP_METHODS:
            table.add(name, _plain_handler(name, result, self._groups_call))

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """The loop notifications are sent on (the endpoint's)."""
        self._loop = loop

    # -- wake -------------------------------------------------------------------------------------

    def _emit(self, owner: object, method: str, model: ProtocolModel) -> None:
        loop = self._loop
        if loop is None or loop.is_closed() or not hasattr(owner, "notify"):
            return
        frame = model.to_wire()
        try:
            loop.call_soon_threadsafe(_send, owner, method, frame)
        except RuntimeError:  # the loop closed meanwhile: the connection is gone too
            log.debug("wake notification dropped: the loop is closed")

    def connection_closed(self, connection: object) -> None:
        self.wake.connection_closed(connection)

    # -- bots and groups --------------------------------------------------------------------------

    def _bots_call(self) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
        with self._guard:
            if self._bots is None:
                from indus.service.bots_rpc import for_root

                self._bots = for_root(self.scope)
            return self._bots.call

    def _groups_call(self) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
        with self._guard:
            if self._groups is None:
                from indus.gateway.rooms.driver import RoomDriver
                from indus.gateway.rooms.rpc import GroupsRpc
                from indus.state.rooms import RoomStore

                store = RoomStore(self.scope)
                self._rooms = store
                self._groups = GroupsRpc(
                    store, RoomDriver(store), process_generation=self.instance_id
                )
            return self._groups.call

    def close(self) -> None:
        """Stop the wake listener and close the room store (the endpoint stopped)."""
        try:
            owner = self.wake.service.owner()
            if owner is not None:
                self.wake.service.stop(owner)
        except Exception:
            log.debug("the wake listener did not stop cleanly", exc_info=True)
        with self._guard:
            store, self._rooms, self._groups = self._rooms, None, None
        if store is not None:
            store.close()


def _send(owner: object, method: str, frame: Mapping[str, Any]) -> None:
    target = cast(_Notifiable, owner)
    target.notify(method, frame)
