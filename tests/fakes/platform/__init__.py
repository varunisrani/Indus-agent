"""Fake messaging platforms for adapter tests.

``FakeChatPlatform`` (``core``) is the scriptable platform of P15-01: inbound injection, an
outbound log, failure injection and a fake clock; the wire fakes (``telegram_wire``,
``discord_wire``, ``slack_wire``) speak the vendor libraries' shapes on top of it.
``FakePlatform`` below is the minimal P00 recorder, kept for the tests that use it.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from tests.fakes.platform.core import (
    Button,
    FailureKind,
    FakeChatPlatform,
    FakeInbound,
    FakeMedia,
    FakeMessage,
    FakePlatformError,
    Op,
)


@dataclass(frozen=True)
class SentMessage:
    message_id: str
    chat_id: str
    text: str
    attachments: tuple[str, ...] = ()


@dataclass(frozen=True)
class Edit:
    chat_id: str
    message_id: str
    text: str


@dataclass(frozen=True)
class InboundEvent:
    chat_id: str
    user_id: str
    text: str
    attachments: tuple[str, ...] = ()
    extra: dict[str, str] = field(default_factory=dict)


class FakePlatform:
    def __init__(self, name: str = "fake") -> None:
        self.name = name
        self.sent: list[SentMessage] = []
        self.edits: list[Edit] = []
        self._inbound: deque[InboundEvent] = deque()
        self._next_id = 1

    # outbound ------------------------------------------------------------------------------

    async def send(self, chat_id: str, text: str, attachments: tuple[str, ...] = ()) -> str:
        message_id = f"m{self._next_id}"
        self._next_id += 1
        self.sent.append(SentMessage(message_id, chat_id, text, tuple(attachments)))
        return message_id

    async def edit(self, chat_id: str, message_id: str, text: str) -> None:
        if not any(m.message_id == message_id and m.chat_id == chat_id for m in self.sent):
            raise KeyError(f"no message {message_id!r} was sent to chat {chat_id!r}")
        self.edits.append(Edit(chat_id, message_id, text))

    def texts(self, chat_id: str | None = None) -> list[str]:
        """Sent texts with edits applied, in send order."""
        latest = {(e.chat_id, e.message_id): e.text for e in self.edits}
        return [
            latest.get((m.chat_id, m.message_id), m.text)
            for m in self.sent
            if chat_id is None or m.chat_id == chat_id
        ]

    # inbound -------------------------------------------------------------------------------

    def push_inbound(self, *events: InboundEvent) -> None:
        self._inbound.extend(events)

    async def receive(self) -> InboundEvent | None:
        """Next scripted inbound event, or None once the script is used up."""
        return self._inbound.popleft() if self._inbound else None


__all__ = [
    "Button",
    "Edit",
    "FailureKind",
    "FakeChatPlatform",
    "FakeInbound",
    "FakeMedia",
    "FakeMessage",
    "FakePlatform",
    "FakePlatformError",
    "InboundEvent",
    "Op",
    "SentMessage",
]
