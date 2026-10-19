"""Fake media providers and a fake ``ffmpeg`` for the media tool tests (P10-16..19).

- ``FakeImageProvider`` / ``FakeVideoProvider``: record every request, answer with bytes (or a
  URL / a file), can hold a call open on an event so tests can count calls in flight.
- ``FakeSpeech``: a ``TtsProvider`` returning recognisable bytes per piece (``piece:<n>|``).
- ``FakeStt``: an ``SttProvider`` answering from a script, recording each file it got and that
  file's duration.
- ``install_fake_ffmpeg(directory)``: writes an executable ``ffmpeg`` there that copies its input
  to its output behind an ``OggS`` header; put the directory on the scope's ``PATH``.
"""

from __future__ import annotations

import asyncio
import stat
import sys
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from indus.foundation.scope import Scope
from indus.tools.builtin.media import audio
from indus.tools.builtin.media.providers import (
    ImageCapabilities,
    ImageRequest,
    MediaResult,
    ProviderFailed,
    VideoCapabilities,
    VideoRequest,
)
from indus.tools.builtin.media.stt import SttError, Transcript


def _tiny_png() -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(kind + body).to_bytes(4, "big")
        return len(body).to_bytes(4, "big") + kind + body + crc

    header = (1).to_bytes(4, "big") * 2 + bytes([8, 2, 0, 0, 0])
    pixels = zlib.compress(b"\x00\xff\x00\x00")
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")
    )


PNG_BYTES = _tiny_png()


@dataclass
class FakeImageProvider:
    name: str = "fake"
    title: str = "Fake images"
    default_model: str = "fake-model-1"
    capabilities: ImageCapabilities = field(default_factory=ImageCapabilities)
    ready: bool = True
    result: Callable[[ImageRequest], Any] | None = None
    hold: asyncio.Event | None = None
    requests: list[ImageRequest] = field(default_factory=list[ImageRequest])
    in_flight: int = 0
    peak: int = 0

    def missing(self, scope: Scope | None) -> str | None:
        return None if self.ready else "FAKE_IMAGE_KEY is not set"

    async def generate(self, request: ImageRequest, *, scope: Scope | None) -> MediaResult:
        self.requests.append(request)
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            if self.hold is not None:
                await self.hold.wait()
            if self.result is not None:
                return self.result(request)
            return MediaResult(data=PNG_BYTES)
        finally:
            self.in_flight -= 1


@dataclass
class FakeVideoProvider:
    name: str = "fakevideo"
    title: str = "Fake video"
    default_model: str = "fake-video-1"
    capabilities: VideoCapabilities = field(default_factory=VideoCapabilities)
    ready: bool = True
    requests: list[VideoRequest] = field(default_factory=list[VideoRequest])

    def missing(self, scope: Scope | None) -> str | None:
        return None if self.ready else "FAKE_VIDEO_KEY is not set"

    async def generate(self, request: VideoRequest, *, scope: Scope | None) -> MediaResult:
        self.requests.append(request)
        return MediaResult(data=b"\x00\x00\x00\x18ftypmp42fake-video")


@dataclass
class FakeSpeech:
    name: str = "fakespeech"
    title: str = "Fake speech"
    max_chars: int = 4096
    speed_range: tuple[float, float] = (0.25, 4.0)
    formats: tuple[str, ...] = ("mp3", "wav")
    calls: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    body: Callable[[int, str], bytes] | None = None
    fail_on: int | None = None

    def missing(self, scope: Scope | None) -> str | None:
        return None

    async def synthesize(
        self,
        text: str,
        voice: str,
        speed: float,
        fmt: str,
        *,
        scope: Scope | None,
        instructions: str | None = None,
    ) -> bytes:
        index = len(self.calls) + 1
        self.calls.append(
            {"text": text, "voice": voice, "speed": speed, "fmt": fmt, "instructions": instructions}
        )
        if self.fail_on == index:
            raise ProviderFailed("the fake engine broke")
        if self.body is not None:
            return self.body(index, fmt)
        return f"piece:{index}|".encode()


@dataclass
class FakeStt:
    name: str = "fakestt"
    title: str = "Fake transcription"
    max_bytes: int | None = 25 * 1024 * 1024
    max_seconds: float | None = None
    replies: list[Any] = field(default_factory=list[Any])
    seen: list[Path] = field(default_factory=list[Path])
    durations: list[float] = field(default_factory=list[float])

    def missing(self, scope: Scope | None) -> str | None:
        return None

    async def transcribe(
        self, path: Path, language: str | None, *, scope: Scope | None
    ) -> Transcript:
        self.seen.append(path)
        if audio.is_wav(path.read_bytes()[:12]):
            self.durations.append(audio.wav_facts(path).duration)
        reply = self.replies.pop(0) if self.replies else f"part {len(self.seen)}"
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, Transcript):
            return reply
        return Transcript(str(reply), language or "en", None, provider=self.name)


def failing_stt(message: str) -> SttError:
    return SttError(message)


FAKE_FFMPEG = """#!{python}
import sys
args = sys.argv[1:]
source = args[args.index("-i") + 1]
target = args[-1]
with open(source, "rb") as handle:
    data = handle.read()
with open(target, "wb") as handle:
    handle.write(b"OggS" + data)
"""


def install_fake_ffmpeg(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    program = directory / "ffmpeg"
    program.write_text(FAKE_FFMPEG.format(python=sys.executable))
    program.chmod(program.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return program
