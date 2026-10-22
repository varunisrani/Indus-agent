"""P24-35 integration: the daemon's ``voice.*`` methods over the P10 speech providers."""

from __future__ import annotations

import asyncio
import base64
import struct
import threading
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.protocol.errors import INVALID_PARAMS, PROVIDER_UNAVAILABLE
from indus.protocol.methods import ProtocolError
from indus.protocol.voice import (
    VoiceListenParams,
    VoiceListenResult,
    VoiceSpeakParams,
    VoiceTranscribeParams,
)
from indus.service import voice_rpc
from indus.service.voice_rpc import voice_listen, voice_speak, voice_transcribe
from indus.tools.builtin.media import stt, tts, wake_word
from tests.unit.backends._aio import aio


@dataclass
class _Ctx:
    scope: Scope


def _clip(data: bytes = b"RIFF fake wav") -> str:
    return base64.b64encode(data).decode("ascii")


@aio
async def test_a_clip_is_transcribed_from_a_private_file_that_is_removed(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[Path, bytes, int]] = []

    async def fake(path: Path, *, scope: Scope | None, **_: Any) -> stt.Transcript:
        seen.append((path, path.read_bytes(), path.stat().st_mode & 0o777))
        return stt.Transcript("hello there")

    monkeypatch.setattr(stt, "transcribe", fake)
    params = VoiceTranscribeParams(audio_b64=_clip(), mime="audio/wav; codecs=1")
    answer = await voice_transcribe(_Ctx(scope), params)
    assert answer.text == "hello there"
    ((path, data, mode),) = seen
    assert data == b"RIFF fake wav" and mode == 0o600 and path.suffix == ".wav"
    assert path.parent == scope.home / "cache" / "voice" and not path.exists()


@aio
async def test_bad_audio_and_a_failing_provider_are_typed_errors(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ProtocolError) as bad:
        await voice_transcribe(_Ctx(scope), VoiceTranscribeParams(audio_b64="%%%", mime="x"))
    assert bad.value.error == INVALID_PARAMS

    async def failing(path: Path, **_: Any) -> stt.Transcript:
        raise stt.SttError("no transcription provider is set up")

    monkeypatch.setattr(stt, "transcribe", failing)
    with pytest.raises(ProtocolError) as down:
        await voice_transcribe(_Ctx(scope), VoiceTranscribeParams(audio_b64=_clip(), mime="x"))
    assert down.value.error == PROVIDER_UNAVAILABLE
    assert list((scope.home / "cache" / "voice").iterdir()) == []


