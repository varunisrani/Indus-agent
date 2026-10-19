"""P15-23: voice messages become a transcript or an honest note (R06.23)."""

from __future__ import annotations

import asyncio
import wave
from pathlib import Path
from typing import Any

from indus.foundation.scope import Scope
from indus.gateway.core.events import Attachment, InboundEvent, MessageKind, Source
from indus.gateway.intake.media import MediaCache, MediaIntake
from indus.gateway.intake.voice import (
    LOCAL_PROVIDER,
    VoiceIntake,
    inaudible_note,
    untranscribed_note,
)
from indus.tools.builtin.media.stt import INAUDIBLE_TEXT, SttError, Transcript
from tests.fakes.agent import DictConfig
from tests.fakes.clock import checkpoint


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class FakeStt:
    """Answers per provider: a text, "" or an exception; records every call."""

    def __init__(self, **answers: object) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str | None]] = []
        self.gate: asyncio.Event | None = None

    async def __call__(self, path: Path, provider: str | None) -> Transcript:
        self.calls.append((path.name, provider))
        if self.gate is not None:
            await self.gate.wait()
        answer = self.answers.get(provider or "configured", "")
        if isinstance(answer, Exception):
            raise answer
        if answer == INAUDIBLE_TEXT:
            return Transcript(INAUDIBLE_TEXT, inaudible=True, provider=provider or "x")
        return Transcript(str(answer), provider=provider or "x")


def clip(folder: Path, name: str = "voice.ogg", data: bytes = b"OggS-fake-audio") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(data)
    return path


def wav(folder: Path, seconds: float = 3.0) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "note.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * int(8000 * seconds))
    return path


def test_a_transcript_is_a_plain_quoted_line(scope: Scope, tmp_path: Path) -> None:
    stt = FakeStt(configured="  turn the lights off  ")
    voice = VoiceIntake(scope, transcriber=stt)
    notes = run(voice.notes_for([clip(tmp_path)]))
    assert notes == ['"turn the lights off"']
    assert stt.calls == [("voice.ogg", None)]


def test_empty_or_inaudible_transcripts_say_so(scope: Scope, tmp_path: Path) -> None:
    path = clip(tmp_path)
    for answer in ("", "   ", INAUDIBLE_TEXT):
        voice = VoiceIntake(scope, transcriber=FakeStt(configured=answer))
        note = run(voice.note_for(path))
        assert note == inaudible_note(path)
        assert "ask them to send it again" in note and "Do not guess" in note


def test_a_failed_provider_falls_back_to_the_local_one(scope: Scope, tmp_path: Path) -> None:
    path = clip(tmp_path)
    stt = FakeStt(configured=SttError("401 from the endpoint"), local="call mum")
    assert run(VoiceIntake(scope, transcriber=stt).note_for(path)) == '"call mum"'
    assert stt.calls == [("voice.ogg", None), ("voice.ogg", LOCAL_PROVIDER)]

    both = FakeStt(configured=SttError("down"), local=RuntimeError("no voice extra"))
    assert run(VoiceIntake(scope, transcriber=both).note_for(path)) == inaudible_note(path)

    local_only = scope.with_config(DictConfig({"stt": {"provider": "local"}}))
    once = FakeStt(configured=SttError("model missing"))
    assert run(VoiceIntake(local_only, transcriber=once).note_for(path)) == inaudible_note(path)
    assert len(once.calls) == 1  # the configured provider is the local one: no second try


def test_stt_off_gives_the_path_and_length(scope: Scope, tmp_path: Path) -> None:
    off = scope.with_config(DictConfig({"gateway": {"stt_enabled": False}}))
    stt = FakeStt(configured="never")
    voice = VoiceIntake(off, transcriber=stt)
    path = wav(tmp_path, 3.0)
    assert run(voice.notes_for([path])) == [untranscribed_note(path, 3.0)]
    assert "(3 s)" in untranscribed_note(path, 3.0)
    other = clip(tmp_path)
    assert run(voice.notes_for([other], [12.4])) == [untranscribed_note(other, 12.4)]
    assert run(voice.notes_for([other])) == [f"[The person sent a voice message: {other}]"]
    assert stt.calls == []


def test_clips_are_transcribed_once(scope: Scope, tmp_path: Path) -> None:
    stt = FakeStt(configured="hello")
    voice = VoiceIntake(scope, transcriber=stt)
    a, b = clip(tmp_path, "a.ogg"), clip(tmp_path, "b.ogg", b"OggS-other")

    async def main() -> None:
        assert await voice.notes_for([a, b, a]) == ['"hello"', '"hello"']
        assert await voice.notes_for([a, b]) == ['"hello"', '"hello"']
        # two asks for a new clip at once share one call
        c = clip(tmp_path, "c.ogg", b"OggS-third")
        stt.gate = asyncio.Event()
        first = asyncio.ensure_future(voice.note_for(c))
        second = asyncio.ensure_future(voice.note_for(c))
        for _ in range(5):
            await checkpoint()
        assert len(stt.calls) == 3  # the second ask waits on the first call
        stt.gate.set()
        assert await first == await second == '"hello"'

    run(main())
    assert sorted(stt.calls) == [("a.ogg", None), ("b.ogg", None), ("c.ogg", None)]


def test_a_merged_burst_is_transcribed_once_per_clip(scope: Scope) -> None:
    stt = FakeStt(configured="pick up bread")
    cache = MediaCache(scope)
    intake = MediaIntake(scope, cache=cache, voice=VoiceIntake(scope, transcriber=stt))

    async def main() -> None:
        first = await cache.save("audio", b"OggS-one", name="one.ogg")
        second = await cache.save("audio", b"OggS-two", name="two.ogg")
        source = Source(platform="telegram", chat_id="200", user_id="200")
        event = InboundEvent(
            source=source,
            kind=MessageKind.VOICE,
            media=(Attachment(ref=str(first), mime="audio/ogg"),),
        )
        assert await intake.enrich(event) == '"pick up bread"'
        merged = event.model_copy(
            update={"media": (*event.media, Attachment(ref=str(second), mime="audio/ogg"))}
        )
        text = await intake.enrich(merged)
        assert text == '"pick up bread"\n\n"pick up bread"'

    run(main())
    assert len(stt.calls) == 2  # one per clip, never twice
