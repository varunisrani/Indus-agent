"""A gateway intake wired to the fake platform and the fake model (P15-11..13 tests).

``build_kit`` makes a real ``SessionService`` (fake model, a real store in the test's ``work``
profile), the authorization chain over the built-in registry with an env dict, the pairing store,
a fake Telegram-shaped adapter over ``FakeChatPlatform`` and the intake between them. Replies go
through the real reply port (``PlatformReplies`` over the P15-17 ``OutboundSender``) to the fake
wire; ``RecordingReplies`` also keeps what the intake asked to send, by kind.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from indus.core.persist import StateStore
from indus.foundation.scope import Scope
from indus.gateway.adapters.base import Adapter, Capabilities
from indus.gateway.core.admission import AdmissionPipeline, IntakeSettings, ModelChoice
from indus.gateway.core.authz import Authorizer, UnauthorizedHandler
from indus.gateway.core.events import Attachment, InboundEvent, MessageKind, Source
from indus.gateway.core.send import SendResult
from indus.gateway.outbound.replies import PlatformReplies
from indus.gateway.outbound.sender import DeliveryReport, OutboundSender
from indus.gateway.registry import PlatformRegistry
from indus.service.session_service import SessionService
from indus.state import registry as store_registry
from indus.state.pairing import PairingStore
from indus.state.registry import StoreHandle
from tests.fakes.agent import fake_options
from tests.fakes.clock import FakeClock, checkpoint
from tests.fakes.model import FakeModelServer, Turn, fake_route, script_of
from tests.fakes.platform.core import FakeChatPlatform, FakeInbound

ALLOWED = "200"
_KINDS = {"photo": MessageKind.PHOTO, "voice": MessageKind.VOICE, "document": MessageKind.DOCUMENT}


class FakeChatAdapter(Adapter):
    """A Telegram-named adapter over the fake platform (no wire library)."""

    platform: ClassVar[str] = "telegram"
    capabilities: ClassVar[Capabilities] = Capabilities(max_len=4096)

    def __init__(self, fake: FakeChatPlatform, **kw: Any) -> None:
        super().__init__(**kw)
        self.fake = fake
        self._reader: asyncio.Task[None] | None = None

    async def _open(self, is_reconnect: bool) -> None:
        await self.fake.connect()
        self._reader = asyncio.ensure_future(self._read())

    async def _close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
        await self.fake.disconnect()

    async def _read(self) -> None:
        while True:
            item = await self.fake.receive_inbound()
            await self.emit(normalise(item, self.platform))

    async def _send(
        self, chat_id: str, text: str, reply_to: str | None, meta: dict[str, Any]
    ) -> SendResult:
        message = await self.fake.post(
            chat_id, text, reply_to=reply_to, thread_id=meta.get("thread_id")
        )
        return SendResult.sent(message.message_id)


def normalise(item: FakeInbound, platform: str = "telegram") -> InboundEvent:
    chat_type = "thread" if item.thread_id else item.chat_kind
    source = Source(
        platform=platform,
        chat_id=item.chat_id,
        chat_type=chat_type,
        chat_name=item.chat_name or None,
        user_id=item.user_id,
        user_name=item.user_name or None,
        thread_id=item.thread_id,
        is_bot=item.is_bot,
    )
    metadata: dict[str, Any] = {}
    if item.mentions_bot:
        metadata["mentioned"] = True
    media = tuple(Attachment(ref=m.ref, mime=m.mime) for m in item.media)
    kind = _KINDS.get(item.media[0].kind, MessageKind.TEXT) if item.media else MessageKind.TEXT
    return InboundEvent(
        source=source,
        text=item.text,
        kind=kind,
        message_id=item.message_id or None,
        media=media,
        reply_to_id=item.reply_to,
        metadata=metadata,
    )


def dm(text: str, user: str = ALLOWED, *, message_id: str = "m1", **kw: Any) -> InboundEvent:
    return InboundEvent(
        source=Source(platform="telegram", chat_id=user, chat_type="dm", user_id=user),
        text=text,
        message_id=message_id,
        **kw,
    )


@dataclass(frozen=True)
class Sent:
    kind: str
    platform: str
    chat_id: str
    text: str
    reply_to: str | None


class RecordingReplies(PlatformReplies):
    """The real reply port, keeping a record of every reply the intake asked for."""

    def __init__(self, senders: Mapping[str, OutboundSender]) -> None:
        super().__init__(senders)
        self.sent: list[Sent] = []

    async def reply(
        self,
        source: Source,
        text: str,
        *,
        reply_to: str | None = None,
        kind: Any = "final",
    ) -> DeliveryReport | None:
        platform = str(source.platform).lower()
        self.sent.append(Sent(kind, platform, str(source.chat_id), text, reply_to))
        return await super().reply(source, text, reply_to=reply_to, kind=kind)

    def texts(self, kind: str | None = None) -> list[str]:
        return [s.text for s in self.sent if kind is None or s.kind == kind]


@dataclass
class RecordingSwitcher:
    """A model switcher that records what each session was pointed at."""

    applied: list[tuple[str, str | None]] = field(default_factory=list[tuple[str, str | None]])
    refuse: set[str] = field(default_factory=set[str])

    async def check(self, service: SessionService, choice: ModelChoice) -> str | None:
        return "unknown model" if choice.model in self.refuse else None

    async def apply(
        self, service: SessionService, session_id: str, choice: ModelChoice | None
    ) -> None:
        self.applied.append((session_id, None if choice is None else choice.model))


@dataclass
class Kit:
    scope: Scope
    clock: FakeClock
    server: FakeModelServer
    service: SessionService
    store: StoreHandle
    fake: FakeChatPlatform
    adapter: FakeChatAdapter
    sender: RecordingReplies
    pipeline: AdmissionPipeline
    env: dict[str, str]
    knobs: dict[str, Any]
    switcher: RecordingSwitcher

    def set(self, **changes: Any) -> None:
        """Change intake settings (read on every message)."""
        self.knobs.update(changes)

    async def until(self, check: Callable[[], object], *, limit: float = 10.0) -> None:
        """Let tasks and threads run, moving the fake clock in small steps, until ``check()``."""
        loop = asyncio.get_running_loop()
        end = loop.time() + limit
        while not check():
            if loop.time() > end:
                raise AssertionError("the condition never held")
            await checkpoint()
            idle = loop.create_future()
            await asyncio.wait({idle}, timeout=0.002)
            idle.cancel()
            self.clock.advance(0.05)

    async def settle(self, rounds: int = 40) -> None:
        """Run for a while (moving the clock), for checks that something did NOT happen."""
        for _ in range(rounds):
            await checkpoint()
            idle = asyncio.get_running_loop().create_future()
            await asyncio.wait({idle}, timeout=0.002)
            idle.cancel()
            self.clock.advance(0.05)

    def key(self, user: str = ALLOWED) -> str:
        return self.pipeline.session_key(dm("x", user).source)

    def wire(self, chat_id: str = ALLOWED) -> list[str]:
        return self.fake.texts(chat_id)

    async def close(self) -> None:
        await self.pipeline.aclose()
        await self.sender.aclose()
        await self.adapter.disconnect()
        await self.service.aclose()
        self.store.close()


async def build_kit(
    scope: Scope,
    clock: FakeClock,
    tmp_path: Path,
    *turns: Turn,
    env: Mapping[str, str] | None = None,
    inline: Mapping[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
    settings: Mapping[str, Any] | None = None,
    **pipeline_kw: Any,
) -> Kit:
    server = FakeModelServer(script_of(*turns), clock=clock)
    store = store_registry.open_unshared(scope)
    options = fake_options(server, session_id=None, inline=dict(inline or {}))
    service = SessionService(
        scope,
        fake_route(server),
        toolsets=[],
        options=options,
        store=StateStore(store),
        lock_dir=tmp_path / "locks",
    )
    fake = FakeChatPlatform("telegram", clock=clock)
    adapter = FakeChatAdapter(fake, clock=clock)
    sender = RecordingReplies({"telegram": OutboundSender(adapter, scope=scope)})
    the_env = {"INDUS_TELEGRAM_ALLOWED_USERS": ALLOWED, **(env or {})}
    pairing = PairingStore(scope)
    authorizer = Authorizer.from_registry(
        PlatformRegistry(),
        env=the_env.get,
        extra_for=lambda _p: extra or {},
        is_paired=pairing.is_approved,
        profile="work",
    )
    knobs: dict[str, Any] = {"profile": "work", **(settings or {})}
    switcher = pipeline_kw.pop("switcher", None) or RecordingSwitcher()
    pipeline = AdmissionPipeline(
        scope=scope,
        service=service,
        sender=sender,
        store=store,
        authorizer=authorizer,
        unauthorized=UnauthorizedHandler(authorizer, pairing),
        adapters={"telegram": adapter},
        settings=lambda: IntakeSettings(**knobs),
        extra_for=lambda _p: extra or {},
        switcher=switcher,
        toolsets_for=lambda _p: None,
        **pipeline_kw,
    )
    pipeline.start()
    await adapter.connect()
    return Kit(
        scope,
        clock,
        server,
        service,
        store,
        fake,
        adapter,
        sender,
        pipeline,
        the_env,
        knobs,
        switcher,
    )


def model_inputs(server: FakeModelServer) -> list[str]:
    """The last user message of every model request, in order."""
    found: list[str] = []
    for request in server.requests:
        body = request.body if isinstance(request.body, dict) else {}
        messages = body.get("messages") or []
        users = [m for m in messages if isinstance(m, dict) and m.get("role") == "user"]
        if users:
            content = users[-1].get("content")
            found.append(content if isinstance(content, str) else str(content))
    return found


__all__ = [
    "ALLOWED",
    "FakeChatAdapter",
    "Kit",
    "RecordingReplies",
    "RecordingSwitcher",
    "Sent",
    "build_kit",
    "dm",
    "model_inputs",
    "normalise",
]
