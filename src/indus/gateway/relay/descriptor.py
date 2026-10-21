# pyright: strict
"""The capability descriptor a relay connector sends for each platform it fronts (P24-08).

Our own contract (D-16.1), version 1, written up in ``docs/user/relay-contract.md``. One
descriptor per fronted platform arrives right after the gateway's signed hello; the adapter for
that platform (``relay.adapter``) takes its length cap, length unit, markup and optional
operations from it, so one adapter class serves every platform without per-platform branches.

Reading is forgiving where a safe default exists and strict where a wrong guess would mislead:

- ``max_len`` 0, negative or not a whole number → 4096 (the adapter must always be able to
  chunk);
- an unknown ``len_unit`` → ``utf16``: a UTF-16 count is never smaller than a code-point count, so
  a chunk measured that way fits either kind of limit;
- ``ops`` absent or empty → the base set (``send``, ``edit``, ``typing``, ``follow_up``); an
  optional op is used only when it is advertised; names we do not know are kept and ignored;
- a ``contract_version`` other than 1, or a platform name that is not a short lower-case
  identifier, is refused (`DescriptorError`): that platform is not fronted.

Unknown fields are ignored (later minor additions stay readable).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal, cast

from indus.gateway.adapters.base import Capabilities
from indus.gateway.outbound.chunker import LenFn, code_points, utf16_units
from indus.gateway.outbound.formatting import Markup

__all__ = [
    "BASE_OPS",
    "CONTRACT_VERSION",
    "DEFAULT_MAX_LEN",
    "OPTIONAL_OPS",
    "Descriptor",
    "DescriptorError",
    "LenUnit",
]

CONTRACT_VERSION: Final = 1
DEFAULT_MAX_LEN: Final = 4096

type LenUnit = Literal["chars", "utf16"]

# Every connector implements these; the optional ones only when advertised.
BASE_OPS: Final[frozenset[str]] = frozenset({"send", "edit", "typing", "follow_up"})
OPTIONAL_OPS: Final[frozenset[str]] = frozenset(
    {"send_media", "prompt", "react", "thread_create", "thread_rename", "chat_info", "delete"}
)

_PLATFORM = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_MAX_LABEL = 100

# A connector's markdown dialect → the markup our formatter writes.
_DIALECTS: Mapping[str, Markup] = {
    "markdown_v2": "markdownv2",
    "markdownv2": "markdownv2",
    "telegram": "markdownv2",
    "mrkdwn": "mrkdwn",
    "slack": "mrkdwn",
    "markdown": "markdown",
    "commonmark": "markdown",
    "discord": "markdown",
    "gfm": "markdown",
    "plain": "plain",
}


class DescriptorError(ValueError):
    """A descriptor we cannot read safely; the platform it names is not fronted."""


def _flag(raw: Mapping[str, Any], key: str) -> bool:
    return raw.get(key) is True


def _max_len(value: object) -> int:
    if isinstance(value, bool):
        return DEFAULT_MAX_LEN
    if isinstance(value, int):
        return value if value > 0 else DEFAULT_MAX_LEN
    if isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
        return number if number > 0 else DEFAULT_MAX_LEN
    return DEFAULT_MAX_LEN


def _ops(value: object) -> frozenset[str]:
    if not isinstance(value, list | tuple):
        return BASE_OPS
    names = {op.strip() for op in cast(list[object], value) if isinstance(op, str) and op.strip()}
    return frozenset(names) if names else BASE_OPS


@dataclass(frozen=True)
class Descriptor:
    platform: str
    label: str = ""
    max_len: int = DEFAULT_MAX_LEN
    len_unit: LenUnit = "chars"
    supports_draft: bool = False
    supports_edit: bool = False
    supports_threads: bool = False
    markdown_dialect: str = "plain"
    block_formatting: bool = False
    ops: frozenset[str] = field(default=BASE_OPS)
    contract_version: int = CONTRACT_VERSION

    # -- reading ----------------------------------------------------------------------------------

    @classmethod
    def parse(cls, raw: object) -> Descriptor:
        """A descriptor from the connector's JSON object (see the module notes)."""
        if not isinstance(raw, Mapping):
            raise DescriptorError("a descriptor must be a JSON object")
        data = cast(Mapping[str, Any], raw)
        version = data.get("contract_version")
        if type(version) is not int or version != CONTRACT_VERSION:
            raise DescriptorError(
                f"contract_version {version!r} is not supported (this gateway speaks "
                f"{CONTRACT_VERSION})"
            )
        platform = data.get("platform")
        if not isinstance(platform, str) or not _PLATFORM.match(platform):
            raise DescriptorError(f"platform {platform!r} is not a valid platform name")
        label = data.get("label")
        unit = data.get("len_unit")
        dialect = data.get("markdown_dialect")
        return cls(
            platform=platform,
            label=(label.strip()[:_MAX_LABEL] if isinstance(label, str) else "") or platform,
            max_len=_max_len(data.get("max_len")),
            len_unit=unit if unit in ("chars", "utf16") else "utf16",
            supports_draft=_flag(data, "supports_draft"),
            supports_edit=_flag(data, "supports_edit"),
            supports_threads=_flag(data, "supports_threads"),
            markdown_dialect=dialect.strip().lower() if isinstance(dialect, str) else "plain",
            block_formatting=_flag(data, "block_formatting"),
            ops=_ops(data.get("ops")),
        )

    def to_wire(self) -> dict[str, Any]:
        """The JSON object a connector sends (fakes and the contract document use it)."""
        return {
            "contract_version": self.contract_version,
            "platform": self.platform,
            "label": self.label,
            "max_len": self.max_len,
            "len_unit": self.len_unit,
            "supports_draft": self.supports_draft,
            "supports_edit": self.supports_edit,
            "supports_threads": self.supports_threads,
            "markdown_dialect": self.markdown_dialect,
            "block_formatting": self.block_formatting,
            "ops": sorted(self.ops),
        }

    # -- what the adapter makes of it -------------------------------------------------------------

    def supports(self, op: str) -> bool:
        return op in self.ops

    @property
    def len_fn(self) -> LenFn:
        return utf16_units if self.len_unit == "utf16" else code_points

    @property
    def markup(self) -> Markup:
        return _DIALECTS.get(self.markdown_dialect, "plain")

    def capabilities(self) -> Capabilities:
        """The adapter capabilities this descriptor grants (relay events are authorised by the
        connector, so ``auth_upstream``; long text is chunked by the gateway, not the connector)."""
        return Capabilities(
            max_len=self.max_len,
            len_fn=self.len_fn,
            markup=self.markup,
            splits_natively=False,
            supports_edit=self.supports_edit and self.supports("edit"),
            supports_delete=self.supports("delete"),
            supports_buttons=self.supports("prompt"),
            owns_access_policy=False,
            auth_upstream=True,
            dm_is_private=True,
            push_capable=True,
        )
