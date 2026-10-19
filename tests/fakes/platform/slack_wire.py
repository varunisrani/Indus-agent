"""A fake of the Slack library surface our adapter touches (P15-01).

Shapes follow slack-sdk / slack-bolt: an async Web API client whose methods take keyword
arguments and answer dict-like responses (``{"ok": True, "ts": ..., "channel": ...}``); errors
raise a ``SlackApiError`` stand-in carrying ``response["error"]`` (``ratelimited`` with a
``Retry-After`` header, ``channel_not_found``, ``not_in_channel``, ``msg_too_long``,
``invalid_blocks``, ...). Inbound events are Events API payloads (``message``, ``app_mention``
sharing one ``ts``, ``bot_message`` subtype, ``block_actions`` for buttons) handed to the
handlers the adapter registers on the fake app by ``dispatch_pending()``. Cap: 39,000.

Message ids on the wire are ``ts`` strings; `SlackPlatform` (the default platform) also finds a
message by its ``ts``, and ids that already look like a ``ts`` are kept as they are. Actions are
matched like Bolt does: a string by equality, a compiled pattern by ``search``.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from tests.fakes.platform.core import (
    Button,
    FailureKind,
    FakeChatPlatform,
    FakeInbound,
    FakeMedia,
    FakeMessage,
    FakePlatformError,
)

MAX_LEN = 39000

SURFACE: dict[str, tuple[str, ...]] = {
    "slack_sdk.web.async_client:AsyncWebClient": (
        "auth_test",
        "chat_postMessage",
        "chat_update",
        "chat_delete",
        "chat_postEphemeral",
        "reactions_add",
        "reactions_remove",
        "files_upload_v2",
        "conversations_replies",
        "users_info",
        "assistant_threads_setStatus",
        "conversations_open",
        "conversations_info",
    ),
    "slack_sdk.errors:SlackApiError": (),
    "slack_bolt.app.async_app:AsyncApp": ("event", "action", "client"),
    "slack_bolt.adapter.socket_mode.async_handler:AsyncSocketModeHandler": (
        "connect_async",
        "start_async",
        "close_async",
    ),
}


class SlackResponse(dict[str, Any]):
    """Dict-like response with ``headers`` (``Retry-After`` on rate limits)."""

    def __init__(self, data: dict[str, Any], headers: dict[str, str] | None = None) -> None:
        super().__init__(data)
        self.headers = headers or {}


class SlackApiError(Exception):
    def __init__(self, message: str, response: SlackResponse) -> None:
        super().__init__(f"{message}\nThe server responded with: {dict(response)}")
        self.response = response


def _error_code(exc: FakePlatformError) -> str:
    return {
        FailureKind.RATE_LIMIT: "ratelimited",
        FailureKind.CHAT_NOT_FOUND: "channel_not_found",
        FailureKind.THREAD_NOT_FOUND: "thread_not_found",
        FailureKind.FORBIDDEN: "not_in_channel",
        FailureKind.PARSE_ERROR: "invalid_blocks",
        FailureKind.TOO_LONG: "msg_too_long",
    }.get(exc.kind, "internal_error")


def _translate(exc: FakePlatformError) -> Exception:
    if exc.kind is FailureKind.READ_TIMEOUT:
        return TimeoutError()
    if exc.kind is FailureKind.CONNECT_TIMEOUT:
        return ConnectionResetError("Cannot connect to host slack.com:443")
    code = _error_code(exc)
    headers = {"Retry-After": str(int(exc.retry_after or 5))} if code == "ratelimited" else {}
    response = SlackResponse({"ok": False, "error": code}, headers)
    return SlackApiError("The request to the Slack API failed.", response)


_TS_BASE = "1700000000."


def _as_ts(value: str | None) -> str:
    """A platform id as a Slack ``ts`` (ids that already are one are kept)."""
    text = str(value or 0)
    return text if "." in text else f"{_TS_BASE}{int(text):06d}"


def _ts(message: FakeMessage | FakeInbound) -> str:
    return _as_ts(message.message_id)


def _id_of(ts: str | None) -> str | None:
    if not ts:
        return None
    if not ts.startswith(_TS_BASE):
        return ts
    return str(int(ts.split(".", 1)[1]))


def _buttons(blocks: list[dict[str, Any]] | None) -> tuple[Button, ...]:
    return tuple(
        Button(str(e.get("text", {}).get("text", "")), str(e.get("action_id", "")))
        for b in blocks or ()
        if b.get("type") == "actions"
        for e in b.get("elements", ())
    )


class SlackPlatform(FakeChatPlatform):
    """The fake platform with Slack's ids: a message is also found by its ``ts``."""

    def message(self, message_id: str) -> FakeMessage | None:
        return super().message(str(_id_of(str(message_id))))


