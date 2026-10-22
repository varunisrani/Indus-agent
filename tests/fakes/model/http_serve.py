"""A fake-model script served on a real loopback socket, for child processes (P12-33).

``FakeModelServer`` answers inside the test process only. A child process (the TUI's bridge, a
renderer started under a pseudo-terminal) reaches a model by URL, so this module puts the same
scripted server behind a small HTTP/1.1 listener on ``127.0.0.1``. Point a profile at it with
``model: {provider: custom, base_url: http://127.0.0.1:<port>/v1}``.

Two kinds of request never use up a script turn: ``GET …/models`` (the model list) and the
automatic session title request (its system prompt is ``TITLE_PROMPT``); it is answered with
``Chat:`` and the first line of the user's first message (the product's ``clean_title`` fits
it to the title width). Every request is reported to
``on_request`` as a plain dict (``{"kind": "model" | "title" | "models", "turn": n, "path": ...,
"user": last user text, "model": requested model}``).

As a program: ``python -m tests.fakes.model.http_serve <script name or path>`` prints
``{"port": n}`` on its first stdout line, then one JSON line per request, and serves until its
stdin closes or it gets SIGTERM.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import sys
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from indus.foundation.clock import SystemClock
from indus.providers.auxiliary import TITLE_PROMPT
from tests.fakes.model import FakeModelServer, Script, load_script, parse_script

TITLE_PREFIX = "Chat:"
_HEAD_LIMIT = 64 * 1024


def _last_user_text(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    messages = body.get("messages")
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = [p.get("text", "") for p in content if isinstance(p, dict)]
                return "".join(str(p) for p in parts)
    return ""


def title_for(exchange: str) -> str:
    """A title that tells sessions apart: ``Chat:`` and the first line the user wrote."""
    first = exchange.removeprefix("User:").strip().split("\n", 1)[0]
    return " ".join([TITLE_PREFIX, *first.split()])


def _is_title_request(body: Any) -> bool:
    if not isinstance(body, dict):
        return False
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return False
    first = messages[0]
    return isinstance(first, dict) and first.get("content") == TITLE_PROMPT


@dataclass
class HttpFakeModel:
    """One scripted model behind a loopback HTTP listener."""

    script: Script
    on_request: Callable[[dict[str, Any]], None] | None = None
    log: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    port: int = 0

    def __post_init__(self) -> None:
        self.model = FakeModelServer(self.script, clock=SystemClock())
        self._server: asyncio.Server | None = None
        self._open: set[asyncio.Task[Any]] = set()

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        self._server = await asyncio.start_server(self._serve, host, port)
        self.port = int(self._server.sockets[0].getsockname()[1])
        return self.port

    async def stop(self) -> None:
        """Stop listening and end every answer still running (a stalled stream included)."""
        if self._server is not None:
            self._server.close()
            for task in list(self._open):
                task.cancel()
            await asyncio.gather(*self._open, return_exceptions=True)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 2)
            self._server = None

    @property
    def model_turns(self) -> int:
        """How many script turns were used so far."""
        return sum(1 for entry in self.log if entry["kind"] == "model")

    # -- one connection, one request ---------------------------------------------------------

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._open.add(task)
            task.add_done_callback(self._open.discard)
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
            writer.close()
            return
        if len(head) > _HEAD_LIMIT:
            writer.close()
            return
        lines = head.decode("latin-1").split("\r\n")
        method, target, _version = (lines[0].split(" ", 2) + ["", ""])[:3]
        headers: dict[str, str] = {}
        for line in lines[1:]:
            name, sep, value = line.partition(":")
            if sep:
                headers[name.strip().lower()] = value.strip()
        size = int(headers.get("content-length", "0") or 0)
        body_bytes = await reader.readexactly(size) if size else b""
        try:
            body: Any = json.loads(body_bytes) if body_bytes else None
        except ValueError:
            body = None
        await self._answer(method, target, headers, body_bytes, body, writer)

    async def _answer(
        self,
        method: str,
        target: str,
        headers: dict[str, str],
        raw: bytes,
        body: Any,
        writer: asyncio.StreamWriter,
    ) -> None:
        if method == "GET":
            kind = "models"
            target_server = self.model
        elif _is_title_request(body):
            kind = "title"
            title = title_for(_last_user_text(body))
            target_server = FakeModelServer(
                parse_script({"turns": [{"steps": [{"text": title}]}]}, "title"),
                clock=SystemClock(),
            )
        else:
            kind = "model"
            target_server = self.model
        entry: dict[str, Any] = {"kind": kind, "path": target, "user": _last_user_text(body)}
        if isinstance(body, dict) and isinstance(body.get("model"), str):
            entry["model"] = body["model"]
        if kind == "model":
            entry["turn"] = self.model_turns + 1
        self.log.append(entry)
        if self.on_request is not None:
            self.on_request(entry)
        request = httpx.Request(
            method, f"http://127.0.0.1:{self.port}{target}", headers=headers, content=raw
        )
        try:
            response = await target_server.transport.handle_async_request(request)
        except Exception as exc:  # script exhausted or broken: a loud 500, never a hang
            await self._send_whole(writer, 500, {"error": {"message": str(exc)}})
            return
        await self._send_stream(writer, response)

    async def _send_whole(self, writer: asyncio.StreamWriter, status: int, data: Any) -> None:
        payload = json.dumps(data).encode("utf-8")
        writer.write(
            f"HTTP/1.1 {status} X\r\ncontent-type: application/json\r\n"
            f"content-length: {len(payload)}\r\nconnection: close\r\n\r\n".encode("latin-1")
            + payload
        )
        with contextlib.suppress(ConnectionError):
            await writer.drain()
        writer.close()

    async def _send_stream(self, writer: asyncio.StreamWriter, response: httpx.Response) -> None:
        head = [f"HTTP/1.1 {response.status_code} X"]
        for name, value in response.headers.items():
            if name.lower() not in {"content-length", "transfer-encoding", "connection"}:
                head.append(f"{name}: {value}")
        head += ["transfer-encoding: chunked", "connection: close", "", ""]
        stream = response.stream
        try:
            writer.write("\r\n".join(head).encode("latin-1"))
            await writer.drain()
            async for chunk in stream:  # type: ignore[union-attr]
                if chunk:
                    writer.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                    await writer.drain()
            writer.write(b"0\r\n\r\n")
            await writer.drain()
            writer.close()
        except httpx.RemoteProtocolError:
            writer.transport.abort()  # the script asked for a cut connection
        except ConnectionError:
            writer.transport.abort()  # the client went away (interrupt, killed child)
        finally:
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                with contextlib.suppress(Exception):
                    await aclose()


@contextlib.contextmanager
def serving(script: Script | str | Path) -> Iterator[HttpFakeModel]:
    """Run an ``HttpFakeModel`` on its own event loop thread for the length of a block."""
    loaded = script if isinstance(script, Script) else load_script(script)
    fake = HttpFakeModel(loaded)
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(fake.start())
        ready.set()
        loop.run_forever()

    thread = threading.Thread(target=run, name="fake-model-http", daemon=True)
    thread.start()
    ready.wait(10)
    try:
        yield fake
    finally:
        asyncio.run_coroutine_threadsafe(fake.stop(), loop).result(5)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
        loop.close()


def _print(entry: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(entry) + "\n")
    sys.stdout.flush()


async def _main(script: Script) -> None:
    fake = HttpFakeModel(script, on_request=_print)
    port = await fake.start()
    _print({"port": port})
    loop = asyncio.get_running_loop()
    done = asyncio.Event()
    loop.add_signal_handler(signal.SIGTERM, done.set)
    # stdin closing means whoever started us is gone.
    watcher = threading.Thread(
        target=lambda: (sys.stdin.read(), loop.call_soon_threadsafe(done.set)), daemon=True
    )
    watcher.start()
    await done.wait()
    await fake.stop()


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        sys.stderr.write("usage: python -m tests.fakes.model.http_serve <script>\n")
        return 64
    asyncio.run(_main(load_script(argv[0])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
