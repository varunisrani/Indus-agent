"""The intake's reply port (P15-11's ``ReplySender``) over the outbound sender (P15-17).

Every reply the admission pipeline, the busy guards and the chat commands produce goes through
``PlatformReplies.reply``: it picks the platform's `OutboundSender`, so formatting, chunking,
redaction, silence tokens, ``reply_to_mode``, dead targets and the sent index apply to all of them.
How each kind maps onto a `FinalReply`:

- ``final`` (a turn's answer, a command's long answer) and ``command``: a reply to the person;
- ``notice`` (pairing, refusals, home-channel notes): the same, and with no message to reply to
  it counts as unsolicited, so a chat known to be gone is skipped;
- ``ack`` (busy acknowledgements): an ephemeral system notice, deleted after
  ``display.ephemeral_system_ttl`` where the platform can delete.

A turn's final also carries its ledger key (``session_key``, ``message_ref``), so a sender with a
delivery ledger (P15-19) records it before sending.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from indus.gateway.core.admission import ReplyKind
from indus.gateway.core.events import Source
from indus.gateway.outbound.sender import DeliveryReport, FinalReply, OutboundSender

__all__ = ["PlatformReplies", "final_reply_for"]

log = logging.getLogger(__name__)


def final_reply_for(
    source: Source,
    text: str,
    *,
    reply_to: str | None,
    kind: ReplyKind,
    session_key: str | None = None,
    message_ref: str | None = None,
) -> FinalReply:
    """The `FinalReply` for one intake reply of ``kind`` (see the module docstring)."""
    final = kind == "final"
    return FinalReply(
        text,
        reply_to=reply_to,
        thread_id=source.thread_id,
        unsolicited=kind == "notice" and reply_to is None,
        ephemeral=kind == "ack",
        session_key=session_key if final else None,
        message_ref=message_ref if final else None,
    )


class PlatformReplies:
    """``ReplySender`` for the intake: one `OutboundSender` per connected platform."""

    def __init__(self, senders: Mapping[str, OutboundSender]) -> None:
        self._senders = {name.lower(): sender for name, sender in senders.items()}

    def sender_for(self, platform: str) -> OutboundSender | None:
        return self._senders.get(platform.lower())

    def add(self, platform: str, sender: OutboundSender) -> None:
        """Use ``sender`` for ``platform`` (an adapter connected or reconnected)."""
        self._senders[platform.lower()] = sender

    def remove(self, platform: str) -> OutboundSender | None:
        return self._senders.pop(platform.lower(), None)

    async def reply(
        self,
        source: Source,
        text: str,
        *,
        reply_to: str | None = None,
        kind: ReplyKind = "final",
        session_key: str | None = None,
        message_ref: str | None = None,
    ) -> DeliveryReport | None:
        sender = self.sender_for(str(source.platform))
        if sender is None:
            log.warning("no outbound sender for %s; a %s reply was dropped", source.platform, kind)
            return None
        reply = final_reply_for(
            source,
            text,
            reply_to=reply_to,
            kind=kind,
            session_key=session_key,
            message_ref=message_ref,
        )
        return await sender.deliver(str(source.chat_id), reply)

    async def aclose(self) -> None:
        """Cancel pending ephemeral deletions on every sender."""
        for sender in self._senders.values():
            await sender.aclose()
