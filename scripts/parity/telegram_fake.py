"""A loopback stand-in for the Telegram Bot API, for the gateway parity flow (P25-05).

The real ``indus gateway run`` reaches it through the Telegram adapter's ``base_url`` setting, so
the product side runs end to end without the network. Written from the public Bot API method
descriptions; standard library only. It keeps:

- an inbound queue served by ``getUpdates`` (long poll, capped at one second so shutdown is quick);
- an outbound log of every message the bot sent or edited, with its inline buttons; the parity
  driver waits on this log and presses buttons through ``press``.

Methods it does not model answer ``{"ok": true, "result": true}``.
"""

from __future__ import annotations

import itertools
import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs

BOT_ID = 1000
DATE = 1_760_000_000
POLL_CAP_SECONDS = 1.0


@dataclass
class Sent:
    """One message the bot sent (``op`` "send") or changed (``op`` "edit")."""

    op: str
    chat_id: str
    message_id: int
    text: str
    buttons: list[dict[str, str]] = field(default_factory=list[dict[str, str]])

    def as_dict(self) -> dict[str, Any]:
        return {
            "op": self.op,
            "chat_id": self.chat_id,
            "message_id": self.message_id,
            "text": self.text,
            "buttons": list(self.buttons),
        }


def _buttons(markup: Any) -> list[dict[str, str]]:
    if isinstance(markup, str):
        try:
            markup = json.loads(markup)
        except ValueError:
            return []
    if not isinstance(markup, dict):
        return []
    rows: Any = markup.get("inline_keyboard") or []  # pyright: ignore[reportUnknownMemberType]
    out: list[dict[str, str]] = []
    for row in rows:
        for button in row if isinstance(row, list) else []:  # pyright: ignore[reportUnknownVariableType]
            if isinstance(button, dict):
                out.append(
                    {
                        "text": str(button.get("text", "")),  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                        "data": str(button.get("callback_data", "")),  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                    }
                )
    return out


class FakeBotApi:
    """The fake service: updates in, messages out, on ``127.0.0.1:<port>``."""

    def __init__(self, user_id: int = 200, username: str = "parity_owner") -> None:
        self.user = {"id": user_id, "is_bot": False, "first_name": "Parity", "username": username}
        self.changed = threading.Condition()
        self.updates: list[dict[str, Any]] = []
        self.sent: list[Sent] = []
        self.calls: list[str] = []
        self._ids = itertools.count(1)
        self._update_ids = itertools.count(1)
        self._httpd: ThreadingHTTPServer | None = None
        self.port = 0

    # -- lifecycle --

    def start(self) -> int:
        handler = type("FakeBotHandler", (_Handler,), {"owner": self})
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._httpd.daemon_threads = True
        self.port = int(self._httpd.server_address[1])
        threading.Thread(target=self._httpd.serve_forever, name="fake-bot-api", daemon=True).start()
        return self.port

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        with self.changed:
            self.changed.notify_all()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # -- what the driver does --

    def say(self, text: str, chat_id: int | None = None) -> None:
        """A direct message from the allowed person."""
        chat = chat_id if chat_id is not None else int(self.user["id"])
        message = {
            "message_id": next(self._ids),
            "from": dict(self.user),
            "chat": {"id": chat, "type": "private", "first_name": "Parity"},
            "date": DATE,
            "text": text,
        }
        self._push({"message": message})

    def press(self, sent: Sent, data: str) -> None:
        """The person presses the button carrying ``data`` under ``sent``."""
        query = {
            "id": f"cbq-{next(self._ids)}",
            "from": dict(self.user),
            "chat_instance": "parity",
            "data": data,
            "message": {
                "message_id": sent.message_id,
                "from": {"id": BOT_ID, "is_bot": True, "first_name": "Indus"},
                "chat": {"id": int(sent.chat_id), "type": "private"},
                "date": DATE,
                "text": sent.text,
            },
        }
        self._push({"callback_query": query})

    def _push(self, body: dict[str, Any]) -> None:
        with self.changed:
            self.updates.append({"update_id": next(self._update_ids), **body})
            self.changed.notify_all()

    # -- what the bot does --

    def call(self, method: str, params: Mapping[str, Any]) -> Any:
        with self.changed:
            self.calls.append(method)
        if method == "getMe":
            return {
                "id": BOT_ID,
                "is_bot": True,
                "first_name": "Indus",
                "username": "indus_parity_bot",
            }
        if method == "getUpdates":
            return self._get_updates(params)
        if method == "sendMessage":
            return self._record("send", params, next(self._ids))
        if method in ("editMessageText", "editMessageReplyMarkup"):
            return self._record("edit", params, int(params.get("message_id") or 0))
        return True

    def _get_updates(self, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        offset = int(params.get("offset") or 0)
        wait = min(float(params.get("timeout") or 0), POLL_CAP_SECONDS)
        with self.changed:
            ready = lambda: [u for u in self.updates if u["update_id"] >= offset]  # noqa: E731
            if not ready() and wait > 0:
                self.changed.wait(wait)
            return ready()

    def _record(self, op: str, params: Mapping[str, Any], message_id: int) -> dict[str, Any]:
        chat = str(params.get("chat_id", ""))
        text = str(params.get("text", ""))
        entry = Sent(op, chat, message_id, text, _buttons(params.get("reply_markup")))
        with self.changed:
            self.sent.append(entry)
            self.changed.notify_all()
        chat_value: Any = int(chat) if chat.lstrip("-").isdigit() else chat
        return {
            "message_id": message_id,
            "from": {"id": BOT_ID, "is_bot": True, "first_name": "Indus"},
            "chat": {"id": chat_value, "type": "private"},
            "date": DATE,
            "text": text,
        }

    def snapshot(self) -> list[dict[str, Any]]:
        with self.changed:
            return [s.as_dict() for s in self.sent]


class _Handler(BaseHTTPRequestHandler):
    owner: FakeBotApi
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        return

    def _params(self) -> dict[str, Any]:
        size = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(size) if size else b""
        kind = (self.headers.get("content-type") or "").split(";")[0].strip()
        if kind == "application/json" and raw:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}  # pyright: ignore[reportUnknownVariableType]
        if kind == "application/x-www-form-urlencoded" and raw:
            return {k: v[-1] for k, v in parse_qs(raw.decode("utf-8")).items()}
        query = self.path.partition("?")[2]
        return {k: v[-1] for k, v in parse_qs(query).items()}

    def _answer(self, status: int, body: Mapping[str, Any]) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        try:
            self.end_headers()
            self.wfile.write(payload)
        except OSError:  # the gateway hung up mid long-poll (shutdown): nothing to answer
            self.close_connection = True

    def _route(self) -> None:
        path = self.path.split("?", 1)[0]
        parts = [p for p in path.split("/") if p]
        if len(parts) != 2 or not parts[0].startswith("bot"):
            self._answer(404, {"ok": False, "error_code": 404, "description": "Not Found"})
            return
        try:
            result = self.owner.call(parts[1], self._params())
        except (ValueError, TypeError) as exc:
            self._answer(
                400, {"ok": False, "error_code": 400, "description": f"Bad Request: {exc}"}
            )
            return
        self._answer(200, {"ok": True, "result": result})

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        self._route()

    def do_POST(self) -> None:  # noqa: N802 - stdlib name
        self._route()


__all__ = ["BOT_ID", "FakeBotApi", "Sent"]
