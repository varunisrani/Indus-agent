"""``wake.*``: the wake-word methods the desktop and the TUI call (P24-20).

The wire models, ``WAKE_METHODS`` rows (``(name, params, result, extra errors)``) and the
``wake.detected`` notification live here so the method catalogue and the generated TypeScript
include them; the handlers are ``indus.service.wake_rpc.WakeRpc``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import Field

from indus.protocol.base import ProtocolModel
from indus.protocol.errors import ErrorCode

SAMPLE_RATE = 16_000
"""The only audio rate ``wake.feed`` takes (16 kHz mono int16)."""
MAX_FEED_BYTES = 64_000
"""Largest decoded audio chunk one ``wake.feed`` may carry (2 s at 16 kHz int16)."""
WAKE_DETECTED_EVENT = "wake.detected"
"""The notification a detection sends to the owning connection."""
WAKE_START_DEADLINE_S = 180.0
"""A first ``wake.start`` may load a model."""

type Surface = Literal["tui", "gui", "cli"]


class WakeNoParams(ProtocolModel):
    """No parameters."""


class WakeStartParams(ProtocolModel):
    """Arm the listener for the calling surface."""

    surface: Surface = "gui"
    """Who asks: ``gui`` (desktop), ``tui`` or ``cli``."""
    consent: bool = False
    """An explicit "on" from the person: store consent and turn the feature on first."""
    client_capture: bool = False
    """The surface sends the audio with ``wake.feed``; no microphone is opened by the daemon."""


class WakeStartResult(ProtocolModel):
    """Whether the listener is armed for this caller."""

    started: bool
    """True when armed."""
    reason: Literal["disabled", "consent_required", "unavailable", "owned"] | None = None
    """Why not."""
    hint: str | None = None
    """What to install or set, for ``unavailable``."""
    phrase: str
    """The phrase listened for."""
    engine: str | None = None
    """The engine in use."""
    capture: Literal["local", "client"] | None = None
    """Who captures the audio."""
    consent_saved: bool | None = None
    """True when this call stored consent."""


class WakeStopParams(ProtocolModel):
    """Disarm the listener."""

    persist: bool = False
    """An explicit "off": also turn ``wake_word.enabled`` off."""


class WakeOwnerResult(ProtocolModel):
    """The answer of an owner-gated call."""

    ok: bool
    """The call took effect."""
    reason: Literal["not_owner"] | None = None
    """Another surface owns the listener (or none is armed)."""


class WakeStatusResult(ProtocolModel):
    """What the listener is doing, for this caller."""

    enabled: bool
    """``wake_word.enabled``."""
    consent: bool
    """``wake_word.consent``."""
    listening: bool
    """Armed for this caller and not paused (the microphone or the feed is live)."""
    owner_surface: str | None = None
    """The surface that owns the listener, if any."""
    phrase: str
    """The phrase listened for."""
    engine: str
    """The configured engine."""
    available: bool
    """The engine and model are present."""
    hint: str | None = None
    """What is missing."""


class WakeFeedParams(ProtocolModel):
    """Client-captured audio for a ``client_capture`` listener."""

    pcm: str = Field(min_length=1, max_length=(MAX_FEED_BYTES * 4) // 3 + 4)
    """Base64 int16 mono little-endian audio."""
    sample_rate: int = SAMPLE_RATE
    """Must be 16000."""


class WakeFeedResult(ProtocolModel):
    """Whether the audio reached the detector."""

    fed: bool
    """False when no client-capture listener of this caller is armed."""


class WakeDetected(ProtocolModel):
    """``wake.detected``: the phrase was heard; start voice capture now."""

    phrase: str
    """The phrase heard."""
    surface: str
    """The owning surface."""


type _Row = tuple[str, type[ProtocolModel], type[ProtocolModel], tuple[ErrorCode, ...]]

WAKE_METHODS: tuple[_Row, ...] = (
    ("wake.start", WakeStartParams, WakeStartResult, ()),
    ("wake.stop", WakeStopParams, WakeOwnerResult, ()),
    ("wake.pause", WakeNoParams, WakeOwnerResult, ()),
    ("wake.resume", WakeNoParams, WakeOwnerResult, ()),
    ("wake.status", WakeNoParams, WakeStatusResult, ()),
    ("wake.feed", WakeFeedParams, WakeFeedResult, ()),
)
"""``(name, params, result, extra errors)`` for the wake methods, in declaration order."""

WAKE_DEADLINES: Mapping[str, float] = {"wake.start": WAKE_START_DEADLINE_S}
"""Dispatcher deadlines (in ``rpc.dispatch.DEADLINES``)."""
