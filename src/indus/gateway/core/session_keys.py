"""The one session-key builder (P15-07, R06.7).

A session key names one conversation slot. It is persisted in the routing index, so its bytes are
a compatibility surface: ``tests/fixtures/gateway/session_keys.golden.json`` freezes them, and no
other code may assemble a key.

Layout: ``<ns>:<platform>:<chat_type>[:<scope_id>][:<chat_id>][<user and thread parts>]``.

- ``<ns>`` is ``@default`` for the default profile and the profile id otherwise. ``@`` is outside
  the profile-id alphabet, so no profile can ever produce the default namespace. Under
  multiplexing (P24-03) the namespace is the *routed* profile's — the profile that runs the
  message, which may not be the one whose bot received it — and ``profile_of_key`` reads it back.
- Only Slack puts a scope (its workspace id) before the chat id; a Discord guild id is never part
  of a key.
- DMs key on the chat id; without one on the sender, without both one slot per platform. In a DM
  the user part comes before the thread part.
- In groups each participant (alternate id first) gets their own slot by default
  (``group_sessions_per_user``), except inside a thread: everyone in a thread shares it unless
  ``thread_sessions_per_user`` is on. In a group the thread part comes before the user part.
- A channel message that will open a thread (Discord auto-threads) is keyed on that future thread
  (``prospective_thread_id``) with the chat-type slot ``thread``, so the follow-ups inside the
  thread produce the same bytes; a real thread id always wins; DMs never use the prospective id.
"""

from __future__ import annotations

from typing import Any

from indus.foundation.config.schema import GatewaySection
from indus.foundation.profiles import DEFAULT_PROFILE
from indus.gateway.core._shapes import SourceLike, text_of

__all__ = [
    "DEFAULT_NAMESPACE",
    "build_session_key",
    "key_options",
    "canonical_participant",
    "key_namespace",
    "origin_of",
    "profile_of_key",
]

DEFAULT_NAMESPACE = "@default"
_DM = "dm"
_THREAD = "thread"
# Platforms whose workspace id is part of the key (one bot installed in several workspaces can see
# the same channel id in each).
_SCOPED_PLATFORMS = frozenset({"slack"})
_ORIGIN_FIELDS = (
    "platform",
    "chat_id",
    "chat_name",
    "chat_type",
    "user_id",
    "user_name",
    "thread_id",
    "user_id_alt",
    "is_bot",
    "scope_id",
    "guild_id",
    "parent_chat_id",
    "prospective_thread_id",
)


def key_namespace(profile: str | None) -> str:
    """``@default`` for the default profile (or none), else the profile id."""
    name = (profile or "").strip()
    if not name or name == DEFAULT_PROFILE:
        return DEFAULT_NAMESPACE
    return name


def profile_of_key(session_key: str) -> str:
    """The profile whose namespace starts ``session_key`` (``default`` for ``@default``)."""
    namespace = session_key.split(":", 1)[0].strip()
    if not namespace or namespace == DEFAULT_NAMESPACE:
        return DEFAULT_PROFILE
    return namespace


def canonical_participant(source: SourceLike) -> str:
    """The sender id used to separate participants: the alternate id when the platform has one.

    Platform-specific identity folding (WhatsApp phone / JID aliases) plugs in here (P16).
    """
    return text_of(source.user_id_alt) or text_of(source.user_id)


# Defaults come from the one config defaults table (R06.25).
_SCHEMA = GatewaySection()


def key_options(section: GatewaySection) -> dict[str, bool]:
    """``build_session_key`` options from a profile's ``gateway`` section."""
    return {
        "group_sessions_per_user": section.group_sessions_per_user,
        "thread_sessions_per_user": section.thread_sessions_per_user,
    }


def build_session_key(
    source: SourceLike,
    *,
    profile: str | None = None,
    group_sessions_per_user: bool = _SCHEMA.group_sessions_per_user,
    thread_sessions_per_user: bool = _SCHEMA.thread_sessions_per_user,
) -> str:
    """The session key for ``source`` (see the module docstring for the rules)."""
    platform = text_of(source.platform).lower()
    chat_type = text_of(source.chat_type).lower() or _DM
    is_dm = chat_type == _DM
    chat_id = text_of(source.chat_id)
    real_thread = text_of(source.thread_id)
    thread = real_thread or ("" if is_dm else text_of(source.prospective_thread_id))
    slot = _THREAD if thread and not real_thread else chat_type

    if is_dm:
        separate_user = not chat_id
    else:
        separate_user = group_sessions_per_user and not (thread and not thread_sessions_per_user)
    participant = canonical_participant(source) if separate_user else ""

    parts = [key_namespace(profile), platform, slot]
    scope = text_of(source.scope_id)
    if platform in _SCOPED_PLATFORMS and scope:
        parts.append(scope)
    if chat_id:
        parts.append(chat_id)
    user_part = [participant] if participant else []
    thread_part = [thread] if thread else []
    parts.extend(user_part + thread_part if is_dm else thread_part + user_part)
    return ":".join(parts)


def origin_of(source: SourceLike) -> dict[str, Any]:
    """A JSON-safe copy of the source for the routing index (delivery and restore use it)."""
    origin: dict[str, Any] = {}
    for name in _ORIGIN_FIELDS:
        value = getattr(source, name, None)
        if name == "is_bot":
            origin[name] = value is True
        elif value is not None and value != "":
            origin[name] = text_of(value)
    return origin
