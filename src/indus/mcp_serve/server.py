"""``indus mcp serve``: the profile's messaging conversations as an MCP server (P19-14..17).

``open_bridge(scope)`` builds everything one server process needs, in this order: the store
reader (one private read-only connection), the event bridge (baseline, then its watch task), the
approval queue probe (the daemon must answer ``approvals-open`` with the queue capability), and
the MCP server named ``indus`` with its tools registered in a fixed order (the order is the
``tools/list`` order):

``conversations_list``, ``conversation_get``, ``messages_read``, ``attachments_fetch``,
``events_poll``, ``events_wait``, ``messages_send``, ``channels_list``, and — only when the probe
found the queue — ``permissions_list_open``, ``permissions_respond`` (D-16.30).

Every tool answers one JSON text. A tool never raises: a problem (a bad argument, a missing store,
no daemon, an unexpected failure) is a JSON object with an ``error`` field. Integer arguments
accept numbers or strings; out-of-range values are clamped, anything that is not a number takes
the default. Blocking work (store reads, control-socket calls) runs on the blocking pool.

``serve(scope, verbose=)`` runs it on stdio until the client closes stdin. Stdout carries MCP
frames only: the CLI entry starts ``mcp serve`` in the stdio logging mode (diagnostics to stderr at
WARNING, DEBUG with ``--verbose``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final

from mcp.server import MCPServer

from indus.foundation.blocking import run_blocking
from indus.foundation.clock import Clock
from indus.foundation.errors import StoreUnavailable
from indus.foundation.scope import Scope
from indus.mcp_serve.approvals import Approvals
from indus.mcp_serve.event_bridge import EventBridge
from indus.mcp_serve.read_tools import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    ReadTools,
    coerce_int,
)
from indus.mcp_serve.send import Sender
from indus.mcp_serve.store import StoreReader, describe_problem

__all__ = [
    "APPROVAL_TOOLS",
    "INSTRUCTIONS",
    "SERVER_NAME",
    "TOOL_ORDER",
    "Bridge",
    "open_bridge",
    "serve",
]

log = logging.getLogger(__name__)

SERVER_NAME: Final = "indus"
INSTRUCTIONS: Final = (
    "Indus Agent messaging bridge. These tools reach the chats this profile's gateway is "
    "connected to (Telegram, Discord, Slack, WhatsApp, Signal, Matrix and the other platforms): "
    "list conversations and read their history, wait for new messages, list the chats you can "
    "send to, and send a text through the running gateway."
)
TOOL_ORDER: Final = (
    "conversations_list",
    "conversation_get",
    "messages_read",
    "attachments_fetch",
    "events_poll",
    "events_wait",
    "messages_send",
    "channels_list",
)
APPROVAL_TOOLS: Final = ("permissions_list_open", "permissions_respond")

POLL_DEFAULT: Final = 20
WAIT_DEFAULT_MS: Final = 30_000
WAIT_MAX_MS: Final = 300_000
CURSOR_MAX: Final = 10**18

Number = int | float | str | None


def _text(answer: dict[str, Any]) -> str:
    return json.dumps(answer, indent=2, ensure_ascii=False, default=str)


def _problem(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, StoreUnavailable | sqlite3.Error):
        return {"error": describe_problem(exc)}
    log.warning("mcp serve tool failed", exc_info=True)
    return {"error": f"the tool failed ({type(exc).__name__})"}


async def _blocking(fn: Callable[[], dict[str, Any]]) -> str:
    try:
        return _text(await run_blocking(fn, label="mcp serve tool"))
    except Exception as exc:
        return _text(_problem(exc))


async def _guarded(make: Callable[[], Awaitable[dict[str, Any]]]) -> str:
    try:
        return _text(await make())
    except Exception as exc:
        return _text(_problem(exc))


@dataclass
class Bridge:
    """One server process's parts; ``aclose`` stops the watch and closes the store reader."""

    server: MCPServer
    reader: StoreReader
    events: EventBridge
    approvals: Approvals

    async def aclose(self) -> None:
        await self.events.aclose()
        self.reader.close()


