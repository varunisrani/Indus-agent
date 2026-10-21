"""``pet.*``: the pet methods the desktop and the TUI call (P24-19).

The wire models and ``PET_METHODS`` rows (``(name, params, result, extra errors)``) live here so
the method catalogue and the generated TypeScript include them; the handlers are in
``indus.service.pets_rpc``.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import Field

from indus.protocol.base import ProtocolModel
from indus.protocol.errors import PROVIDER_UNAVAILABLE, ErrorCode

MAX_DRAFTS = 4
"""Draft looks one ``pet.generate`` may draw."""
MAX_IDEA_CHARS = 500
"""Longest pet idea."""
MAX_NAME_CHARS = 40
"""Longest pet name."""
GENERATE_TIMEOUT_S = 420.0
"""Longest the draft step may take, all drafts together."""
HATCH_TIMEOUT_S = 3_600.0
"""Longest the hatch step may take, every mood together."""


class PetNoParams(ProtocolModel):
    """No parameters."""


class PetInfoParams(ProtocolModel):
    """The shown pet, for a renderer."""

    known_revision: str | None = None
    """The revision the caller already holds; the pictures are left out when it is current."""


class PetInfoResult(ProtocolModel):
    """The shown pet; only ``enabled`` (false) when no pet is shown."""

    enabled: bool
    """A pet is chosen and the display is on."""
    slug: str | None = None
    """The pet's slug."""
    name: str | None = None
    """Its display name."""
    scale: float | None = None
    """Size on every surface (0.25-3)."""
    revision: str | None = None
    """Changes whenever a picture or the name changes."""
    unchanged: bool | None = None
    """True when ``known_revision`` is current and ``moods`` is left out."""
    moods: dict[str, str] | None = None
    """Mood → picture as a data URI."""


class PetRow(ProtocolModel):
    """One pet in the list."""

    slug: str
    """The pet's slug."""
    name: str
    """Its display name."""
    shown: bool
    """It is the active pet and the display is on."""


class PetListResult(ProtocolModel):
    """Every pet of the profile."""

    pets: list[PetRow]
    """The pets, by slug."""
    active: str
    """The chosen slug ("" when none)."""
    enabled: bool
    """The display is on."""


class PetGenerateParams(ProtocolModel):
    """Draw draft looks for a new pet."""

    idea: str = Field(min_length=1, max_length=MAX_IDEA_CHARS)
    """What the pet is, in a few words."""
    count: int = Field(default=MAX_DRAFTS, ge=1, le=MAX_DRAFTS)
    """How many looks to draw."""


class PetDraftInfo(ProtocolModel):
    """One draft look."""

    index: int
    """Its number, for ``pet.hatch``."""
    data_uri: str
    """The picture."""


class PetGenerateResult(ProtocolModel):
    """The drafts and the token that names them."""

    token: str
    """Pass it to ``pet.hatch`` (valid for an hour)."""
    drafts: list[PetDraftInfo]
    """The looks that came back."""


class PetHatchParams(ProtocolModel):
    """Turn one draft into a pet."""

    token: str = Field(min_length=1, max_length=64)
    """From ``pet.generate``."""
    pick: int = Field(ge=0, lt=MAX_DRAFTS)
    """The draft's index."""
    name: str = Field(min_length=1, max_length=MAX_NAME_CHARS)
    """The pet's name."""


class PetSlugParams(ProtocolModel):
    """One pet."""

    slug: str = Field(min_length=1, max_length=64)
    """The pet's slug."""


class PetRenameParams(ProtocolModel):
    """Rename a pet."""

    slug: str = Field(min_length=1, max_length=64)
    """The pet's slug."""
    name: str = Field(min_length=1, max_length=MAX_NAME_CHARS)
    """The new name."""


class PetSlugResult(ProtocolModel):
    """The pet the call acted on."""

    slug: str
    """Its slug (after a rename, the new one)."""
    name: str
    """Its display name."""
    shown: bool
    """It is the pet shown now."""


class PetDisableResult(ProtocolModel):
    """The display state after the call."""

    enabled: bool
    """Always false."""


type _Row = tuple[str, type[ProtocolModel], type[ProtocolModel], tuple[ErrorCode, ...]]

PET_METHODS: tuple[_Row, ...] = (
    ("pet.info", PetInfoParams, PetInfoResult, ()),
    ("pet.list", PetNoParams, PetListResult, ()),
    ("pet.generate", PetGenerateParams, PetGenerateResult, (PROVIDER_UNAVAILABLE,)),
    ("pet.hatch", PetHatchParams, PetSlugResult, (PROVIDER_UNAVAILABLE,)),
    ("pet.select", PetSlugParams, PetSlugResult, ()),
    ("pet.disable", PetNoParams, PetDisableResult, ()),
    ("pet.rename", PetRenameParams, PetSlugResult, ()),
    ("pet.remove", PetSlugParams, PetSlugResult, ()),
)
"""``(name, params, result, extra errors)`` for the pet methods, in declaration order."""

PET_DEADLINES: Mapping[str, float] = {
    # The provider deadline plus a little room to write and answer.
    "pet.generate": GENERATE_TIMEOUT_S + 15.0,
    "pet.hatch": HATCH_TIMEOUT_S + 15.0,
}
"""Dispatcher deadlines for the drawing steps (in ``rpc.dispatch.DEADLINES``)."""
