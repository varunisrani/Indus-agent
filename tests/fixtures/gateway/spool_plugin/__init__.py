"""A chat platform over spool files, for gateway process tests (P15-24..26).

A test drops one JSON file per inbound message into ``<dir>/inbound/`` (written under another
name, then renamed to ``*.json``); every send is appended as one JSON line to
``<dir>/outbound.jsonl``. ``dir`` comes from ``platforms.spool.dir`` in the profile's config.
While ``<dir>/die-on-send`` exists, a send kills the process before anything is written (a crash
between "reply produced" and "platform acknowledged").
The plugin offers the platform through the P13 provider slot ``platform``.
"""

import asyncio
import contextlib
import json
import os
from pathlib import Path

from indus.gateway.adapters.base import Adapter, Capabilities
from indus.gateway.core.events import InboundEvent, Source
from indus.gateway.core.send import SendResult
from indus.gateway.registry import PlatformEntry


class SpoolAdapter(Adapter):
    platform = "spool"
    capabilities = Capabilities(max_len=4096)

    def __init__(self, config):
        super().__init__(clock=config.clock, settings=config.settings)
        self.dir = Path(str(config.extra["dir"]))
        self._reader = None
        self._count = 0

    async def _open(self, is_reconnect):
        (self.dir / "inbound").mkdir(parents=True, exist_ok=True)
        self._reader = asyncio.ensure_future(self._read())

    async def _close(self):
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader

    async def _read(self):
        inbox = self.dir / "inbound"
        while True:
            for path in sorted(inbox.glob("*.json")):
                data = json.loads(path.read_text(encoding="utf-8"))
                path.unlink()
                source = Source(
                    platform="spool",
                    chat_id=str(data["chat_id"]),
                    chat_type="dm",
                    user_id=str(data["user_id"]),
                )
                event = InboundEvent(
                    source=source, text=data["text"], message_id=data.get("message_id")
                )
                await self.emit(event)
            await self.clock.sleep(0.05)

    async def _send(self, chat_id, text, reply_to, meta):
        if (self.dir / "die-on-send").exists():
            os._exit(70)
        self._count += 1
        message_id = f"out-{os.getpid()}-{self._count}"
        line = {"chat_id": chat_id, "text": text, "reply_to": reply_to, "id": message_id}
        data = (json.dumps(line) + "\n").encode("utf-8")
        fd = os.open(self.dir / "outbound.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        return SendResult.sent(message_id)


def create_adapter(config):
    return SpoolAdapter(config)


def register(ctx):
    entry = PlatformEntry(
        name="spool",
        label="Spool",
        factory=create_adapter,
        check_fn=lambda: True,
        allowlist_env="INDUS_SPOOL_ALLOWED_USERS",
        allow_all_env="INDUS_SPOOL_ALLOW_ALL_USERS",
        home_env="INDUS_SPOOL_HOME_CHANNEL",
        max_len=4096,
    )
    ctx.register_provider("platform", entry, name="spool")