@aio
async def test_speech_comes_back_as_base64_and_the_file_goes(
    scope: Scope, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    made = tmp_path / "tts-1.mp3"

    async def speak(self: tts.SpeechTool, text: str, scope: Scope, **_: Any) -> tts.Speech:
        made.write_bytes(b"ID3 " + text.encode())
        return tts.Speech(made, "mp3", "fake", 1, False)

    monkeypatch.setattr(tts.SpeechTool, "speak", speak)
    answer = await voice_speak(_Ctx(scope), VoiceSpeakParams(text="read me"))
    assert base64.b64decode(answer.audio_b64) == b"ID3 read me"
    assert answer.mime == "audio/mpeg" and not made.exists()


@aio
async def test_no_speech_engine_is_provider_unavailable(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def speak(self: tts.SpeechTool, text: str, scope: Scope, **_: Any) -> tts.Speech:
        raise tts.SpeechError("speech engine 'openai' is not ready: no key")

    monkeypatch.setattr(tts.SpeechTool, "speak", speak)
    with pytest.raises(ProtocolError) as down:
        await voice_speak(_Ctx(scope), VoiceSpeakParams(text="x"))
    assert down.value.error == PROVIDER_UNAVAILABLE


# -- voice.listen: the wake word's hand-off for the terminal UI (P24-20 wiring, D-16.799) ---------

BLOCK = 1_600  # 0.1 s at 16 kHz
LOUD_BLOCK = struct.pack("<h", 3000) * BLOCK
QUIET_BLOCK = b"\x00\x00" * BLOCK


class ScriptedMicrophone:
    """One input device: each ``open`` plays the next script; never two streams at once."""

    def __init__(self, *scripts: list[bytes]) -> None:
        self.scripts = list(scripts)
        self.opened: list[tuple[int, int]] = []
        self.live = 0
        self.now = 0.0

    def open(self, sample_rate: int, frame_length: int) -> Stream:
        assert self.live == 0, "the microphone is already open"
        self.opened.append((sample_rate, frame_length))
        self.live += 1
        return Stream(self, list(self.scripts.pop(0)) if self.scripts else [])

    def clock(self) -> float:
        return self.now


class Stream:
    def __init__(self, mic: ScriptedMicrophone, blocks: list[bytes]) -> None:
        self.mic = mic
        self.blocks = blocks
        self.closed = False

    def read(self) -> bytes | None:
        if self.closed or not self.blocks:
            return None
        block = self.blocks.pop(0)
        self.mic.now += len(block) / 2 / 16_000
        return block

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.mic.live -= 1


def _take(loud_s: float, quiet_s: float) -> list[bytes]:
    return [LOUD_BLOCK] * round(loud_s * 10) + [QUIET_BLOCK] * round(quiet_s * 10)


@pytest.fixture
def heard(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, Path]]:
    seen: list[tuple[int, Path]] = []

    async def fake(path: Path, *, scope: Scope | None, **_: Any) -> stt.Transcript:
        with wave.open(str(path), "rb") as reader:
            assert reader.getframerate() == 16_000 and reader.getnchannels() == 1
            seen.append((reader.getnframes(), path))
        return stt.Transcript("  what is on my calendar  ")

    monkeypatch.setattr(stt, "transcribe", fake)
    return seen


def _use(monkeypatch: pytest.MonkeyPatch, mic: ScriptedMicrophone) -> None:
    monkeypatch.setattr(voice_rpc, "MICROPHONE", lambda: mic)
    monkeypatch.setattr(voice_rpc, "CLOCK", mic.clock)


@aio
async def test_listen_records_until_the_pause_after_speech_and_transcribes(
    scope: Scope, monkeypatch: pytest.MonkeyPatch, heard: list[tuple[int, Path]]
) -> None:
    mic = ScriptedMicrophone(_take(0.6, 3.0))
    _use(monkeypatch, mic)
    answer = await voice_listen(_Ctx(scope), VoiceListenParams())
    assert answer.text == "what is on my calendar" and answer.reason == "spoke"
    ((frames, path),) = heard
    assert frames == round(2.6 * 16_000)  # 0.6 s of speech, then the 2 s pause ended it
    assert not path.exists() and path.parent == scope.home / "cache" / "voice"
    assert mic.opened == [(16_000, BLOCK)] and mic.live == 0


@aio
async def test_listen_without_speech_sends_nothing_to_the_provider(
    scope: Scope, monkeypatch: pytest.MonkeyPatch, heard: list[tuple[int, Path]]
) -> None:
    mic = ScriptedMicrophone(_take(0.0, 12.0))
    _use(monkeypatch, mic)
    answer = await voice_listen(_Ctx(scope), VoiceListenParams())
    assert answer == VoiceListenResult(text="", reason="no_speech")
    assert heard == [] and mic.live == 0


@aio
async def test_listen_without_a_microphone_is_a_typed_error(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing() -> Any:
        raise wake_word.WakeUnavailable("No audio input here: uv sync --extra voice.")

    monkeypatch.setattr(voice_rpc, "MICROPHONE", missing)
    with pytest.raises(ProtocolError) as down:
        await voice_listen(_Ctx(scope), VoiceListenParams())
    assert down.value.error == PROVIDER_UNAVAILABLE and "extra voice" in down.value.message


def test_wake_detection_hands_the_microphone_to_one_take_then_listens_again(
    scope: Scope, monkeypatch: pytest.MonkeyPatch, heard: list[tuple[int, Path]]
) -> None:
    """The terminal's flow over the real handlers: ``wake.start`` (consent) opens the microphone;
    the phrase pauses the listener (stream closed) and sends ``wake.detected``; ``voice.listen``
    records one take on the same device; ``wake.resume`` opens it for the phrase again."""
    from indus.foundation import config
    from indus.protocol.wake import WakeNoParams, WakeStartParams
    from indus.service import wake_rpc
    from indus.tools.builtin.media.wake_word import FRAME_LENGTH, WakeService

    model = scope.home / "hey_indus.onnx"
    model.write_bytes(b"model")
    monkeypatch.setattr(wake_word, "_module_present", lambda name: True)
    config.save(scope, {"wake_word": {"model": str(model)}})

    class Engine:
        name = "fake"
        frame_length = FRAME_LENGTH

        def score(self, frame: bytes) -> float:
            return struct.unpack("<h", frame[:2])[0] / 1000

    phrase = struct.pack("<h", 900) + b"\x00" * (FRAME_LENGTH * 2 - 2)
    mic = ScriptedMicrophone([phrase] * 4, _take(0.5, 2.5), [])
    _use(monkeypatch, mic)
    events: list[tuple[object, str]] = []
    detected = threading.Event()

    def emit(owner: object, name: str, payload: Any) -> None:
        events.append((owner, name))
        detected.set()

    rpc = wake_rpc.WakeRpc(scope, emit=emit)
    rpc.service = WakeService(
        lambda: wake_rpc.settings_for(scope),
        on_detect=rpc._detected,  # pyright: ignore[reportPrivateUsage]
        microphone=mic,
        engine_factory=lambda s: Engine(),
    )
    ctx = _Ctx(scope)
    started = asyncio.run(rpc.start(ctx, WakeStartParams(surface="tui", consent=True)))
    assert started.started and started.capture == "local"
    assert detected.wait(5)
    rpc.service.wait_idle()
    assert events == [(ctx, "wake.detected")] and mic.live == 0  # the listener let go
    answer = asyncio.run(voice_listen(ctx, VoiceListenParams()))
    assert answer.text == "what is on my calendar"
    assert asyncio.run(rpc.resume(ctx, WakeNoParams())).ok
    assert [size for _rate, size in mic.opened] == [FRAME_LENGTH, BLOCK, FRAME_LENGTH]
    rpc.connection_closed(ctx)
    assert mic.live == 0
