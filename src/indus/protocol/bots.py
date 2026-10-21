"""``bots.*``: the methods the desktop's Bots surface calls (P24-31).

The wire models and ``BOT_METHODS`` rows (``(name, params, result, extra errors)``) live here so
the method catalogue and the generated TypeScript include them. The handlers are
``indus.service.bots_rpc.BotsRpc``, which checks the same params with its own strict models and
answers plain JSON; the daemon's WebSocket validates that JSON against the result models below.
A bot with no forever-chat yet has ``canonical: null`` (kept on the wire).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, JsonValue

from indus.protocol.base import ProtocolModel
from indus.protocol.errors import ErrorCode

MAX_AVATAR_BYTES = 256 * 1024
"""Largest decoded avatar picture."""

type AvatarKind = Literal["face", "upload", "portrait", "pet"]


class BotsNoParams(ProtocolModel):
    """No parameters."""


class BotsProfileParams(ProtocolModel):
    """One bot (a profile)."""

    profile: str = Field(min_length=1, max_length=128)
    """The profile's name."""


class BotAvatar(ProtocolModel):
    """A bot's picture: a generated face, an uploaded or portrait image, or the profile's pet."""

    kind: AvatarKind
    """``face``, ``upload``, ``portrait`` or ``pet``."""
    seed: str | None = Field(default=None, max_length=64)
    """The face's seed (``face``)."""
    data: str | None = Field(default=None, max_length=MAX_AVATAR_BYTES * 2)
    """A ``data:image/png|jpeg|webp;base64,…`` URL (``upload``, ``portrait``)."""


class BotsUpdateParams(ProtocolModel):
    """Change a bot's picture, visibility or sidebar sections."""

    profile: str = Field(min_length=1, max_length=128)
    """The profile's name."""
    avatar: BotAvatar | None = None
    """The new picture."""
    hidden: bool | None = None
    """Hide the bot from the list."""
    sections: list[str] | None = None
    """Sidebar sections (at most 20, 64 characters each)."""


class BotRoutine(ProtocolModel):
    """One of the bot's scheduled jobs."""

    id: str
    """The job id."""
    name: str
    """Its name."""
    schedule: str
    """Its schedule."""
    enabled: bool
    """It runs."""


class BotCanonical(ProtocolModel):
    """The bot's forever-chat."""

    session_id: str
    """The live tip of the chat."""


class BotRow(ProtocolModel):
    """One bot."""

    profile: str
    """The profile's name."""
    avatar: dict[str, JsonValue]
    """The picture (``{kind, seed?, data?}``)."""
    hidden: bool
    """Hidden from the list."""
    sections: list[str]
    """Sidebar sections."""
    routines: list[BotRoutine]
    """Scheduled jobs."""
    canonical: BotCanonical | None
    """The forever-chat, or null when it has none yet."""


class _KeepNulls(ProtocolModel):
    def to_wire(self) -> dict[str, Any]:
        """Keep ``canonical: null`` (the client tells "no chat yet" from it)."""
        return self.model_dump(mode="json")


class BotsListResult(_KeepNulls):
    """Every bot."""

    bots: list[BotRow]
    """The bots, by profile."""


class BotsOpenResult(ProtocolModel):
    """The bot's forever-chat, created only when it has none."""

    session_id: str
    """The chat's live tip."""
    created: bool
    """This call created it."""


class BotsUpdateResult(_KeepNulls):
    """The changed bot."""

    bot: BotRow
    """The bot."""


type _Row = tuple[str, type[ProtocolModel], type[ProtocolModel], tuple[ErrorCode, ...]]

BOT_METHODS: tuple[_Row, ...] = (
    ("bots.list", BotsNoParams, BotsListResult, ()),
    ("bots.open", BotsProfileParams, BotsOpenResult, ()),
    ("bots.update", BotsUpdateParams, BotsUpdateResult, ()),
)
"""``(name, params, result, extra errors)`` for the bots methods, in declaration order."""
