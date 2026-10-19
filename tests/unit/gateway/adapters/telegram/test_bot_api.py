"""P15-33: our Telegram Bot API client over ``httpx.MockTransport`` (no network).

The bot token travels in every request path; these tests pin that it reaches the wire and
nothing else: not httpx's request log, not an exception text, not a repr.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from indus.gateway.adapters.telegram import bot_api
from indus.gateway.adapters.telegram.bot_api import (
    BadRequest,
    BotApi,
    BotCommand,
    Conflict,
    Forbidden,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    NetworkError,
    RetryAfter,
    TelegramError,
    TimedOut,
    Unauthorized,
)

# Built at runtime: the Telegram token shape never appears literally in the source.
TOKEN = "".join(["98765", "4321", ":", "AA", "Hk" * 16, "z"])

type Handler = Callable[[httpx.Request], httpx.Response]


def ok(result: Any) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


def refused(status: int, description: str, **parameters: Any) -> httpx.Response:
    body: dict[str, Any] = {"ok": False, "error_code": status, "description": description}
    if parameters:
        body["parameters"] = parameters
    return httpx.Response(status, json=body)


class Wire:
    """A mock Bot API: records what reached the wire, answers with ``handler``."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.seen: list[httpx.Request] = []
        self.bodies: list[bytes] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        self.bodies.append(request.read())
        return self.handler(request)

    def client(self, **kw: Any) -> BotApi:
        return BotApi(TOKEN, transport=httpx.MockTransport(self), **kw)

    def method(self, n: int = -1) -> str:
        return self.seen[n].url.path.rsplit("/", 1)[-1]

    def json(self, n: int = -1) -> dict[str, Any]:
        return json.loads(self.bodies[n])


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def me() -> dict[str, Any]:
    return {"id": 1000, "is_bot": True, "first_name": "Indus", "username": "indus_bot"}


def test_the_token_reaches_the_wire_but_no_log_repr_or_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    # an earlier test's log setup may have pinned the HTTP libraries' loggers at WARNING
    for noisy in ("httpx", "httpcore"):
        caplog.set_level(logging.DEBUG, logger=noisy)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ok(me())
        if calls == 2:
            # a transport error whose own text names the full URL, token included
            raise httpx.ConnectError(f"cannot reach {request.url}", request=request)
        return refused(400, "Bad Request: chat not found")

    wire = Wire(handler)
    api = wire.client()

    async def main() -> list[str]:
        texts: list[str] = []
        user = await api.get_me()
        assert user.id == 1000 and user.username == "indus_bot"
        for _ in range(2):
            try:
                await api.send_message(42, "hi")
            except TelegramError as exc:
                texts.append(str(exc))
                # no chained httpx error (its request carries the real URL) is ever shown
                assert exc.__cause__ is None
                assert exc.__context__ is None or exc.__suppress_context__
        await api.aclose()
        return texts

    texts = run(main())
    assert all(f"/bot{TOKEN}/" in str(r.url) for r in wire.seen)  # the real path on the wire
    assert texts[0].startswith("ConnectError") and "****" in texts[0]
    assert texts[1] == "Bad Request: chat not found"
    assert TOKEN not in caplog.text and "/bot****/getMe" in caplog.text  # httpx's request log
    assert all(TOKEN not in t for t in texts)
    assert TOKEN not in repr(api) and repr(api) == "<BotApi api.telegram.org>"
    assert TOKEN not in str(vars(api).get("_client"))


def test_a_malformed_token_is_refused_without_echoing_it() -> None:
    bad = "12" + "/" + "x" * 10
    with pytest.raises(ValueError) as caught:
        BotApi(bad)
    assert bad not in str(caught.value)