@dataclass
class FakeWebClient:
    """``AsyncWebClient`` stand-in over a `FakeChatPlatform`; ids are ``ts`` strings."""

    wire: SlackWire
    calls: list[tuple[str, dict[str, Any]]] = field(
        default_factory=list[tuple[str, dict[str, Any]]]
    )

    async def _do(self, name: str, kwargs: dict[str, Any], coro: Any) -> Any:
        self.calls.append((name, kwargs))
        try:
            return await coro
        except FakePlatformError as exc:
            raise _translate(exc) from None

    async def auth_test(self) -> SlackResponse:
        platform = self.wire.platform
        await self._do("auth_test", {}, platform.connect())
        return SlackResponse(
            {
                "ok": True,
                "user_id": platform.bot_user_id,
                "team_id": self.wire.team_id,
                "bot_id": "B" + platform.bot_user_id,
            }
        )

    async def chat_postMessage(  # noqa: N802 (vendor method name)
        self,
        *,
        channel: str,
        text: str = "",
        thread_ts: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
        **kw: Any,
    ) -> SlackResponse:
        buttons = _buttons(blocks)
        sent = await self._do(
            "chat_postMessage",
            {"channel": channel, "text": text, "thread_ts": thread_ts, "blocks": blocks, **kw},
            self.wire.platform.post(channel, text, thread_id=_id_of(thread_ts), buttons=buttons),
        )
        return SlackResponse({"ok": True, "channel": channel, "ts": _ts(sent)})

    async def chat_update(  # noqa: N802
        self, *, channel: str, ts: str, text: str = "", **kw: Any
    ) -> SlackResponse:
        # Like Slack: blocks given replace the old ones (an empty list drops the buttons).
        buttons = _buttons(kw["blocks"]) if "blocks" in kw else None
        await self._do(
            "chat_update",
            {"channel": channel, "ts": ts, "text": text, **kw},
            self.wire.platform.edit(channel, str(_id_of(ts)), text, buttons=buttons),
        )
        return SlackResponse({"ok": True, "channel": channel, "ts": ts})

    async def chat_delete(self, *, channel: str, ts: str) -> SlackResponse:  # noqa: N802
        await self._do(
            "chat_delete",
            {"channel": channel, "ts": ts},
            self.wire.platform.delete(channel, str(_id_of(ts))),
        )
        return SlackResponse({"ok": True})

    async def chat_postEphemeral(  # noqa: N802
        self, *, channel: str, user: str, text: str, **kw: Any
    ) -> SlackResponse:
        self.calls.append(("chat_postEphemeral", {"channel": channel, "user": user, "text": text}))
        self.wire.ephemeral.append((channel, user, text))
        return SlackResponse({"ok": True, "message_ts": "1700000000.999999"})

    async def reactions_add(self, *, channel: str, timestamp: str, name: str) -> SlackResponse:
        await self._do(
            "reactions_add",
            {"channel": channel, "timestamp": timestamp, "name": name},
            self.wire.platform.react(channel, str(_id_of(timestamp)), name),
        )
        return SlackResponse({"ok": True})

    async def reactions_remove(self, *, channel: str, timestamp: str, name: str) -> SlackResponse:
        self.calls.append(("reactions_remove", {"channel": channel, "timestamp": timestamp}))
        return SlackResponse({"ok": True})

    async def files_upload_v2(
        self,
        *,
        channel: str,
        file: str,
        initial_comment: str = "",
        thread_ts: str | None = None,
        **kw: Any,
    ) -> SlackResponse:
        media = FakeMedia("document", str(file), caption=initial_comment)
        sent = await self._do(
            "files_upload_v2",
            {"channel": channel, "file": file, **kw},
            self.wire.platform.upload(channel, media, thread_id=_id_of(thread_ts)),
        )
        return SlackResponse({"ok": True, "files": [{"id": f"F{sent.message_id}"}]})

    async def conversations_replies(self, *, channel: str, ts: str, **kw: Any) -> SlackResponse:
        self.calls.append(("conversations_replies", {"channel": channel, "ts": ts}))
        root = self.wire.platform.message(str(_id_of(ts)))
        messages = [{"ts": ts, "text": root.text}] if root is not None else []
        return SlackResponse({"ok": True, "messages": messages})

    async def users_info(self, *, user: str) -> SlackResponse:
        self.calls.append(("users_info", {"user": user}))
        return SlackResponse({"ok": True, "user": {"id": user, "name": f"user-{user}"}})

    async def conversations_open(self, *, users: str, **kw: Any) -> SlackResponse:
        """A user's DM; on the fake platform the DM chat id is the user id."""
        self.calls.append(("conversations_open", {"users": users}))
        return SlackResponse({"ok": True, "channel": {"id": users}})

    async def conversations_info(self, *, channel: str, **kw: Any) -> SlackResponse:
        chat = await self._do(
            "conversations_info", {"channel": channel}, self.wire.platform.chat(channel)
        )
        return SlackResponse(
            {
                "ok": True,
                "channel": {
                    "id": chat.chat_id,
                    "name": chat.name,
                    "is_im": chat.kind == "dm",
                    "is_channel": chat.kind != "dm",
                },
            }
        )

    async def assistant_threads_setStatus(  # noqa: N802
        self, *, channel_id: str, thread_ts: str, status: str
    ) -> SlackResponse:
        await self._do(
            "assistant_threads_setStatus",
            {"channel_id": channel_id, "thread_ts": thread_ts, "status": status},
            self.wire.platform.typing(channel_id, thread_id=_id_of(thread_ts)),
        )
        return SlackResponse({"ok": True})


