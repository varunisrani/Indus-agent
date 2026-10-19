"""A loopback fake model that answers each request from the conversation it belongs to.

``HttpFakeModel`` plays one script in order, which only works for one conversation at a time. A
batch run has several conversations in flight (one per worker process), so this fake picks each
answer from the request itself: ``reply(prompt, step)`` gets the conversation's first user
message and how many assistant messages it already has, and returns one script turn (a mapping in
the script file format). Every request is logged as ``{"kind": "start" | "continue", "prompt",
"step"}``; ``starts`` counts the conversations that began.

``start_limit`` lets that many conversations begin and stalls every later first request until
the server stops (a run frozen mid-way, ready to be killed); follow-up requests of conversations
already running are always answered.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Callable, Iterator, Mapping
from typing import Any

import httpx

from indus.foundation.clock import SystemClock
from tests.fakes.model import FakeModelServer, parse_script
from tests.fakes.model.http_serve import HttpFakeModel

type Reply = Callable[[str, int], Mapping[str, Any]]


def _messages(body: Any) -> list[dict[str, Any]]:
    if isinstance(body, dict) and isinstance(body.get("messages"), list):
        return [m for m in body["messages"] if isinstance(m, dict)]
    return []


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return ""


def conversation_of(body: Any) -> tuple[str, int]:
    """``(first user text, assistant messages after it)`` of one request."""
    messages = _messages(body)
    for position, message in enumerate(messages):
        if message.get("role") == "user":
            later = messages[position + 1 :]
            return _text(message.get("content")).strip(), sum(
                1 for m in later if m.get("role") == "assistant"
            )
    return "", 0


class RoutedFakeModel(HttpFakeModel):
    def __init__(self, reply: Reply, *, start_limit: int | None = None) -> None:
        super().__init__(parse_script({"turns": [{"steps": [{"text": "unused"}]}]}, "routed"))
        self.reply = reply
        self.start_limit = start_limit
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._never = asyncio.Event()

    @property
    def starts(self) -> list[str]:
        with self._lock:
            return [r["prompt"] for r in self.requests if r["kind"] == "start"]

    async def _answer(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
        method: str,
        target: str,
        headers: dict[str, str],
        raw: bytes,
        body: Any,
        writer: asyncio.StreamWriter,
    ) -> None:
        if method == "GET":
            await super()._answer(method, target, headers, raw, body, writer)
            return
        prompt, step = conversation_of(body)
        kind = "start" if step == 0 else "continue"
        with self._lock:
            started = sum(1 for r in self.requests if r["kind"] == "start")
            stall = kind == "start" and self.start_limit is not None and started >= self.start_limit
            self.requests.append(
                {"kind": "stalled" if stall else kind, "prompt": prompt, "step": step}
            )
        if stall:
            await self._never.wait()  # cancelled when the server stops
            return
        turn = self.reply(prompt, step)
        server = FakeModelServer(parse_script({"turns": [turn]}, "routed"), clock=SystemClock())
        request = httpx.Request(
            method, f"http://127.0.0.1:{self.port}{target}", headers=headers, content=raw
        )
        try:
            response = await server.transport.handle_async_request(request)
        except Exception as exc:
            await self._send_whole(writer, 500, {"error": {"message": str(exc)}})
            return
        await self._send_stream(writer, response)


@contextlib.contextmanager
def serving_routed(reply: Reply, *, start_limit: int | None = None) -> Iterator[RoutedFakeModel]:
    """Run a ``RoutedFakeModel`` on its own event-loop thread for the length of a block."""
    fake = RoutedFakeModel(reply, start_limit=start_limit)
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(fake.start())
        ready.set()
        loop.run_forever()

    thread = threading.Thread(target=run, name="routed-fake-model", daemon=True)
    thread.start()
    ready.wait(10)
    try:
        yield fake
    finally:
        asyncio.run_coroutine_threadsafe(fake.stop(), loop).result(10)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
        loop.close()