@pytest.mark.parametrize(
    ("response", "error", "check"),
    [
        (
            refused(429, "Too Many Requests: retry after 5820", retry_after=5820),
            RetryAfter,
            lambda e: e.retry_after == 5820,
        ),
        (refused(400, "Bad Request: can't parse entities"), BadRequest, None),
        (refused(401, "Unauthorized"), Unauthorized, None),
        (refused(404, "Not Found"), Unauthorized, None),
        (refused(403, "Forbidden: bot was blocked by the user"), Forbidden, None),
        (refused(409, "Conflict: terminated by other getUpdates request"), Conflict, None),
        (httpx.Response(502, text="<html>bad gateway</html>"), NetworkError, None),
    ],
)
def test_bot_api_errors_map_to_our_classes(
    response: httpx.Response, error: type[TelegramError], check: Any
) -> None:
    wire = Wire(lambda request: response)

    async def main() -> None:
        with pytest.raises(error) as caught:
            await wire.client().send_message(1, "x")
        assert type(caught.value) is error
        if check is not None:
            assert check(caught.value)

    run(main())


@pytest.mark.parametrize(
    ("raised", "error"),
    [
        (httpx.ConnectTimeout("slow connect"), NetworkError),
        (httpx.PoolTimeout("pool"), NetworkError),
        (httpx.ReadTimeout("slow answer"), TimedOut),
        (httpx.RemoteProtocolError("peer closed"), TimedOut),
        (httpx.ProxyError("proxy refused"), NetworkError),
    ],
)
def test_transport_errors_say_whether_the_request_may_have_landed(
    raised: Exception, error: type[TelegramError]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise raised

    async def main() -> None:
        with pytest.raises(error) as caught:
            await Wire(handler).client().send_message(1, "x")
        assert type(caught.value) is error

    run(main())


def test_send_message_wire_shape() -> None:
    sent = {"message_id": 7, "chat": {"id": -100123, "type": "supergroup", "is_forum": True}}
    wire = Wire(lambda request: ok(sent))
    markup = InlineKeyboardMarkup(((InlineKeyboardButton("Allow", "apv:r1:once"),),))

    async def main() -> None:
        message = await wire.client().send_message(
            "-100123",
            "*hi*",
            parse_mode="MarkdownV2",
            reply_to_message_id="55",
            message_thread_id=12,
            reply_markup=markup,
        )
        assert message.message_id == 7 and message.chat.is_forum

    run(main())
    assert wire.method() == "sendMessage"
    body = wire.json()
    assert body["chat_id"] == -100123 and body["parse_mode"] == "MarkdownV2"
    assert body["reply_parameters"] == {"message_id": 55, "allow_sending_without_reply": True}
    assert body["message_thread_id"] == 12
    assert body["reply_markup"] == {
        "inline_keyboard": [[{"text": "Allow", "callback_data": "apv:r1:once"}]]
    }
    assert "reply_to_message_id" not in body  # None values are never sent


def test_updates_are_parsed_into_typed_results() -> None:
    rows = [
        {
            "update_id": 10,
            "message": {
                "message_id": 3,
                "date": 1767258000,
                "chat": {"id": -200, "type": "supergroup", "title": "Ops", "is_forum": True},
                "from": {"id": 7, "is_bot": False, "first_name": "Ana", "last_name": "Li"},
                "message_thread_id": 12,
                "is_topic_message": True,
                "text": "@indus_bot status?",
                "entities": [{"type": "mention", "offset": 0, "length": 10}],
                "reply_to_message": {
                    "message_id": 2,
                    "chat": {"id": -200, "type": "supergroup"},
                    "from": {"id": 1000, "is_bot": True, "first_name": "Indus"},
                    "text": "earlier",
                },
                "quote": {"text": "earl"},
            },
        },
        {
            "update_id": 11,
            "message": {
                "message_id": 4,
                "chat": {"id": 7, "type": "private", "first_name": "Ana"},
                "from": {"id": 7, "first_name": "Ana"},
                "photo": [
                    {"file_id": "small", "file_unique_id": "s", "width": 90, "height": 90},
                    {"file_id": "big", "file_unique_id": "b", "width": 900, "height": 900},
                ],
                "caption": "look",
                "media_group_id": "album1",
            },
        },
        {
            "update_id": 12,
            "callback_query": {
                "id": "cb1",
                "from": {"id": 7, "is_bot": False, "first_name": "Ana"},
                "data": "apv:r1:once",
                "message": {"message_id": 9, "chat": {"id": 7, "type": "private"}},
            },
        },
        "not an update",
    ]
    wire = Wire(lambda request: ok(rows))

    async def main() -> list[bot_api.Update]:
        return await wire.client().get_updates(
            offset=10, timeout=30, allowed_updates=("message", "callback_query")
        )

    topic, photo, press = run(main())
    assert wire.json() == {
        "offset": 10,
        "timeout": 30,
        "allowed_updates": ["message", "callback_query"],
    }
    message = topic.message
    assert message is not None and message.message_thread_id == 12 and message.is_topic_message
    assert message.chat.is_forum and message.chat.title == "Ops"
    assert message.from_user is not None and message.from_user.full_name == "Ana Li"
    assert message.entities[0].type == "mention" and message.entities[0].length == 10
    assert message.reply_to_message is not None and message.quote_text == "earl"
    assert message.date is not None and message.date.year == 2026
    assert photo.message is not None and photo.message.photo[-1].file_id == "big"
    assert photo.message.media_group_id == "album1"
    assert press.callback_query is not None and press.callback_query.data == "apv:r1:once"


def test_uploads_are_multipart_and_other_methods_json(tmp_path: Path) -> None:
    picture = tmp_path / "chart.png"
    picture.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    answer = {"message_id": 5, "chat": {"id": 1, "type": "private"}}
    wire = Wire(lambda request: ok(answer if "send" in request.url.path else True))

    async def main() -> None:
        api = wire.client()
        await api.send_photo(1, InputFile(picture, "chart.png"), caption="Q3", message_thread_id=3)
        await api.send_document(1, "https://files.example/report.pdf")
        assert await api.set_my_commands(
            [BotCommand("help", "Show the commands")], scope={"type": "default"}
        )
        assert await api.set_message_reaction(1, 5, reaction="\N{EYES}")
        assert await api.set_message_reaction(1, 5)

    run(main())
    upload = wire.seen[0]
    assert upload.headers["content-type"].startswith("multipart/form-data")
    assert b'filename="chart.png"' in wire.bodies[0] and b"\x89PNG" in wire.bodies[0]
    assert b'name="message_thread_id"' in wire.bodies[0]
    assert wire.json(1)["document"] == "https://files.example/report.pdf"
    assert wire.json(2) == {
        "commands": [{"command": "help", "description": "Show the commands"}],
        "scope": {"type": "default"},
    }
    assert wire.json(3)["reaction"] == [{"type": "emoji", "emoji": "\N{EYES}"}]
    assert wire.json(4)["reaction"] == []


def test_file_download_streams_and_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getFile"):
            return ok({"file_id": "F", "file_path": "voice/file_1.oga", "file_size": 12})
        assert request.url.path == f"/file/bot{TOKEN}/voice/file_1.oga"
        return httpx.Response(200, content=b"x" * 12)

    wire = Wire(handler)

    async def fetch(api: BotApi, path: str) -> bytes:
        return b"".join([chunk async for chunk in api.download_file(path)])

    async def main() -> None:
        api = wire.client()
        info = await api.get_file("F")
        assert info.file_path == "voice/file_1.oga" and info.file_size == 12
        assert await fetch(api, "voice/file_1.oga") == b"x" * 12
        with pytest.raises(BadRequest):
            await fetch(api, "../../etc/passwd")
        monkeypatch.setattr(bot_api, "MAX_DOWNLOAD_BYTES", 10)
        with pytest.raises(BadRequest, match="too big"):
            await fetch(api, "voice/file_1.oga")

    run(main())


def test_the_client_can_be_reused_after_close() -> None:
    wire = Wire(lambda request: ok(me()))

    async def main() -> None:
        api = wire.client()
        await api.get_me()
        await api.aclose()
        await api.aclose()
        await api.get_me()
        await api.aclose()

    run(main())
    assert len(wire.seen) == 2


def test_long_polls_wait_longer_than_the_poll() -> None:
    timeouts: list[float | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        timeouts.append(request.extensions["timeout"]["read"])
        return ok([])

    async def main() -> None:
        api = Wire(handler).client(request_timeout=30.0)
        await api.get_updates(timeout=30)
        await api.get_me()

    run(main())
    assert timeouts == [60.0, 30.0]
