"""``voice.*``: the daemon's speech methods for the desktop composer (P24-35, integration).

``voice.transcribe`` writes the clip to a private file under ``<home>/cache/voice/``, runs the
profile's speech-to-text provider on it (``tools.builtin.media.stt.transcribe``) and removes the
file; ``voice.speak`` runs the text-to-speech provider (``tts.SpeechTool``), reads the audio back
and removes the file. A provider that is missing, not set up or failing is ``PROVIDER_UNAVAILABLE``
with the provider's own reason (never a key); a clip that is not base64 is ``INVALID_PARAMS``.
Neither the clip nor the text is logged.

``voice.listen`` (the wake word's hand-off for the terminal UI, P24-20) records one take on this
process's microphone — the one the wake word listened on, which a detection already released —
with the REPL voice mode's rules (``tools.builtin.media.voice_take``: ``voice.silence_seconds``
after speech ends it, ``voice.wait_seconds`` without speech gives up, ``voice.max_seconds`` caps
it), then transcribes it as ``voice.transcribe`` does. A take without speech is never sent to the
provider. ``MICROPHONE`` and ``CLOCK`` are the seams tests replace.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from indus.foundation.blocking import run_blocking
from indus.foundation.fsio import atomic_write_bytes
from indus.foundation.home import secure_dir
from indus.foundation.scope import Scope
from indus.protocol.base import ProtocolModel
from indus.protocol.errors import INVALID_PARAMS, PROVIDER_UNAVAILABLE
from indus.protocol.methods import ProtocolError
from indus.protocol.voice import (
    MAX_CLIP_BYTES,
    VOICE_METHODS,
    VoiceListenParams,
    VoiceListenResult,
    VoiceSpeakParams,
    VoiceSpeakResult,
    VoiceTranscribeParams,
    VoiceTranscribeResult,
)

__all__ = [
    "CLOCK",
    "MICROPHONE",
    "VOICE_HANDLERS",
    "VOICE_METHODS",
    "register",
    "voice_listen",
    "voice_speak",
    "voice_transcribe",
]

_SUFFIXES: Mapping[str, str] = {
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/flac": ".flac",
}
_MIMES: Mapping[str, str] = {
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
}


class VoiceContext(Protocol):
    @property
    def scope(self) -> Scope: ...


def _clip_dir(scope: Scope) -> Path:
    return secure_dir(scope.home / "cache" / "voice", env=scope.env)


async def voice_transcribe(
    ctx: VoiceContext, params: VoiceTranscribeParams
) -> VoiceTranscribeResult:
    from indus.tools.builtin.media import stt

    try:
        clip = base64.b64decode(params.audio_b64, validate=True)
    except (binascii.Error, ValueError):
        raise ProtocolError(INVALID_PARAMS, "audio_b64 is not base64", path="audio_b64") from None
    if not clip or len(clip) > MAX_CLIP_BYTES:
        raise ProtocolError(INVALID_PARAMS, "the clip is empty or too large", path="audio_b64")
    scope = ctx.scope
    base = params.mime.split(";", 1)[0].strip().lower()
    suffix = _SUFFIXES.get(base, ".bin")

    def save() -> Path:
        path = _clip_dir(scope) / f"clip-{secrets.token_hex(8)}{suffix}"
        atomic_write_bytes(path, clip, mode=0o600)
        return path

    path = await run_blocking(save, label="voice clip")
    try:
        heard = await stt.transcribe(path, scope=scope)
    except stt.SttError as exc:
        raise ProtocolError(PROVIDER_UNAVAILABLE, str(exc)) from None
    finally:
        with contextlib.suppress(OSError):
            path.unlink()
    return VoiceTranscribeResult(text="" if heard.inaudible else heard.text)


async def voice_speak(ctx: VoiceContext, params: VoiceSpeakParams) -> VoiceSpeakResult:
    from indus.tools.builtin.media import tts

    scope = ctx.scope
    try:
        made = await tts.SpeechTool().speak(params.text, scope)
    except (tts.SpeechError, tts.OutputRefused, OSError) as exc:
        raise ProtocolError(PROVIDER_UNAVAILABLE, str(exc)) from None

    def take() -> bytes:
        try:
            return made.path.read_bytes()
        finally:
            with contextlib.suppress(OSError):
                made.path.unlink()

    audio = await run_blocking(take, label="voice read")
    mime = _MIMES.get(made.path.suffix.lower(), "application/octet-stream")
    return VoiceSpeakResult(audio_b64=base64.b64encode(audio).decode("ascii"), mime=mime)


def _default_microphone() -> Any:
    from indus.tools.builtin.media.wake_word import SoundDeviceMicrophone

    return SoundDeviceMicrophone()


MICROPHONE: Callable[[], Any] = _default_microphone
"""The audio input ``voice.listen`` opens (a ``wake_word.Microphone``)."""
CLOCK: Callable[[], float] = time.monotonic
"""The clock a take's silence and limits are measured on."""


async def voice_listen(ctx: VoiceContext, params: VoiceListenParams) -> VoiceListenResult:
    from indus.foundation import config
    from indus.tools.builtin.media import stt
    from indus.tools.builtin.media.voice_take import VoiceSettings, record_take
    from indus.tools.builtin.media.wake_word import WakeUnavailable

    scope = ctx.scope
    if scope.config is None:
        scope = scope.with_config(await run_blocking(config.load, scope, label="voice settings"))
    settings = VoiceSettings.from_scope(scope)

    def record() -> tuple[Any, str]:
        return record_take(MICROPHONE(), settings, CLOCK)

    try:
        take, ended = await run_blocking(record, label="voice take")
    except WakeUnavailable as exc:
        raise ProtocolError(PROVIDER_UNAVAILABLE, str(exc)) from None
    except Exception as exc:  # the audio library's own errors (no device, device busy)
        raise ProtocolError(
            PROVIDER_UNAVAILABLE, f"the microphone could not record: {exc}"
        ) from None
    if not take.usable:
        return VoiceListenResult(text="", reason="cancel" if ended == "cancel" else "no_speech")
    wav = take.wav()

    def save() -> Path:
        path = _clip_dir(scope) / f"take-{secrets.token_hex(8)}.wav"
        atomic_write_bytes(path, wav, mode=0o600)
        return path

    path = await run_blocking(save, label="voice take")
    try:
        heard = await stt.transcribe(path, scope=scope)
    except stt.SttError as exc:
        raise ProtocolError(PROVIDER_UNAVAILABLE, str(exc)) from None
    finally:
        with contextlib.suppress(OSError):
            path.unlink()
    if heard.inaudible or not heard.text.strip():
        return VoiceListenResult(text="", reason="inaudible")
    return VoiceListenResult(
        text=heard.text.strip(), reason="limit" if ended == "limit" else "spoke"
    )


VOICE_HANDLERS: Mapping[str, Callable[[Any, Any], Awaitable[ProtocolModel]]] = {
    "voice.transcribe": voice_transcribe,
    "voice.speak": voice_speak,
    "voice.listen": voice_listen,
}


def register(table: Any) -> None:
    """Add the handlers to a ``MethodTable``."""
    for name, handler in VOICE_HANDLERS.items():
        table.add(name, handler)
