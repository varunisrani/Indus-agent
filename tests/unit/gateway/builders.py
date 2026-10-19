"""Short builders for the real gateway models in tests: the inbound ``Source`` (P15-03) and an
adapter (P15-05) whose capability flags and own access policy a test chooses."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, Literal, cast

from indus.gateway.adapters.base import Adapter, Capabilities
from indus.gateway.core.events import ChatType, Source
from indus.gateway.core.send import SendResult
from tests.fakes.clock import FakeClock


def src(platform: str, chat_id: str | None, chat_type: ChatType = "dm", **fields: Any) -> Source:
    """``Source(platform=..., chat_id=..., chat_type=..., **fields)``; a None chat id is ``""``."""
    return Source(platform=platform, chat_id=chat_id or "", chat_type=chat_type, **fields)


def replace(source: Source, **changes: Any) -> Source:
    """``source`` with ``changes``, unvalidated (so a test can plant a non-bool flag)."""
    return source.model_copy(update=changes)


class PolicyAdapter(Adapter):
    """An adapter with nothing behind it but the access-policy hooks authorization reads."""

    platform = "telegram"

    def __init__(
        self,
        *,
        policies: dict[str, str] | None = None,
        dm_allow: Iterable[str] | None = None,
        resolved_ids: Iterable[str] = (),
    ) -> None:
        super().__init__(clock=FakeClock(datetime(2026, 1, 1, tzinfo=UTC)))
        self._policies = dict(policies or {})
        self._dm_allow = None if dm_allow is None else frozenset(dm_allow)
        self._resolved = frozenset(resolved_ids)

    async def _open(self, is_reconnect: bool) -> None:
        return None

    async def _close(self) -> None:
        return None

    async def _send(
        self, chat_id: str, text: str, reply_to: str | None, meta: dict[str, Any]
    ) -> SendResult:
        return SendResult.refused("sending")

    def access_policy(self, kind: Literal["dm", "group"]) -> str:
        return self._policies.get(kind, "")

    def allows_dm(self, user_id: str) -> bool:
        return self._dm_allow is not None and user_id in self._dm_allow

    def resolved_allowlist_ids(self) -> frozenset[str]:
        return self._resolved


def policy_adapter(
    *,
    auth_upstream: bool = False,
    owns_access_policy: bool = False,
    dm_is_private: bool = True,
    policies: dict[str, str] | None = None,
    dm_allow: Iterable[str] | None = None,
    resolved_ids: Iterable[str] = (),
) -> PolicyAdapter:
    """A `PolicyAdapter` whose class carries the chosen capability flags."""
    caps = Capabilities(
        auth_upstream=auth_upstream,
        owns_access_policy=owns_access_policy,
        dm_is_private=dm_is_private,
    )
    cls = cast(type[PolicyAdapter], type("PolicyAdapter", (PolicyAdapter,), {"capabilities": caps}))
    return cls(policies=policies, dm_allow=dm_allow, resolved_ids=resolved_ids)
