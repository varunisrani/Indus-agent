"""What the session-key builder, the authorization chain and the guards read from a source and an
adapter, written as structural types.

The inbound ``Source`` model (``gateway.core.events``, P15-03) and the adapter base
(``gateway.adapters.base``, P15-05) satisfy these protocols; naming them here keeps the core
modules free of an import of the adapter package (which itself imports the core). Values may be
plain strings or enums: ``text_of`` reduces both to the string.
"""

from __future__ import annotations

from collections.abc import Set as AbstractSet
from enum import Enum
from typing import Literal, Protocol, runtime_checkable

__all__ = [
    "AdapterLike",
    "CapabilityFlags",
    "SourceLike",
    "capability",
    "flag",
    "text_of",
]


@runtime_checkable
class SourceLike(Protocol):
    """Where an inbound message came from (fields of ``Source``, research 06 §3.4)."""

    @property
    def platform(self) -> object: ...

    @property
    def chat_id(self) -> str | None: ...

    @property
    def chat_type(self) -> object: ...

    @property
    def user_id(self) -> str | None: ...

    @property
    def user_name(self) -> str | None: ...

    @property
    def thread_id(self) -> str | None: ...

    @property
    def user_id_alt(self) -> str | None: ...

    @property
    def is_bot(self) -> bool: ...

    @property
    def scope_id(self) -> str | None: ...

    @property
    def parent_chat_id(self) -> str | None: ...

    @property
    def prospective_thread_id(self) -> str | None: ...

    @property
    def role_authorized(self) -> bool: ...


class CapabilityFlags(Protocol):
    """The adapter capability flags authorization reads (``Capabilities``)."""

    @property
    def auth_upstream(self) -> bool: ...

    @property
    def owns_access_policy(self) -> bool: ...

    @property
    def dm_is_private(self) -> bool: ...


class AdapterLike(Protocol):
    """A connected adapter as the authorization chain sees it (``Adapter``)."""

    @property
    def capabilities(self) -> CapabilityFlags: ...

    def access_policy(self, kind: Literal["dm", "group"]) -> str: ...

    def allows_dm(self, user_id: str) -> bool: ...

    def resolved_allowlist_ids(self) -> AbstractSet[str]: ...


def text_of(value: object) -> str:
    """A field as plain text: an enum's value, ``None`` as ``""``, anything else ``str``."""
    if value is None:
        return ""
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def flag(obj: object, name: str) -> bool:
    """``obj.name`` only when it is exactly ``True`` (a truthy non-bool never grants)."""
    return getattr(obj, name, False) is True


def capability(adapter: AdapterLike | None, name: str) -> bool:
    """The adapter's capability flag ``name``, exactly ``True`` only; False without an adapter."""
    return adapter is not None and flag(adapter.capabilities, name)