Handler = Callable[..., Awaitable[None]]


class FakeApp:
    """``AsyncApp`` stand-in: ``event(name)`` / ``action(id)`` decorators register handlers."""

    def __init__(self, wire: SlackWire) -> None:
        self.wire = wire
        self.events: dict[str, Handler] = {}
        self.actions: dict[str | re.Pattern[str], Handler] = {}

    @property
    def client(self) -> FakeWebClient:
        return self.wire.client

    def event(self, name: str) -> Callable[[Handler], Handler]:
        def register(fn: Handler) -> Handler:
            self.events[name] = fn
            return fn

        return register

    def action(self, action_id: str | re.Pattern[str]) -> Callable[[Handler], Handler]:
        def register(fn: Handler) -> Handler:
            self.actions[action_id] = fn
            return fn

        return register

    def action_for(self, action_id: str) -> Handler | None:
        """The handler Bolt would pick: an equal string, then a matching pattern, then ``*``."""
        found = self.actions.get(action_id)
        if found is not None:
            return found
        for key, handler in self.actions.items():
            if isinstance(key, re.Pattern) and key.search(action_id):
                return handler
        return self.actions.get("*")


class FakeSocketModeHandler:
    """``AsyncSocketModeHandler`` stand-in: connecting checks the app token with the platform."""

    def __init__(self, app: FakeApp, app_token: str) -> None:
        self.app = app
        self.app_token = app_token
        self.connected = False

    async def connect_async(self) -> None:
        try:
            await self.app.wire.platform.connect()
        except FakePlatformError as exc:
            raise _translate(exc) from None
        self.connected = True

    async def start_async(self) -> None:
        await self.connect_async()

    async def close_async(self) -> None:
        self.connected = False
        await self.app.wire.platform.disconnect()