def _register(server: MCPServer, tools: ReadTools, events: EventBridge, sender: Sender) -> None:
    async def conversations_list(
        platform: str | None = None, limit: Number = DEFAULT_LIMIT, search: str | None = None
    ) -> str:
        """List the messaging conversations of the connected platforms, newest first.

        Each one has the session key that messages_read, conversation_get and attachments_fetch
        take, its platform, chat type, display and chat names, and its last activity time.

        Args:
            platform: Only this platform (telegram, discord, slack, ...), any case.
            limit: How many to return, 1 to 200 (default 50).
            search: Only conversations whose display name, chat name or key contains this text.
        """
        return await _blocking(lambda: tools.conversations_list(platform, limit, search))

    async def conversation_get(session_key: str) -> str:
        """Details of one conversation: chat and thread ids, times and token totals.

        Args:
            session_key: A session key from conversations_list.
        """
        return await _blocking(lambda: tools.conversation_get(session_key))

    async def messages_read(session_key: str, limit: Number = DEFAULT_LIMIT) -> str:
        """The latest user and assistant messages of a conversation, oldest first.

        Each message has its id (for attachments_fetch), role, text (up to 2000 characters) and
        time; total_in_session counts every text message of the conversation.

        Args:
            session_key: A session key from conversations_list.
            limit: How many of the most recent messages, 1 to 200 (default 50).
        """
        return await _blocking(lambda: tools.messages_read(session_key, limit))

    async def attachments_fetch(session_key: str, message_id: str) -> str:
        """The attachments of one message: images, other non-text parts and the media files
        it names. Files outside the folders media may be sent from are listed under refused.

        Args:
            session_key: A session key from conversations_list.
            message_id: A message id from messages_read.
        """
        return await _blocking(lambda: tools.attachments_fetch(session_key, str(message_id)))

    async def events_poll(
        after_cursor: Number = 0, session_key: str | None = None, limit: Number = POLL_DEFAULT
    ) -> str:
        """Events queued since a cursor: new messages (and approval prompts when offered).

        Pass the returned next_cursor to the next call. dropped tells how many events after
        your cursor were pushed out of the queue (it keeps the latest 1000).

        Args:
            after_cursor: Return events after this cursor (0 for everything queued).
            session_key: Only events of this conversation.
            limit: How many events, 1 to 200 (default 20).
        """

        async def run() -> dict[str, Any]:
            after = coerce_int(after_cursor, default=0, low=0, high=CURSOR_MAX)
            count = coerce_int(limit, default=POLL_DEFAULT, low=1, high=MAX_LIMIT)
            return events.poll(after, session_key or None, count)

        return await _guarded(run)

    async def events_wait(
        after_cursor: Number = 0,
        session_key: str | None = None,
        timeout_ms: Number = WAIT_DEFAULT_MS,
    ) -> str:
        """Wait for the next event after a cursor (long poll).

        Answers as soon as a matching event is queued, or {"event": null, "reason": "timeout"}
        when the time is up.

        Args:
            after_cursor: Wait for an event after this cursor.
            session_key: Only events of this conversation.
            timeout_ms: How long to wait, 0 to 300000 milliseconds (default 30000).
        """

        async def run() -> dict[str, Any]:
            after = coerce_int(after_cursor, default=0, low=0, high=CURSOR_MAX)
            wait = coerce_int(timeout_ms, default=WAIT_DEFAULT_MS, low=0, high=WAIT_MAX_MS)
            return await events.wait(after, session_key or None, wait)

        return await _guarded(run)

    async def messages_send(target: str, message: str) -> str:
        """Send a text to a chat through the running gateway.

        The target is "platform:chat_id" (as channels_list shows it) or a platform and a chat
        name, for example "telegram:123456789", "discord:#general", "slack:#engineering".
        Text only. The gateway must be running.

        Args:
            target: Where to send, "platform:chat_id" or "platform:<chat name>".
            message: The text to send.
        """
        return await _blocking(lambda: sender.send(target, message))

    async def channels_list(platform: str | None = None) -> str:
        """The chats you can send to, with the target string messages_send takes.

        Args:
            platform: Only this platform (telegram, discord, slack, ...), any case.
        """
        return await _blocking(lambda: tools.channels_list(platform))

    for fn in (
        conversations_list,
        conversation_get,
        messages_read,
        attachments_fetch,
        events_poll,
        events_wait,
        messages_send,
        channels_list,
    ):
        server.add_tool(fn, structured_output=False)


def _register_approvals(server: MCPServer, approvals: Approvals) -> None:
    async def permissions_list_open() -> str:
        """Approval prompts waiting for an answer in the daemon's sessions.

        Each has its id, the conversation's session key (for chats), the command (redacted),
        why it needs approval and the decisions it allows.
        """
        return await _guarded(approvals.list_open)

    async def permissions_respond(id: str, decision: str) -> str:  # noqa: A002 - the tool's wire name
        """Answer an approval prompt. The first answer from any surface wins.

        Args:
            id: An approval id from permissions_list_open.
            decision: One of "allow-once", "allow-session", "allow-always" or "deny".
        """
        return await _guarded(lambda: approvals.respond(id, decision))

    server.add_tool(permissions_list_open, structured_output=False)
    server.add_tool(permissions_respond, structured_output=False)


async def open_bridge(
    scope: Scope,
    *,
    clock: Clock | None = None,
    approvals: Approvals | None = None,
    sender: Sender | None = None,
    start_events: bool = True,
) -> Bridge:
    """Build one server's parts (see the module notes); the caller ``aclose``s the result."""
    used_clock: Clock = clock or scope.clock
    reader = StoreReader(scope)
    events = EventBridge(reader, used_clock)
    queue = approvals or Approvals(scope, bridge=events, clock=used_clock)
    queue.bridge = events
    try:
        if start_events:
            await events.start()
        else:
            await events.baseline()
        offered = await queue.probe()
        server = MCPServer(SERVER_NAME, instructions=INSTRUCTIONS)
        _register(server, ReadTools(scope, reader), events, sender or Sender(scope))
        if offered:
            _register_approvals(server, queue)
            events.on_tick(queue.watch)
    except BaseException:
        await events.aclose()
        reader.close()
        raise
    return Bridge(server, reader, events, queue)


def serve(scope: Scope, *, verbose: bool = False) -> int:
    """Run the server on stdio until the client closes it; the process exit code."""
    if verbose:
        from indus.foundation import log as process_log

        process_log.setup(scope, mode="stdio", verbose=True)

    async def main() -> None:
        bridge = await open_bridge(scope)
        try:
            await bridge.server.run_stdio_async()
        finally:
            await bridge.aclose()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        return 0
    return 0
