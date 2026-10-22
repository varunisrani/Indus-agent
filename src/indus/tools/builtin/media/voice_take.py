"""One voice take: 16 kHz mono 16-bit audio until the speaker stops (P10-19, P24-20).

Shared by the classic REPL's voice mode (``indus.cli.voice``: push to talk) and the wake word's
hand-off on the other surfaces (``voice.listen``, ``indus.service.voice_rpc``: the TUI says "hey
indus", then speaks). ``VoiceSettings`` reads ``voice.*``; ``Capture`` keeps the blocks of one take
and says when it should end — the speaker paused for ``silence_seconds`` after speech, nobody spoke
within ``wait_seconds``, or the take reached ``max_seconds``; a short loud click does not count as
speech (``CONFIRM_SECONDS``). ``record_take`` runs one take on a ``Microphone`` (the wake word's
audio input seam) and returns it.
"""

from __future__ import annotations

import array
import io
import math
import sys
import threading
import wave
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from indus.foundation.scope import Scope
from indus.tools.builtin.media.common import setting
from indus.tools.builtin.media.wake_word import Microphone

__all__ = [
    "BLOCK_SECONDS",
    "CHANNELS",
    "DEFAULT_KEY",
    "SAMPLE_BYTES",
    "SAMPLE_RATE",
    "Capture",
    "StopReason",
    "VoiceSettings",
    "level",
    "record_take",
]

SAMPLE_RATE = 16_000
CHANNELS = 1
SAMPLE_BYTES = 2
BLOCK_SECONDS = 0.1
MIN_TAKE_SECONDS = 0.3
CONFIRM_SECONDS = 0.3
"""Loud audio needed before a take counts as speech (a click or a knock does not)."""
DIP_SECONDS = 0.3
"""Before speech is confirmed, a pause this long starts the count again."""
DEFAULT_KEY = "ctrl+space"


def _number(value: object, fallback: float, *, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return fallback
    number = float(value)
    if not math.isfinite(number) or number < low or number > high:
        return fallback
    return number


@dataclass(frozen=True)
class VoiceSettings:
    """``voice.*`` from the profile's config; odd values fall back to the defaults."""

    record_key: str = DEFAULT_KEY
    silence_threshold: float = 300.0
    silence_seconds: float = 2.0
    wait_seconds: float = 10.0
    max_seconds: float = 120.0
    read_aloud: bool = False

    @classmethod
    def from_scope(cls, scope: Scope | None) -> VoiceSettings:
        base = cls()

        def get(name: str) -> object:
            return setting(scope, f"voice.{name}")

        key = get("record_key")
        return cls(
            record_key=key.strip() if isinstance(key, str) and key.strip() else base.record_key,
            silence_threshold=_number(
                get("silence_threshold"), base.silence_threshold, low=1, high=32767
            ),
            silence_seconds=_number(get("silence_seconds"), base.silence_seconds, low=0.2, high=60),
            wait_seconds=_number(get("wait_seconds"), base.wait_seconds, low=1, high=600),
            max_seconds=_number(get("max_seconds"), base.max_seconds, low=1, high=3600),
            read_aloud=get("read_aloud") is True,
        )


def level(block: bytes) -> float:
    """Root mean square of a block of signed 16-bit little-endian samples."""
    usable = len(block) - len(block) % SAMPLE_BYTES
    if usable <= 0:
        return 0.0
    samples = array.array("h")
    samples.frombytes(block[:usable])
    if sys.byteorder == "big":
        samples.byteswap()
    return math.sqrt(sum(s * s for s in samples) / len(samples))


type StopReason = Literal["released", "silence", "no_speech", "limit", "card", "cancel"]


class Capture:
    """One take: the blocks so far and what the levels say about them."""

    def __init__(self, settings: VoiceSettings, started: float) -> None:
        self.settings = settings
        self.started = started
        self.blocks: list[bytes] = []
        self.frames = 0
        self.peak = 0.0
        self.heard = False
        self._loud = 0.0
        self._dip = 0.0
        self._quiet_since: float | None = None

    @property
    def seconds(self) -> float:
        return self.frames / SAMPLE_RATE

    @property
    def usable(self) -> bool:
        return self.heard and self.seconds >= MIN_TAKE_SECONDS

    def add(self, block: bytes, now: float) -> StopReason | None:
        """Keep ``block`` (arrived at ``now``); a reason when the take should end here."""
        self.blocks.append(block)
        frames = len(block) // (SAMPLE_BYTES * CHANNELS)
        self.frames += frames
        span = frames / SAMPLE_RATE
        loudness = level(block)
        self.peak = max(self.peak, loudness)
        rules = self.settings
        if loudness >= rules.silence_threshold:
            self._dip = 0.0
            self._quiet_since = None
            if not self.heard:
                self._loud += span
                self.heard = self._loud >= CONFIRM_SECONDS - 1e-6
        elif self.heard:
            if self._quiet_since is None:
                self._quiet_since = now - span
            if now - self._quiet_since >= rules.silence_seconds - 1e-6:
                return "silence"
        else:
            if self._loud:
                self._dip += span
                if self._dip >= DIP_SECONDS - 1e-6:
                    self._loud = self._dip = 0.0
            if now - self.started >= rules.wait_seconds - 1e-6:
                return "no_speech"
        if now - self.started >= rules.max_seconds - 1e-6:
            return "limit"
        return None

    def wav(self) -> bytes:
        out = io.BytesIO()
        with wave.open(out, "wb") as writer:
            writer.setnchannels(CHANNELS)
            writer.setsampwidth(SAMPLE_BYTES)
            writer.setframerate(SAMPLE_RATE)
            writer.writeframes(b"".join(self.blocks))
        return out.getvalue()


def record_take(
    microphone: Microphone,
    settings: VoiceSettings,
    clock: Callable[[], float],
    *,
    stop: threading.Event | None = None,
) -> tuple[Capture, StopReason]:
    """Record one take from ``microphone`` until ``Capture`` ends it (or ``stop`` is set, or the
    stream ends: ``cancel``). The stream is always closed."""
    started = clock()
    take = Capture(settings, started)
    stream = microphone.open(SAMPLE_RATE, int(SAMPLE_RATE * BLOCK_SECONDS))
    try:
        while True:
            if stop is not None and stop.is_set():
                return take, "cancel"
            block = stream.read()
            if block is None:
                return take, "cancel"
            reason = take.add(block, clock())
            if reason is not None:
                return take, reason
    finally:
        stream.close()
