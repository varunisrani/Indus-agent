"""P24-35 integration: the daemon's ``voice.*`` methods over the P10 speech providers."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.protocol.errors import INVALID_PARAMS, PROVIDER_UNAVAILABLE
from indus.protocol.methods import ProtocolError
from indus.protocol.voice import VoiceSpeakParams, VoiceTranscribeParams
from indus.service.voice_rpc import voice_speak, voice_transcribe
from indus.tools.builtin.media import stt, tts
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
