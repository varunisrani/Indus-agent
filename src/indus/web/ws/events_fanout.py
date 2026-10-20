"""``/api/pub`` and ``/api/events``: the Chat tab's event feed (P18-09).

The TUI child (started by ``/api/pty`` with the publish URL and the internal credential) opens
``/api/pub?channel=<id>`` and sends every event it sees as one text frame. The backend hands each
frame to every ``/api/events?channel=<id>`` subscriber of that channel — the right rail's feed —
without touching the TUI's own terminal stream.

Both pass the shared WebSocket helper (``guarded_ws``): ``/api/events`` with purpose ``web-events``
(a person's credential), ``/api/pub`` with ``web-pub``, which only the internal credential may
open. Publishing is for the server-spawned child only: the helper refuses any other credential
there, and a grant that is not the internal one is closed 4401 again in the handler, so a browser
cannot put events into anyone's feed. A missing or invalid channel closes 4400. Publisher frames
must be text (binary closes 1003) of at most 1 MiB (else 1009).

Subscribers never speak: whatever they send is read and dropped. Each subscriber has its own queue
of at most 256 frames and its own writer, so a slow tab never holds up the publisher or the other
tabs; one that falls that far behind is closed 1013 and reconnects. A channel whose last subscriber
left is dropped.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Final

from starlette.websockets import WebSocket, WebSocketDisconnect

from indus.web.pty.channels import valid_channel
from indus.web.pty.registry import close_quietly
from indus.web.ws import WsGrant, guarded_ws
from indus.web.ws.chat_gate import INTERNAL, PURPOSE_EVENTS, PURPOSE_PUB

__all__ = [
    "CLOSE_BAD_CHANNEL",
    "CLOSE_NOT_PUBLISHER",
    "MAX_FRAME_BYTES",
    "QUEUE_FRAMES",
    "EventsFanout",
    "EventsRoutes",
]

log = logging.getLogger(__name__)

CLOSE_BAD_CHANNEL: Final = 4400
CLOSE_NOT_PUBLISHER: Final = 4401
CLOSE_UNSUPPORTED: Final = 1003
CLOSE_TOO_BIG: Final = 1009
CLOSE_TOO_SLOW: Final = 1013
MAX_FRAME_BYTES: Final = 1024 * 1024
QUEUE_FRAMES: Final = 256


class _Subscriber:
    def __init__(self, limit: int) -> None:
        self.queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=limit)
        self.overflowed = False

    def offer(self, frame: str) -> None:
        if self.overflowed:
            return
        try:
            self.queue.put_nowait(frame)
        except asyncio.QueueFull:
            self.overflowed = True
            self._wake()

    def _wake(self) -> None:
        # Make room for the stop marker: the writer ends at once and closes the socket.
        with contextlib.suppress(asyncio.QueueEmpty):
            while True:
                self.queue.get_nowait()
        self.queue.put_nowait(None)

    def stop(self) -> None:
        with contextlib.suppress(asyncio.QueueFull):
            self.queue.put_nowait(None)


class EventsFanout:
    """Channels and their subscribers (in-process, one per backend)."""

    def __init__(self, *, queue_frames: int = QUEUE_FRAMES) -> None:
        self._queue_frames = queue_frames
        self._channels: dict[str, set[_Subscriber]] = {}

    def channels(self) -> dict[str, int]:
        """Subscriber counts per live channel."""
        return {name: len(subs) for name, subs in self._channels.items()}

    def _join(self, channel: str) -> _Subscriber:
        subscriber = _Subscriber(self._queue_frames)
        self._channels.setdefault(channel, set()).add(subscriber)
        return subscriber

    def _leave(self, channel: str, subscriber: _Subscriber) -> None:
        subs = self._channels.get(channel)
        if subs is None:
            return
        subs.discard(subscriber)
        if not subs:
            del self._channels[channel]

    def publish(self, channel: str, frame: str) -> int:
        """Queue ``frame`` for every subscriber of ``channel``; how many got it."""
        subs = list(self._channels.get(channel, ()))
        for subscriber in subs:
            subscriber.offer(frame)
        return len(subs)

    async def serve_subscriber(self, websocket: WebSocket, channel: str) -> None:
        """Send ``channel``'s frames to an accepted socket until it goes (or falls behind)."""
        subscriber = self._join(channel)

        async def drain_input() -> None:
            try:
                while True:
                    message = await websocket.receive()
                    if message.get("type") == "websocket.disconnect":
                        break
            except (RuntimeError, WebSocketDisconnect):
                pass
            finally:
                subscriber.stop()

        reader = asyncio.get_running_loop().create_task(drain_input(), name="indus-events-in")
        try:
            while True:
                frame = await subscriber.queue.get()
                if frame is None:
                    break
                try:
                    await websocket.send_text(frame)
                except Exception:
                    break
            if subscriber.overflowed:
                await close_quietly(websocket, CLOSE_TOO_SLOW, "event feed fell behind")
        finally:
            self._leave(channel, subscriber)
            reader.cancel()
            with contextlib.suppress(BaseException):
                await reader

    async def serve_publisher(self, websocket: WebSocket, channel: str) -> None:
        """Fan out every text frame of an accepted publisher socket."""
        while True:
            try:
                message = await websocket.receive()
            except (RuntimeError, WebSocketDisconnect):
                return
            if message.get("type") == "websocket.disconnect":
                return
            text = message.get("text")
            if not isinstance(text, str):
                await close_quietly(websocket, CLOSE_UNSUPPORTED, "text frames only")
                return
            if len(text.encode("utf-8", "replace")) > MAX_FRAME_BYTES:
                await close_quietly(websocket, CLOSE_TOO_BIG, "frame too large")
                return
            self.publish(channel, text)


class EventsRoutes:
    """The two endpoints over one ``EventsFanout`` (see the module notes)."""

    purpose: Final = PURPOSE_EVENTS
    publish_purpose: Final = PURPOSE_PUB

    def __init__(self, *, fanout: EventsFanout | None = None) -> None:
        self.fanout = fanout if fanout is not None else EventsFanout()

    @property
    def publish_endpoint(self) -> Callable[[WebSocket], Awaitable[None]]:
        async def pub_socket(websocket: WebSocket, grant: WsGrant) -> None:
            await self.handle_publish(websocket, grant)

        return guarded_ws(self.publish_purpose, allow_internal=True)(pub_socket)

    @property
    def events_endpoint(self) -> Callable[[WebSocket], Awaitable[None]]:
        async def events_socket(websocket: WebSocket, grant: WsGrant) -> None:
            await self.handle_events(websocket, grant)

        return guarded_ws(self.purpose)(events_socket)

    async def handle_publish(self, websocket: WebSocket, grant: WsGrant) -> None:
        if grant.credential != INTERNAL:
            log.warning("event publish refused: not the server's own child")
            await close_quietly(websocket, CLOSE_NOT_PUBLISHER, "publishing is not allowed")
            return
        channel = valid_channel(websocket.query_params.get("channel"))
        if channel is None:
            await close_quietly(websocket, CLOSE_BAD_CHANNEL, "bad channel")
            return
        await self.fanout.serve_publisher(websocket, channel)

    async def handle_events(self, websocket: WebSocket, grant: WsGrant) -> None:
        del grant  # any person's credential may watch a channel's feed
        channel = valid_channel(websocket.query_params.get("channel"))
        if channel is None:
            await close_quietly(websocket, CLOSE_BAD_CHANNEL, "bad channel")
            return
        await self.fanout.serve_subscriber(websocket, channel)
