"""Slash-command admin tiers per platform and scope (P15-10, K17).

Among the people allowed to talk to the agent, who may run which slash command. Each platform's
``extra`` holds two lists per scope:

- DM scope: ``allow_admin_from`` (admins: every command, plugin commands included) and
  ``user_allowed_commands`` (what everyone else may run);
- group scope: ``group_allow_admin_from`` and ``group_user_allowed_commands``.

Non-admins can always run ``help`` and ``whoami``. A scope with no admin list has tiers off:
everyone runs everything there, so nothing changes until an operator names an admin. Plain chat is
never gated. ``/help`` lists only what the caller may run (``runnable``). A source with an empty or
unknown chat type is judged by whichever scope is gated, so ambiguity never opens access.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

__all__ = [
    "FLOOR",
    "SlashPolicy",
    "policy_for",
    "policy_from_extra",
    "runnable",
    "scope_of",
    "tier_label",
]

FLOOR: frozenset[str] = frozenset({"help", "whoami"})
_DM_TYPES = frozenset({"dm", "direct", "private"})
_KEYS: dict[str, tuple[str, str]] = {
    "dm": ("allow_admin_from", "user_allowed_commands"),
    "group": ("group_allow_admin_from", "group_user_allowed_commands"),
}

type Scope = Literal["dm", "group"]


def _items(raw: object, normalise: Callable[[str], str]) -> frozenset[str]:
    if raw is None:
        return frozenset()
    if isinstance(raw, str):
        parts: Iterable[Any] = raw.split(",")
    elif isinstance(raw, list | tuple | set | frozenset):
        parts = raw  # pyright: ignore[reportUnknownVariableType]
    else:
        parts = (raw,)
    return frozenset(v for v in (normalise(str(p).strip()) for p in parts) if v)


def _command(name: str) -> str:
    return name.lstrip("/").strip().lower()


@dataclass(frozen=True)
class SlashPolicy:
    enabled: bool
    admins: frozenset[str] = frozenset()
    user_commands: frozenset[str] = frozenset()

    def is_admin(self, user_id: str | None) -> bool:
        """With tiers off everyone counts as an admin."""
        if not self.enabled:
            return True
        return bool(user_id) and str(user_id) in self.admins

    def can_run(self, user_id: str | None, command: str) -> bool:
        if self.is_admin(user_id):
            return True
        name = _command(command)
        return bool(name) and (name in FLOOR or name in self.user_commands)


def policy_from_extra(extra: Mapping[str, Any] | None, scope: Scope) -> SlashPolicy:
    data: Mapping[str, Any] = extra if isinstance(extra, Mapping) else {}
    admin_key, commands_key = _KEYS[scope]
    admins = _items(data.get(admin_key), str)
    commands = _items(data.get(commands_key), _command)
    return SlashPolicy(enabled=bool(admins), admins=admins, user_commands=commands)


def scope_of(chat_type: object) -> Scope | None:
    """``dm`` / ``group`` for a chat type; None when it is empty."""
    raw = getattr(chat_type, "value", chat_type)
    text = str(raw).strip().lower() if raw is not None else ""
    if not text:
        return None
    return "dm" if text in _DM_TYPES else "group"


def policy_for(extra: Mapping[str, Any] | None, chat_type: object) -> SlashPolicy:
    """The policy for a chat; an empty chat type takes whichever scope is gated (group on a tie)."""
    scope = scope_of(chat_type)
    if scope is not None:
        return policy_from_extra(extra, scope)
    dm = policy_from_extra(extra, "dm")
    group = policy_from_extra(extra, "group")
    return dm if dm.enabled and not group.enabled else group


def runnable(policy: SlashPolicy, user_id: str | None, names: Iterable[str]) -> list[str]:
    """The commands of ``names`` the caller may run, in the given order (for ``/help``)."""
    return [name for name in names if policy.can_run(user_id, name)]


def tier_label(policy: SlashPolicy, user_id: str | None) -> str:
    """What ``/whoami`` says about the caller's tier."""
    if not policy.enabled:
        return "full access (no admins are set here)"
    if policy.is_admin(user_id):
        return "admin (every command)"
    return "user (limited commands)"
