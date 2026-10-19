"""A real adapter (P15-05 base) that records every outbound operation, for the outbound tests.

Text sends and edits are recorded at the public door, with the metadata the egress doors passed
(``interim`` / ``final`` marks included); what reached the hooks — the wire side — is kept apart in
``wire_meta`` so a test can prove the marks were taken off. Scripted results (``send_results``,
``edit_results``) are real ``SendResult`` values; a failure is classified by P15-04 from its text.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from indus.gateway.adapters.base import Adapter, Capabilities, LenFn, MediaKind, code_points
from indus.gateway.core.send import ErrorKind, SendResult
from indus.gateway.outbound.formatting import Markup
from tests.fakes.clock import FakeClock

# Media kinds this platform sends natively; the rest are refused and go out as documents.
NATIVE_MEDIA: frozenset[MediaKind] = frozenset({"image", "document", "voice"})


def fail(kind: str, error: str = "", retry_after: float | None = None) -> SendResult:
    """A failure whose text P15-04's classifier reads as ``kind``."""
    result = SendResult.failure(error or kind, retry_after=retry_after)
    assert result.kind == ErrorKind(kind), f"{error!r} classifies as {result.kind}, not {kind}"
    return result


@dataclass
class Op:
    op: str
    chat_id: str
    text: str
    message_id: str | None = None
    reply_to: str | None = None
    meta: dict[str, Any] = field(default_factory=dict[str, Any])
    at: float = 0.0

    @property
    def interim(self) -> bool:
        return bool(self.meta.get("interim"))

    @property
    def final(self) -> bool:
        return bool(self.meta.get("final"))


class RecordingAdapter(Adapter):
    platform = "stub"

    def __init__(self, *, clock: FakeClock | None = None) -> None:
        super().__init__(clock=clock or FakeClock(datetime(2026, 1, 1, tzinfo=UTC)))
        self.ops: list[Op] = []
        self.wire_meta: list[dict[str, Any]] = []
        self.send_results: deque[SendResult] = deque()
        self.edit_results: deque[SendResult] = deque()
        self.messages: dict[str, str] = {}
        self._next = 1

    def _op(self, op: str, chat_id: str, text: str, **kw: Any) -> Op:
        record = Op(op, chat_id, text, at=self.clock.monotonic(), **kw)
        self.ops.append(record)
        return record

    def _new_id(self) -> str:
        message_id = f"m{self._next}"
        self._next += 1
        return message_id

    async def _open(self, is_reconnect: bool) -> None:
        return None

    async def _close(self) -> None:
        return None

    # -- public doors (recorded with the caller's metadata) --------------------------------------

    async def send(
        self,
        chat_id: str,
        text: str,
        *,
        reply_to: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> SendResult:
        op = self._op("send", chat_id, text, reply_to=reply_to, meta=dict(meta or {}))
        result = await super().send(chat_id, text, reply_to=reply_to, meta=meta)
        if result.ok:
            op.message_id = result.message_id
        return result

    async def edit(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        *,
        final: bool = False,
        meta: Mapping[str, Any] | None = None,
    ) -> SendResult:
        self._op("edit", chat_id, text, message_id=message_id, meta=dict(meta or {}))
        return await super().edit(chat_id, message_id, text, final=final, meta=meta)

    # -- hooks (the wire side) --------------------------------------------------------------------

    async def _send(
        self, chat_id: str, text: str, reply_to: str | None, meta: dict[str, Any]
    ) -> SendResult:
        self.wire_meta.append(dict(meta))
        if self.send_results:
            scripted = self.send_results.popleft()
            if not scripted.ok:
                return scripted
        message_id = self._new_id()
        self.messages[message_id] = text
        return SendResult.sent(message_id)

    async def _edit(
        self, chat_id: str, message_id: str, text: str, final: bool, meta: dict[str, Any]
    ) -> SendResult:
        self.wire_meta.append(dict(meta))
        if self.edit_results:
            scripted = self.edit_results.popleft()
            if not scripted.ok:
                return scripted
        self.messages[message_id] = text
        return SendResult.sent(message_id)

    async def _delete(self, chat_id: str, message_id: str) -> bool:
        self._op("delete", chat_id, "", message_id=message_id)
        self.messages.pop(message_id, None)
        return True

    async def _typing(self, chat_id: str, meta: dict[str, Any]) -> None:
        self._op("typing", chat_id, "")

    async def _typing_stop(self, chat_id: str) -> None:
        self._op("typing_stop", chat_id, "")

    async def _send_media(
        self, kind: MediaKind, chat_id: str, path: str, caption: str | None, meta: dict[str, Any]
    ) -> SendResult:
        if kind not in NATIVE_MEDIA:
            return SendResult.refused(f"sending {kind}s")
        self.wire_meta.append(dict(meta))
        wire = dict(meta)
        reply_to = wire.pop("reply_to", None)
        self._op(kind, chat_id, path, reply_to=reply_to, meta=wire)
        if self.send_results:
            scripted = self.send_results.popleft()
            if not scripted.ok:
                return scripted
        return SendResult.sent(self._new_id())

    # -- views ------------------------------------------------------------------------------------

    def of(self, *kinds: str) -> list[Op]:
        return [op for op in self.ops if op.op in kinds]

    def visible(self) -> list[str]:
        """Message texts as they now stand, in send order."""
        return [
            self.messages[op.message_id]
            for op in self.of("send")
            if op.message_id is not None and op.message_id in self.messages
        ]


def recording_adapter(
    *,
    name: str = "stub",
    max_len: int = 4096,
    len_fn: LenFn = code_points,
    markup: Markup = "markdown",
    supports_edit: bool = True,
    supports_delete: bool = True,
    clock: FakeClock | None = None,
) -> RecordingAdapter:
    """A `RecordingAdapter` whose class carries the chosen platform name and capabilities."""
    caps = Capabilities(
        max_len=max_len,
        len_fn=len_fn,
        markup=markup,
        supports_edit=supports_edit,
        supports_delete=supports_delete,
    )
    attrs = {"capabilities": caps, "platform": name}
    cls = cast(type[RecordingAdapter], type("RecordingAdapter", (RecordingAdapter,), attrs))
    return cls(clock=clock)
