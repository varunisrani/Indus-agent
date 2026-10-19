"""Discord application commands built from the slash registry, and their paced sync (P15-34).

Discord shows a bot's chat commands in its picker once they are registered with the application.
We register the chat commands of the shared slash registry (gateway surface), in table order, then
any extra names the host adds (plugin or skill commands), and stop at Discord's limit of 100:
whatever does not fit is dropped with one warning, never sent (one command over the limit makes
Discord refuse the whole set). Names are lower case and at most 32 characters; descriptions and
the optional ``args`` text option are cut to 100 characters.

``sync_commands`` brings the application's global commands in line with that list by difference:
commands we no longer offer are deleted first (so a full application can make room), then missing
ones are created and changed ones edited; unchanged commands are left alone. Command routes have a
small rate-limit bucket, so mutations are spaced ``MUTATION_GAP_S`` apart on the adapter's clock.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from indus.foundation.clock import Clock
from indus.service.commands.registry import REGISTRY

__all__ = [
    "MAX_COMMANDS",
    "MUTATION_GAP_S",
    "NAME_MAX",
    "TEXT_MAX",
    "CommandSpec",
    "SyncSummary",
    "cap_commands",
    "clip",
    "desired_commands",
    "registry_commands",
    "sync_commands",
]

log = logging.getLogger(__name__)

MAX_COMMANDS = 100
NAME_MAX = 32
TEXT_MAX = 100
MUTATION_GAP_S = 4.5
# Chat-input commands are type 1; user and message context-menu commands are left alone.
CHAT_INPUT = 1
STRING_OPTION = 3
ARGS_OPTION = "args"

_VALID_NAME = re.compile(r"^[-_a-z0-9]{1,32}$")
_ELLIPSIS = "…"


def clip(text: str, limit: int = TEXT_MAX) -> str:
    """``text`` on one line, cut to ``limit`` characters with an ellipsis."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: max(0, limit - 1)].rstrip() + _ELLIPSIS


@dataclass(frozen=True)
class CommandSpec:
    """One chat command as Discord stores it."""

    name: str
    description: str
    args_hint: str = ""

    def payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "type": CHAT_INPUT,
            "name": self.name,
            "description": self.description,
        }
        if self.args_hint:
            body["options"] = [
                {
                    "type": STRING_OPTION,
                    "name": ARGS_OPTION,
                    "description": clip(self.args_hint),
                    "required": False,
                }
            ]
        return body


def _spec(name: str, description: str, args_hint: str = "") -> CommandSpec | None:
    clean = name.strip().lstrip("/").lower()
    if not _VALID_NAME.match(clean):
        log.debug("discord: /%s cannot be an application command name; skipped", name)
        return None
    text = clip(description) or f"Run /{clean}"
    return CommandSpec(clean, text, args_hint.strip())


def registry_commands() -> list[CommandSpec]:
    """The chat commands of the shared slash registry, in table order."""
    specs: list[CommandSpec] = []
    for cmd in REGISTRY.commands("gateway"):
        spec = _spec(cmd.name, cmd.description, cmd.args_hint)
        if spec is not None:
            specs.append(spec)
    return specs


def cap_commands(specs: Iterable[CommandSpec], limit: int = MAX_COMMANDS) -> list[CommandSpec]:
    """``specs`` without repeated names, at most ``limit`` of them (one warning for the rest)."""
    kept: list[CommandSpec] = []
    seen: set[str] = set()
    dropped = 0
    for spec in specs:
        if spec.name in seen:
            continue
        if len(kept) >= limit:
            dropped += 1
            continue
        seen.add(spec.name)
        kept.append(spec)
    if dropped:
        log.warning(
            "discord: %d command(s) left out of the command picker: Discord allows at most %d",
            dropped,
            limit,
        )
    return kept


def desired_commands(extra: Iterable[tuple[str, str, str]] = ()) -> list[CommandSpec]:
    """Registry commands first, then ``extra`` ``(name, description, args_hint)``, capped."""
    specs = registry_commands()
    for name, description, args_hint in extra:
        spec = _spec(name, description, args_hint)
        if spec is not None:
            specs.append(spec)
    return cap_commands(specs)


# -- sync ------------------------------------------------------------------------------------------


def _comparable(payload: Mapping[str, Any]) -> tuple[Any, ...]:
    options = tuple(
        (
            int(o.get("type", STRING_OPTION) or STRING_OPTION),
            str(o.get("name", "")),
            str(o.get("description", "")),
            bool(o.get("required", False)),
        )
        for o in payload.get("options") or ()
        if isinstance(o, Mapping)
    )
    return (str(payload.get("name", "")), str(payload.get("description", "")), options)


@dataclass
class SyncSummary:
    created: list[str] = field(default_factory=list[str])
    edited: list[str] = field(default_factory=list[str])
    deleted: list[str] = field(default_factory=list[str])
    unchanged: int = 0

    @property
    def mutations(self) -> int:
        return len(self.created) + len(self.edited) + len(self.deleted)


async def sync_commands(
    http: Any,
    application_id: int | str,
    specs: Sequence[CommandSpec],
    *,
    clock: Clock,
    gap: float = MUTATION_GAP_S,
) -> SyncSummary:
    """Bring the application's global chat commands in line with ``specs`` (see the module notes).

    ``http`` is discord.py's ``HTTPClient`` (``client.http``). Errors propagate to the caller.
    """
    summary = SyncSummary()
    existing: dict[str, Mapping[str, Any]] = {}
    for item in await http.get_global_commands(application_id):
        if isinstance(item, Mapping) and int(item.get("type", CHAT_INPUT) or CHAT_INPUT) == 1:
            existing[str(item.get("name", "")).lower()] = item
    wanted = {spec.name: spec for spec in specs}
    done = 0

    async def paced() -> None:
        nonlocal done
        if done:
            await clock.sleep(gap)
        done += 1

    for name, item in existing.items():
        if name not in wanted:
            await paced()
            await http.delete_global_command(application_id, item["id"])
            summary.deleted.append(name)
    for name, spec in wanted.items():
        payload = spec.payload()
        current = existing.get(name)
        if current is None:
            await paced()
            await http.upsert_global_command(application_id, payload)
            summary.created.append(name)
        elif _comparable(current) != _comparable(payload):
            await paced()
            await http.edit_global_command(application_id, current["id"], payload)
            summary.edited.append(name)
        else:
            summary.unchanged += 1
    return summary
