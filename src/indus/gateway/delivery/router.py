"""The delivery router: platform deliverers for cron, ``indus send`` and mirror records (P15-20).

The target grammar and ``DeliveryTarget`` belong to P14 (``indus.cron.targets``) and are used as
they are; nothing here parses a target string. When a platform's adapter connects, ``attach``
registers a deliverer for it with P14's ``cron.delivery.register_deliverer`` (a reconnect simply
registers again), plus one for ``all`` while any platform is attached; ``detach`` (a fatal
disconnect) and ``close`` take them away.

How a target is resolved, at send time:

- ``<platform>`` → that platform's home channel; none set → ``no_home_channel``;
- ``<platform>:<chat>[:<thread>]`` → that chat; a chat written as ``#name`` is looked up in the
  channel directory (not found → ``unknown_channel``);
- ``origin`` → already turned into a concrete target by P14 from the job's stored origin;
- ``all`` → the home channel of every platform attached at that moment (platforms without one
  are reported, not fatal unless none is left).

The transport is the platform's connected native adapter (a relay transport arrives in P24); no
adapter → ``platform_not_connected``. Sends are unsolicited deliveries through the platform's
``OutboundSender``: chats proven dead are skipped, the text is masked, formatted and chunked, and
media tags in it are never followed — only trusted files the caller passes are attached (cron's
checked attachments, ``indus send --file`` after the media path policy). Cron output longer than
``MAX_PLATFORM_OUTPUT`` on a platform whose adapter does not split natively is cut with a pointer to
the saved output file. A delivered message leaves a mirror note for the chat's conversation.

The router is a library for the gateway and the CLI; it is never offered to the model as a tool
(R02.26, D-16.37).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from indus.cron import delivery as cron_delivery
from indus.cron import targets
from indus.cron.delivery import DeliveryReceipt, DeliveryRequest
from indus.cron.targets import DeliveryTarget
from indus.foundation.scope import Scope
from indus.gateway.config import HomeChannel
from indus.gateway.delivery.directory import ChannelDirectory
from indus.gateway.delivery.mirror import Mirror
from indus.gateway.outbound.media_policy import MediaItem, MediaRef
from indus.gateway.outbound.sender import DeliveryReport, FinalReply, OutboundSender

__all__ = [
    "MAX_PLATFORM_OUTPUT",
    "DeliveryRouter",
    "Resolved",
    "cut_output",
    "trusted_files",
]

log = logging.getLogger(__name__)

MAX_PLATFORM_OUTPUT: Final = 4000
"""Cron output above this many characters is cut on platforms that do not split natively."""

type HomeLookup = Callable[[str], HomeChannel | None]


@dataclass(frozen=True)
class Resolved:
    platform: str
    chat_id: str
    thread_id: str | None = None


def cut_output(text: str, output_path: Path) -> str:
    """``text`` cut to ``MAX_PLATFORM_OUTPUT`` with a footer naming the full output file."""
    if len(text) <= MAX_PLATFORM_OUTPUT:
        return text
    footer = f"\n\n[… cut here: the whole output is saved in {output_path}]"
    return text[: max(0, MAX_PLATFORM_OUTPUT - len(footer))].rstrip() + footer


def trusted_files(paths: Sequence[Path]) -> tuple[MediaItem, ...]:
    """Files the caller already cleared, as attachments (the kind follows the extension)."""
    items: list[MediaItem] = []
    for path in paths:
        ref = MediaRef(str(path))
        items.append(MediaItem(str(path), ref.kind, ref.name))
    return tuple(items)


def _receipt(report: DeliveryReport) -> DeliveryReceipt:
    match report.outcome:
        case "sent":
            last = report.message_ids[-1] if report.message_ids else None
            return DeliveryReceipt(ok=True, verified=last is not None, message_id=last)
        case "skipped_dead":
            return DeliveryReceipt(
                ok=False, error="chat_unreachable: earlier sends proved this chat unreachable"
            )
        case "silent" | "empty":
            return DeliveryReceipt(ok=True, verified=False)
        case _:
            failure = report.failure
            reason = failure.error if failure is not None and failure.error else "send failed"
            return DeliveryReceipt(ok=False, error=reason)


class DeliveryRouter:
    """Platform deliverers over the gateway's outbound senders (see the module notes)."""

    def __init__(
        self,
        scope: Scope,
        *,
        homes: HomeLookup,
        directory: ChannelDirectory | None = None,
        mirror: Mirror | None = None,
    ) -> None:
        self.scope = scope
        self.homes = homes
        self.directory = directory
        self.mirror = mirror
        self._lock = threading.Lock()
        self._senders: dict[str, OutboundSender] = {}
        self._undo: dict[str, Callable[[], None]] = {}
        self._all_undo: Callable[[], None] | None = None

    # -- registration ----------------------------------------------------------------------------

    def attach(self, platform: str, sender: OutboundSender) -> None:
        """``platform``'s adapter connected: deliver to it through ``sender`` from now on."""
        name = platform.strip().lower()

        async def deliver(request: DeliveryRequest) -> DeliveryReceipt:
            return await self.deliver_request(name, request)

        # a reconnect registers again: the new deliverer replaces the old one in P14's table
        undo = cron_delivery.register_deliverer(name, deliver)
        with self._lock:
            self._senders[name] = sender
            self._undo[name] = undo
            need_all = self._all_undo is None
        if need_all:
            all_undo = cron_delivery.register_deliverer(targets.ALL, self._deliver_all)
            with self._lock:
                self._all_undo = all_undo

    def detach(self, platform: str) -> None:
        """``platform`` failed for good: stop delivering to it."""
        name = platform.strip().lower()
        with self._lock:
            self._senders.pop(name, None)
            undo = self._undo.pop(name, None)
            last = not self._senders
            all_undo = self._all_undo if last else None
            if last:
                self._all_undo = None
        if undo is not None:
            undo()
        if all_undo is not None:
            all_undo()

    def close(self) -> None:
        for name in self.attached():
            self.detach(name)

    def attached(self) -> list[str]:
        with self._lock:
            return sorted(self._senders)

    def sender_for(self, platform: str) -> OutboundSender | None:
        with self._lock:
            return self._senders.get(platform.strip().lower())

    # -- resolution ------------------------------------------------------------------------------

    def resolve(self, target: DeliveryTarget) -> Resolved | str:
        """Where ``target`` goes, or the error code and reason."""
        platform = target.platform.strip().lower()
        if platform in targets.RESERVED:
            return f"bad_target: {target.render()} is not a single chat"
        if target.chat_id is None:
            home = self.homes(platform)
            if home is None or not home.chat_id:
                return (
                    f"no_home_channel: {platform} has no home channel (set "
                    f"platforms.{platform}.home_channel, or send /sethome in the chat)"
                )
            return Resolved(platform, home.chat_id, target.thread_id or home.thread_id)
        chat = target.chat_id
        if chat.startswith("#"):
            found = self.directory.resolve(platform, chat) if self.directory else None
            if found is None:
                return f"unknown_channel: no chat named {chat} is known on {platform}"
            return Resolved(platform, found[0], target.thread_id or found[1])
        return Resolved(platform, chat, target.thread_id)

    # -- sending ---------------------------------------------------------------------------------

    async def send(
        self,
        target: DeliveryTarget,
        text: str,
        *,
        files: Sequence[MediaItem] = (),
        label: str = "a scheduled delivery",
        output_path: Path | None = None,
    ) -> DeliveryReceipt:
        """Deliver ``text`` (and cleared ``files``) to one target; ``all`` fans out."""
        if target.is_broadcast:
            return await self._broadcast(text, files, label, output_path)
        where = self.resolve(target)
        if isinstance(where, str):
            return DeliveryReceipt(ok=False, error=where)
        return await self._send_to(where, text, files, label, output_path)

    async def _send_to(
        self,
        where: Resolved,
        text: str,
        files: Sequence[MediaItem],
        label: str,
        output_path: Path | None,
    ) -> DeliveryReceipt:
        sender = self.sender_for(where.platform)
        if sender is None:
            return DeliveryReceipt(
                ok=False, error=f"platform_not_connected: {where.platform} is not connected"
            )
        body = text
        if output_path is not None and not sender.adapter.capabilities.splits_natively:
            body = cut_output(text, output_path)
        reply = FinalReply(
            body,
            thread_id=where.thread_id,
            unsolicited=True,
            reply_expected=False,
            allow_media=False,
            files=tuple(files),
        )
        try:
            report = await sender.deliver(where.chat_id, reply)
        except Exception as exc:
            log.warning("delivery to %s:%s failed", where.platform, where.chat_id, exc_info=True)
            return DeliveryReceipt(ok=False, error=f"{type(exc).__name__}: {exc}")
        receipt = _receipt(report)
        if receipt.ok and report.outcome == "sent" and self.mirror is not None:
            await self.mirror.record(where.platform, where.chat_id, where.thread_id, body, label)
        return receipt

    async def _broadcast(
        self,
        text: str,
        files: Sequence[MediaItem],
        label: str,
        output_path: Path | None,
    ) -> DeliveryReceipt:
        names = self.attached()
        if not names:
            return DeliveryReceipt(ok=False, error="no_platform: no platform is connected")
        problems: list[str] = []
        last: str | None = None
        sent = 0
        verified = True
        for name in names:
            where = self.resolve(DeliveryTarget(name))
            if isinstance(where, str):
                problems.append(where)
                continue
            receipt = await self._send_to(where, text, files, label, output_path)
            if not receipt.ok:
                problems.append(f"{name}: {receipt.error}")
                continue
            sent += 1
            verified = verified and receipt.verified
            last = receipt.message_id or last
        if sent == 0 or problems:
            return DeliveryReceipt(ok=False, error="; ".join(problems) or "nothing was sent")
        return DeliveryReceipt(ok=True, verified=verified, message_id=last)

    # -- P14 deliverers --------------------------------------------------------------------------

    @staticmethod
    def _label(request: DeliveryRequest) -> str:
        return f'the scheduled job "{request.job_name}"' if request.job_name else "a scheduled job"

    async def deliver_request(self, platform: str, request: DeliveryRequest) -> DeliveryReceipt:
        """The deliverer registered for ``platform`` (P14 hands in one target at a time)."""
        target = request.target
        if target.platform != platform:
            target = DeliveryTarget(platform, target.chat_id, target.thread_id)
        return await self.send(
            target,
            request.text,
            files=trusted_files(request.attachments),
            label=self._label(request),
            output_path=request.output_path,
        )

    async def _deliver_all(self, request: DeliveryRequest) -> DeliveryReceipt:
        return await self._broadcast(
            request.text,
            trusted_files(request.attachments),
            self._label(request),
            request.output_path,
        )
