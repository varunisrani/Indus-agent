"""``voice.*``: speech to text and text to speech for the desktop composer (P24-35).

The desktop records a clip and asks ``voice.transcribe`` for its text (dictation, spoken
conversation); ``voice.speak`` reads a reply aloud. Both run the profile's configured providers
(P10: ``stt.*``, ``tts.*``) inside the daemon, so provider keys never reach the renderer. The
handlers are in ``indus.service.voice_rpc``.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import Field

from indus.protocol.base import ProtocolModel
from indus.protocol.errors import PROVIDER_UNAVAILABLE, ErrorCode

MAX_CLIP_BYTES = 16 * 1024 * 1024
"""Largest decoded clip ``voice.transcribe`` takes."""
MAX_SPEAK_CHARS = 20_000
"""Longest text ``voice.speak`` reads."""
VOICE_TIMEOUT_S = 300.0
"""A long clip or text may take a provider a while."""


class VoiceTranscribeParams(ProtocolModel):
    """The text of one recorded clip."""

    session_id: str | None = None
    """The chat the clip is for (null: a new chat)."""
    audio_b64: str = Field(min_length=1, max_length=(MAX_CLIP_BYTES * 4) // 3 + 4)
    """The clip, base64."""
    mime: str = Field(min_length=1, max_length=100)
    """Its type (``audio/webm``, ``audio/wav`` ...)."""


class VoiceTranscribeResult(ProtocolModel):
    """What was said."""

    text: str
    """The transcript ("" when nothing could be heard)."""


class VoiceSpeakParams(ProtocolModel):
    """Read a text aloud."""

    text: str = Field(min_length=1, max_length=MAX_SPEAK_CHARS)
    """The text."""


class VoiceSpeakResult(ProtocolModel):
    """The speech."""

    audio_b64: str
    """The audio, base64."""
    mime: str
    """Its type."""


type _Row = tuple[str, type[ProtocolModel], type[ProtocolModel], tuple[ErrorCode, ...]]

VOICE_METHODS: tuple[_Row, ...] = (
    ("voice.transcribe", VoiceTranscribeParams, VoiceTranscribeResult, (PROVIDER_UNAVAILABLE,)),
    ("voice.speak", VoiceSpeakParams, VoiceSpeakResult, (PROVIDER_UNAVAILABLE,)),
)
"""``(name, params, result, extra errors)`` for the voice methods, in declaration order."""

VOICE_DEADLINES: Mapping[str, float] = {
    "voice.transcribe": VOICE_TIMEOUT_S,
    "voice.speak": VOICE_TIMEOUT_S,
}
"""Dispatcher deadlines (in ``rpc.dispatch.DEADLINES``)."""
