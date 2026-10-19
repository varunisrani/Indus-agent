"""A bounded memory of what the gateway sent, for reply context (P15-03).

Some platforms do not echo a rich message's text when a user replies to it (Telegram) or quote
only its id. The outbound sender records each delivered message here; inbound replies look the
id up to recover the quoted text and to learn that the reply targets one of our own messages.

Bounded twice: per chat the least recently used ids go first beyond ``per_chat``, and beyond
``max_chats`` the least recently used chat is dropped whole. Texts are cut to ``max_text_bytes``
of UTF-8 on a character boundary. Memory only: nothing is written to disk.
"""

from __future__ import annotations

import threading
from collections import OrderedDict

DEFAULT_PER_CHAT = 2000
DEFAULT_MAX_TEXT_BYTES = 4096
DEFAULT_MAX_CHATS = 1000

_ChatKey = tuple[str, str]


def _cut(text: str, limit: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", errors="ignore")


class SentIndex:
    def __init__(
        self,
        *,
        per_chat: int = DEFAULT_PER_CHAT,
        max_text_bytes: int = DEFAULT_MAX_TEXT_BYTES,
        max_chats: int = DEFAULT_MAX_CHATS,
    ) -> None:
        if per_chat < 1 or max_chats < 1 or max_text_bytes < 0:
            raise ValueError("SentIndex bounds must be positive")
        self.per_chat = per_chat
        self.max_text_bytes = max_text_bytes
        self.max_chats = max_chats
        self._chats: OrderedDict[_ChatKey, OrderedDict[str, str]] = OrderedDict()
        self._lock = threading.Lock()

    def record(self, platform: str, chat_id: str, message_id: str, text: str) -> None:
        key = (platform, str(chat_id))
        with self._lock:
            entries = self._chats.get(key)
            if entries is None:
                entries = OrderedDict()
                self._chats[key] = entries
                while len(self._chats) > self.max_chats:
                    self._chats.popitem(last=False)
            else:
                self._chats.move_to_end(key)
            entries[str(message_id)] = _cut(text or "", self.max_text_bytes)
            entries.move_to_end(str(message_id))
            while len(entries) > self.per_chat:
                entries.popitem(last=False)

    def _touch(self, platform: str, chat_id: str, message_id: str) -> str | None:
        key = (platform, str(chat_id))
        entries = self._chats.get(key)
        if entries is None:
            return None
        text = entries.get(str(message_id))
        if text is None:
            return None
        entries.move_to_end(str(message_id))
        self._chats.move_to_end(key)
        return text

    def lookup(self, platform: str, chat_id: str, message_id: str) -> str | None:
        """The recorded text (None when unknown or recorded empty)."""
        with self._lock:
            return self._touch(platform, chat_id, message_id) or None

    def is_own(self, platform: str, chat_id: str, message_id: str) -> bool:
        with self._lock:
            return self._touch(platform, chat_id, message_id) is not None

    def chats(self) -> list[_ChatKey]:
        with self._lock:
            return list(self._chats)

    def __len__(self) -> int:
        with self._lock:
            return sum(len(entries) for entries in self._chats.values())


__all__ = ["DEFAULT_MAX_CHATS", "DEFAULT_MAX_TEXT_BYTES", "DEFAULT_PER_CHAT", "SentIndex"]
