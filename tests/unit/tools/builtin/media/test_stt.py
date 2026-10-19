"""Speech-to-text library: caps, silence cuts, inaudible flag, provider errors (P10-18)."""

from __future__ import annotations

import asyncio
import math
import wave
from array import array
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from indus.foundation.scope import EnvView, Scope
from indus.tools.builtin.media import audio
from indus.tools.builtin.media.providers import ProviderRegistry
from indus.tools.builtin.media.stt import (
    INAUDIBLE_TEXT,
    SttError,
    SttProvider,
    Transcript,
    default_registry,
    transcribe,
)
from tests.fakes.agent import DictConfig
from tests.fakes.media_providers import FakeStt
from tests.fakes.secret_store import MemorySecretStore

RATE = 8000
MB = 1024 * 1024


def profile(scope: Scope, config: Mapping[str, Any] | None = None, **keys: str) -> Scope:
    target = scope.with_config(DictConfig(config or {})).with_env(EnvView({}))
    return target.with_secrets(MemorySecretStore(dict(keys)))


def registry_of(*providers: SttProvider) -> ProviderRegistry[SttProvider]:
    return ProviderRegistry[SttProvider]("transcription", list(providers))


def run(path: Path, scope: Scope, **kwargs: Any) -> Transcript:
    return asyncio.run(transcribe(path, scope=scope, **kwargs))


def speech_wav(path: Path, seconds: float, pauses: list[tuple[float, float]]) -> Path:
    """A tone at about -12 dBFS with silent stretches ``(start, end)`` in seconds."""
    samples = array("h")
    for n in range(int(seconds * RATE)):
        t = n / RATE
        quiet = any(start <= t < end for start, end in pauses)
        samples.append(0 if quiet else int(8000 * math.sin(2 * math.pi * 220 * t)))
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(RATE)
        writer.writeframes(samples.tobytes())
    return path


def test_cap_refuses_30mb_non_wav(scope: Scope, tmp_path: Path) -> None:
    big = tmp_path / "memo.mp3"
    with big.open("wb") as handle:
        handle.write(b"ID3\x04")
        handle.truncate(30 * MB)
    fake = FakeStt(max_bytes=25 * MB)
    with pytest.raises(SttError) as caught:
        run(big, profile(scope), registry=registry_of(fake))
    assert "30.0 MB" in str(caught.value) and "at most 25 MB" in str(caught.value)
    assert fake.seen == []


def test_three_minute_wav_split_near_silences(scope: Scope, tmp_path: Path) -> None:
    pauses = [(50.0, 51.0), (100.0, 101.0), (140.0, 141.0)]
    recording = speech_wav(tmp_path / "talk.wav", 180.0, pauses)
    fake = FakeStt(max_seconds=60.0)
    result = run(recording, profile(scope), registry=registry_of(fake))
    cuts = [sum(fake.durations[: i + 1]) for i in range(len(fake.durations) - 1)]
    assert len(cuts) == 3
    for cut, (start, end) in zip(cuts, pauses, strict=True):
        assert start <= cut <= end
    assert all(d <= 60.0 for d in fake.durations)
    assert result.text == "part 1 part 2 part 3 part 4"
    assert result.pieces == 4 and result.inaudible is False
    assert result.duration == pytest.approx(180.0)
    assert list((scope.home / "cache" / "audio").iterdir()) == []  # pieces cleaned up


def test_cut_points_without_pauses_and_lookback() -> None:
    assert audio.cut_points(180.0, 60.0, []) == [60.0, 120.0]
    # A pause further back than the lookback window (a third of 60 s) is not used.
    assert audio.cut_points(100.0, 60.0, [30.0]) == [60.0]
    assert audio.cut_points(100.0, 60.0, [45.0, 55.0]) == [55.0]
    assert audio.cut_points(1000.0, 400.0, [300.0])[0] == 400.0  # lookback capped at 60 s


def test_wav_over_byte_cap_is_cut(scope: Scope, tmp_path: Path) -> None:
    recording = speech_wav(tmp_path / "long.wav", 70.0, [(50.0, 51.0)])  # about 1.1 MB
    fake = FakeStt(max_bytes=MB)
    sizes: list[int] = []
    original = fake.transcribe

    async def measuring(path: Path, language: str | None, *, scope: Scope | None) -> Transcript:
        sizes.append(path.stat().st_size)
        return await original(path, language, scope=scope)

    fake.transcribe = measuring  # type: ignore[method-assign]
    result = run(recording, profile(scope), registry=registry_of(fake))
    assert result.pieces == 2 and all(size <= MB for size in sizes)
    assert fake.durations[0] == pytest.approx(50.5, abs=0.1)