class SlackWire:
    SURFACE = SURFACE
    FAKES: dict[str, type] = {}

    def __init__(self, platform: FakeChatPlatform | None = None, *, team_id: str = "T1") -> None:
        self.platform = platform or SlackPlatform("slack", max_len=MAX_LEN, bot_user_id="U0BOT")
        self.team_id = team_id
        self.client = FakeWebClient(self)
        self.app = FakeApp(self)
        self.ephemeral: list[tuple[str, str, str]] = []
        self._envelopes = itertools.count(1)

    def handler(self, app_token: str) -> FakeSocketModeHandler:
        return FakeSocketModeHandler(self.app, app_token)

    def payloads(self, item: FakeInbound) -> list[tuple[str, dict[str, Any]]]:
        """Events API payloads for one inbound update (a mention yields two sharing one ts)."""
        team = item.scope_id or self.team_id
        ts = _ts(item)
        if item.event == "button":
            body = {
                "type": "block_actions",
                "team": {"id": team},
                "user": {"id": item.user_id},
                "channel": {"id": item.chat_id},
                "actions": [{"action_id": item.button_data, "value": item.button_data}],
                "message": {"ts": _as_ts(item.message_id)},
            }
            return [("action", body)]
        event: dict[str, Any] = {
            "type": "message",
            "channel": item.chat_id,
            "channel_type": "im" if item.chat_kind == "dm" else "channel",
            "text": item.text,
            "ts": ts,
            "team": team,
        }
        if item.is_bot:
            event.update(subtype="bot_message", bot_id=item.user_id)
        else:
            event["user"] = item.user_id
        if item.thread_id:
            event["thread_ts"] = _as_ts(item.thread_id)
        if item.event == "edited":
            event = {
                "type": "message",
                "subtype": "message_changed",
                "channel": item.chat_id,
                "message": {**event, "edited": {"ts": ts}},
                "ts": ts,
                "team": team,
            }
        if item.media:
            event["files"] = [
                {"name": m.ref, "mimetype": m.mime, "url_private": f"https://files.example/{m.ref}"}
                for m in item.media
            ]
        out = [("message", event)]
        if item.mentions_bot and item.event == "message":
            out.append(("app_mention", {**event, "type": "app_mention"}))
        return out

    async def dispatch_pending(self) -> int:
        delivered = 0
        for item in self.platform.drain_inbound():
            for kind, body in self.payloads(item):
                if kind == "action":
                    action_id = body["actions"][0]["action_id"]
                    handler = self.app.action_for(action_id)
                    if handler is not None:
                        await handler(body=body, ack=_ack)
                else:
                    handler = self.app.events.get(kind)
                    if handler is not None:
                        await handler(event=body, say=None)
            delivered += 1
        return delivered


async def _ack(*args: Any, **kwargs: Any) -> None:
    return None


SlackWire.FAKES = {
    "slack_sdk.web.async_client:AsyncWebClient": FakeWebClient,
    "slack_sdk.errors:SlackApiError": SlackApiError,
    "slack_bolt.app.async_app:AsyncApp": FakeApp,
    "slack_bolt.adapter.socket_mode.async_handler:AsyncSocketModeHandler": FakeSocketModeHandler,
}

__all__ = [
    "SURFACE",
    "FakeApp",
    "FakeSocketModeHandler",
    "FakeWebClient",
    "SlackApiError",
    "SlackPlatform",
    "SlackResponse",
    "SlackWire",
]