def test_empty_is_inaudible(scope: Scope, tmp_path: Path) -> None:
    recording = speech_wav(tmp_path / "quiet.wav", 2.0, [])
    result = run(recording, profile(scope), registry=registry_of(FakeStt(replies=["  "])))
    assert result.inaudible is True and result.text == INAUDIBLE_TEXT and result.text
    split = speech_wav(tmp_path / "split.wav", 130.0, [])
    result = run(
        split, profile(scope), registry=registry_of(FakeStt(max_seconds=60.0, replies=["", "", ""]))
    )
    assert result.inaudible is True and result.pieces == 3


def test_provider_error_mapped_to_plain_message(scope: Scope, tmp_path: Path) -> None:
    recording = speech_wav(tmp_path / "a.wav", 1.0, [])
    fake = FakeStt(replies=[RuntimeError("socket closed")])
    with pytest.raises(SttError, match="Fake transcription failed: RuntimeError: socket closed"):
        run(recording, profile(scope), registry=registry_of(fake))
    split = speech_wav(tmp_path / "b.wav", 130.0, [])
    broken = FakeStt(max_seconds=60.0, replies=["ok", SttError("quota used up")])
    with pytest.raises(SttError, match="piece 2 of 3: quota used up"):
        run(split, profile(scope), registry=registry_of(broken))


def test_secret_missing_and_empty_files_refused(scope: Scope, tmp_path: Path) -> None:
    secret_file = tmp_path / ".env"
    secret_file.write_text("KEY=1\n")
    fake = FakeStt()
    for path, words in (
        (secret_file, "secret-bearing"),
        (tmp_path / "nothing.wav", "no audio file"),
    ):
        with pytest.raises(SttError, match=words):
            run(path, profile(scope), registry=registry_of(fake))
    empty = tmp_path / "empty.ogg"
    empty.write_bytes(b"")
    with pytest.raises(SttError, match="empty"):
        run(empty, profile(scope), registry=registry_of(fake))
    assert fake.seen == []


def test_auto_choice_and_not_installed(
    scope: Scope, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib.util

    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, *a: None if name == "faster_whisper" else real(name, *a),
    )
    recording = speech_wav(tmp_path / "a.wav", 1.0, [])
    with pytest.raises(SttError, match="install the `voice` extra"):
        run(recording, profile(scope), registry=default_registry())
    with pytest.raises(SttError, match="'nope' is not a transcription provider"):
        run(recording, profile(scope), registry=default_registry(), provider="nope")

    with respx.mock:
        route = respx.post("https://api.groq.com/openai/v1/audio/transcriptions").mock(
            return_value=httpx.Response(200, json={"text": "hello", "language": "en"})
        )
        groq_key = "test-" + "groq-key"
        result = run(recording, profile(scope, GROQ_API_KEY=groq_key), registry=default_registry())
        assert (result.text, result.provider, result.language) == ("hello", "groq", "en")
        assert route.calls.last.request.headers["authorization"] == f"Bearer {groq_key}"
        assert b"whisper-large-v3-turbo" in route.calls.last.request.content


@respx.mock
def test_openai_request_and_errors(scope: Scope, tmp_path: Path) -> None:
    recording = speech_wav(tmp_path / "a.wav", 1.0, [])
    key = "test-" + "stt-key"
    target = profile(scope, {"stt": {"provider": "openai", "language": "de"}}, OPENAI_API_KEY=key)
    route = respx.post("https://api.openai.com/v1/audio/transcriptions").mock(
        return_value=httpx.Response(
            200, json={"text": " Guten Tag ", "language": "german", "duration": 1.0}
        )
    )
    result = run(recording, target, registry=default_registry())
    assert (result.text, result.language, result.duration) == ("Guten Tag", "german", 1.0)
    content = route.calls.last.request.content
    for part in (b"whisper-1", b"verbose_json", b'name="language"', b"de", b'filename="a.wav"'):
        assert part in content

    for status, words in ((401, "refused the key"), (429, "rate limiting"), (413, "too large")):
        route.mock(return_value=httpx.Response(status, text="nope"))
        with pytest.raises(SttError, match=words):
            run(recording, target, registry=default_registry())
    route.mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(SttError, match="did not answer in time"):
        run(recording, target, registry=default_registry())
