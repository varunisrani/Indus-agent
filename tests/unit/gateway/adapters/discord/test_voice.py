"""P16-35: Discord voice channels over a fake voice link and a fake clock (no discord.py)."""

from __future__ import annotations

import asyncio
import struct
import threading
import wave
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from indus.gateway.adapters.discord import DiscordAdapter
from indus.gateway.adapters.discord import voice as v
from indus.gateway.adapters.discord.adapter import VOICE_WORKING
from indus.gateway.config import GLOBAL_ALLOWED_USERS_ENV, policy_env
from indus.gateway.core.events import InboundEvent
from tests.fakes.clock import FakeClock
from tests.unit.gateway.adapters.discord.test_adapter import World, settings, world

START = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
ALLOWED_ENV = policy_env("discord").allowed_users
ANA, BEN, EVE = "200", "201", "666"
VOICE = 500


async def until(clock: FakeClock, ready: Callable[[], bool], seconds: float = 10.0) -> None:
    """Wait in real time (the fake clock does not move) for the io pool's WAV writes."""
    for _ in range(int(seconds / 0.002)):
        if ready():
            return
        await asyncio.to_thread(threading.Event().wait, 0.002)
        await clock.run_until_idle()
    raise AssertionError("the voice pipeline never settled")


async def polls(clock: FakeClock, seconds: float) -> None:
    """Move the fake clock ``seconds`` forward one listen-loop poll at a time."""
    for _ in range(round(seconds / v.POLL_S)):
        await clock.advance_and_settle(v.POLL_S)


def frame(level: int = 1000) -> bytes:
    return struct.pack("<h", level) * (v.FRAME_BYTES // 2)


# -- fakes -----------------------------------------------------------------------------------------


class FakeConnection:
    def __init__(self, channel: Any, on_audio: Callable[[str, bytes], None]) -> None:
        self.channel = channel
        self.on_audio = on_audio
        self.plays = 0
        self.keepalives = 0
        self.closed = False
        self.mixer: v.VoiceMixer | None = None

    def talk(self, user_id: str, seconds: float) -> None:
        for _ in range(round(seconds * 1000 / v.FRAME_MS)):
            self.on_audio(user_id, frame())

    def play(self, mixer: v.VoiceMixer) -> None:
        self.plays += 1
        self.mixer = mixer

    def keepalive(self) -> None:
        self.keepalives += 1

    async def close(self) -> None:
        self.closed = True


@dataclass
class FakeLink:
    connections: list[FakeConnection] = field(default_factory=list[FakeConnection])
    error: Exception | None = None

    async def connect(self, channel: Any, on_audio: Callable[[str, bytes], None]) -> Any:
        if self.error is not None:
            raise self.error
        made = FakeConnection(channel, on_audio)
        self.connections.append(made)
        return made

    @property
    def last(self) -> FakeConnection:
        return self.connections[-1]


@dataclass
class Speech:
    """The speech services: canned transcripts per speaker, recorded syntheses."""

    folder: Path
    texts: dict[str, str] = field(default_factory=dict[str, str])
    heard: list[str] = field(default_factory=list[str])
    spoken: list[str] = field(default_factory=list[str])
    ffmpeg: str | None = "/usr/bin/ffmpeg"
    libraries: str | None = None
    wavs: list[bytes] = field(default_factory=list[bytes])

    def deps(self, link: FakeLink) -> v.VoiceDeps:
        async def transcribe(path: Path) -> str:
            speaker = path.name.split("-")[2]
            self.heard.append(speaker)
            self.wavs.append(path.read_bytes())
            return self.texts.get(speaker, "")

        async def synthesize(text: str) -> Path:
            self.spoken.append(text)
            made = self.folder / f"tts-{len(self.spoken)}.wav"
            made.write_bytes(b"audio")
            return made

        async def decode(path: Path) -> bytes:
            assert path.read_bytes() == b"audio"
            return frame(2000) * 5

        return v.VoiceDeps(
            transcribe=transcribe,
            synthesize=synthesize,
            decode=decode,
            ffmpeg=lambda: self.ffmpeg,
            workdir=lambda: self.folder,
            libraries=lambda: self.libraries,
            link=link,
        )


class Host:
    """``VoiceHost``: allowed people by id, recorded events and channel posts."""

    platform: ClassVar[str] = "discord"
    sdk: Any = None

    def __init__(self, clock: FakeClock, allowed: set[str]) -> None:
        self.clock = clock
        self.allowed = allowed
        self.events: list[InboundEvent] = []
        self.posts: list[tuple[str, str]] = []

    async def voice_allowed(self, user: Any, guild: Any) -> bool:
        return str(getattr(user, "id", "")) in self.allowed

    def role_granted(self, user: Any, guild: Any) -> bool:
        return False

    async def emit(self, event: InboundEvent) -> None:
        self.events.append(event)

    async def post_voice_text(self, channel_id: str, text: str) -> None:
        self.posts.append((channel_id, text))

    def scrub(self, text: str) -> str:
        return text


def member(user_id: int) -> Any:
    return SimpleNamespace(id=user_id, display_name=f"user{user_id}")


GUILD = SimpleNamespace(id=10, name="Ops", get_member=member)
CHANNEL = SimpleNamespace(id=VOICE, name="lounge")
OTHER = SimpleNamespace(id=501, name="studio")


def person(user_id: str, channel: Any = CHANNEL) -> Any:
    return SimpleNamespace(
        id=int(user_id), display_name=f"user{user_id}", voice=SimpleNamespace(channel=channel)
    )


@dataclass
class Rig:
    clock: FakeClock
    host: Host
    link: FakeLink
    speech: Speech
    voice: v.VoiceChannels


def rig(tmp_path: Path, *, idle: float = v.IDLE_LEAVE_S, **speech: Any) -> Rig:
    clock = FakeClock(START)
    host = Host(clock, {ANA, BEN})
    link = FakeLink()
    talk = Speech(tmp_path, **speech)
    channels = v.VoiceChannels(host, talk.deps(link), idle_seconds=idle, me=lambda: "900")
    return Rig(clock, host, link, talk, channels)


def drive(body: Callable[[], Awaitable[None]], made: Rig) -> None:
    async def main() -> None:
        try:
            await body()
        finally:
            await made.voice.close()

    asyncio.run(main())


# -- the row's checks ------------------------------------------------------------------------------


def test_two_speakers_become_two_turns_keyed_on_the_voice_channel(tmp_path: Path) -> None:
    made = rig(tmp_path, texts={ANA: "what is on today", BEN: "and tomorrow", EVE: "secret"})

    async def body() -> None:
        answer = await made.voice.command("join", user=person(ANA), guild=GUILD)
        assert answer.startswith("Joined #lounge.")
        conn = made.link.last
        conn.talk(ANA, 1.0)
        conn.talk(BEN, 1.2)
        conn.talk(EVE, 1.0)  # not allowed: never transcribed, never a turn
        await polls(made.clock, 1.0)
        await until(made.clock, lambda: len(made.host.events) == 2)
        await polls(made.clock, 0.4)
        events = made.host.events
        assert sorted(e.text for e in events) == ["and tomorrow", "what is on today"]
        assert {e.source.user_id for e in events} == {ANA, BEN}
        for event in events:
            assert event.source.chat_id == str(VOICE) and event.source.chat_type == "group"
            assert event.source.guild_id == "10" and event.source.chat_name == "Ops / lounge"
            assert event.metadata == {"mentioned": True, "voice_channel": True}
            assert event.allow_control is False and event.reply_expected is True
        assert sorted(made.speech.heard) == [ANA, BEN]
        echoes = [text for chat, text in made.host.posts if chat == str(VOICE)]
        assert sorted(echoes) == [
            "user200 (voice): what is on today",
            "user201 (voice): and tomorrow",
        ]

    drive(body, made)


def test_the_bot_leaves_after_300_seconds_without_speech(tmp_path: Path) -> None:
    made = rig(tmp_path, texts={ANA: "still here"})

    async def body() -> None:
        await made.voice.command("join", user=person(ANA), guild=GUILD)
        conn = made.link.last
        await polls(made.clock, 299.0)
        assert made.voice.joined("10") and not conn.closed
        assert conn.keepalives == 19  # every 15 s
        await polls(made.clock, 1.2)
        assert not made.voice.joined("10") and conn.closed
        assert made.host.posts[-1] == (
            str(VOICE),
            "Left #lounge after 5 minutes without speech.",
        )

    drive(body, made)


def test_speech_restarts_the_idle_clock(tmp_path: Path) -> None:
    made = rig(tmp_path, texts={ANA: "keep listening"})

    async def body() -> None:
        await made.voice.command("join", user=person(ANA), guild=GUILD)
        conn = made.link.last
        await polls(made.clock, 200.0)
        conn.talk(ANA, 1.0)
        await polls(made.clock, 1.0)
        await until(made.clock, lambda: len(made.host.events) == 1)
        await polls(made.clock, 250.0)
        assert made.voice.joined("10")  # 451 s after joining, 250 s after speech
        await polls(made.clock, 60.0)
        assert not made.voice.joined("10")

    drive(body, made)


def test_a_strangers_speech_does_not_keep_the_bot(tmp_path: Path) -> None:
    made = rig(tmp_path, idle=10.0)

    async def body() -> None:
        await made.voice.command("join", user=person(ANA), guild=GUILD)
        for _ in range(3):
            made.link.last.talk(EVE, 1.0)
            await polls(made.clock, 4.0)
        assert not made.voice.joined("10")
        assert made.speech.heard == [] and made.host.events == []

    drive(body, made)


def test_missing_ffmpeg_is_a_clear_answer_and_nothing_is_joined(tmp_path: Path) -> None:
    made = rig(tmp_path, ffmpeg=None)

    async def body() -> None:
        answer = await made.voice.command("join", user=person(ANA), guild=GUILD)
        assert answer == v.FFMPEG_MISSING and "Text chat keeps working" in answer
        assert made.link.connections == [] and not made.voice.joined("10")

    drive(body, made)


def test_missing_voice_libraries_name_the_extra(tmp_path: Path) -> None:
    why = "Voice needs the discord-voice extra (PyNaCl not installed)."
    made = rig(tmp_path, libraries=why)

    async def body() -> None:
        assert await made.voice.command("join", user=person(ANA), guild=GUILD) == why
        assert made.link.connections == []

    drive(body, made)


def test_the_library_check_names_the_extra_or_libopus(monkeypatch: pytest.MonkeyPatch) -> None:
    def spec(name: str) -> object | None:
        return None if name == "davey" else object()

    monkeypatch.setattr(v.importlib.util, "find_spec", spec)
    missing = v.voice_libraries_missing()
    assert missing is not None and "davey not installed" in missing
    assert "pip install 'indus-agent[discord-voice]'" in missing
    monkeypatch.setattr(v.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(v.ctypes.util, "find_library", lambda name: None)
    assert "libopus" in (v.voice_libraries_missing() or "")
    monkeypatch.setattr(v.ctypes.util, "find_library", lambda name: "libopus.so.0")
    assert v.voice_libraries_missing() is None


# -- commands --------------------------------------------------------------------------------------


def test_join_leave_and_status_answers(tmp_path: Path) -> None:
    made = rig(tmp_path)

    async def body() -> None:
        voice = made.voice
        assert (await voice.command("join", user=person(ANA), guild=None)).startswith(
            "Voice channels work in a server"
        )
        assert "not allowed" in await voice.command("join", user=person(EVE), guild=GUILD)
        away = SimpleNamespace(id=int(ANA), voice=None)
        assert (await voice.command("join", user=away, guild=GUILD)).startswith(
            "Join a voice channel first"
        )
        assert (await voice.command("status", user=away, guild=GUILD)).startswith(
            "I'm not in a voice channel"
        )
        await voice.command("join", user=person(ANA), guild=GUILD)
        again = await voice.command("join", user=person(BEN), guild=GUILD)
        assert again == "I'm already listening in #lounge." and len(made.link.connections) == 1
        await made.clock.advance_and_settle(120.0)
        status = await voice.command("status", user=away, guild=GUILD)
        assert status.startswith("Listening in #lounge for 2 minutes; 0 spoken turns so far.")
        assert "(180 s from now unless someone talks)" in status
        moved = await voice.command("join", user=person(ANA, OTHER), guild=GUILD)
        assert moved.startswith("Joined #studio.") and made.link.connections[0].closed
        assert "not allowed" in await voice.command("leave", user=person(EVE), guild=GUILD)
        assert (
            await voice.command("leave", user=person(ANA), guild=GUILD) == "Left the voice channel."
        )
        assert made.link.last.closed
        assert (await voice.command("leave", user=person(ANA), guild=GUILD)).startswith("I'm not")

    drive(body, made)


def test_a_failed_join_names_the_error(tmp_path: Path) -> None:
    made = rig(tmp_path)
    made.link.error = PermissionError("Missing Permissions")

    async def body() -> None:
        answer = await made.voice.command("join", user=person(ANA), guild=GUILD)
        assert answer == "I could not join #lounge: PermissionError: Missing Permissions"
        assert not made.voice.joined("10")

    drive(body, made)


def test_voice_command_reads_only_the_channel_subcommands() -> None:
    assert v.voice_command("/voice join") == "join"
    assert v.voice_command("/voice channel") == "join"
    assert v.voice_command("/voice LEAVE now") == "leave"
    assert v.voice_command("/voice status") == "status"
    assert v.voice_command("/voice on") is None
    assert v.voice_command("/voice") is None
    assert v.voice_command("/model join") is None
    assert v.voice_command("voice join") is None


# -- utterances ------------------------------------------------------------------------------------


def test_silence_gap_minimum_and_maximum() -> None:
    gate = v.UtteranceGate()
    one_second = frame() * 50
    gate.feed("a", one_second, 0.0)
    gate.feed("b", frame() * 20, 0.0)  # 0.4 s: noise
    assert gate.due(0.79) == []
    done = gate.due(0.8)
    assert done == [("a", one_second)] and gate.speakers == 0
    piece = None
    for n in range(3000):  # 60 s without a pause
        piece = gate.feed("c", frame(), n * 0.02) or piece
    assert piece is not None and len(piece) == v.BYTES_PER_SECOND * 60
    assert gate.speakers == 0


def test_a_repeated_transcript_is_dropped(tmp_path: Path) -> None:
    made = rig(tmp_path, texts={ANA: "Hello, there!"})

    async def body() -> None:
        await made.voice.command("join", user=person(ANA), guild=GUILD)
        conn = made.link.last
        conn.talk(ANA, 1.0)
        await polls(made.clock, 1.0)
        await until(made.clock, lambda: len(made.speech.heard) == 1)
        conn.talk(ANA, 1.0)
        await polls(made.clock, 1.0)
        await until(made.clock, lambda: len(made.speech.heard) == 2)
        await polls(made.clock, 0.4)
        assert len(made.host.events) == 1
        await polls(made.clock, 12.0)
        conn.talk(ANA, 1.0)
        await polls(made.clock, 1.0)
        await until(made.clock, lambda: len(made.host.events) == 2)

    drive(body, made)


def test_utterances_reach_stt_as_16khz_mono_wav(tmp_path: Path) -> None:
    made = rig(tmp_path, texts={ANA: "hi"})

    async def body() -> None:
        await made.voice.command("join", user=person(ANA), guild=GUILD)
        made.link.last.talk(ANA, 1.0)
        await polls(made.clock, 1.0)
        await until(made.clock, lambda: len(made.host.events) == 1)
        with wave.open(BytesIO(made.speech.wavs[0])) as handle:
            assert handle.getnchannels() == 1 and handle.getframerate() == 16_000
            assert handle.getnframes() == 16_000
            assert struct.unpack("<h", handle.readframes(1))[0] == 1000
        assert list(tmp_path.glob("utterance-*")) == []  # removed after transcription

    drive(body, made)


# -- speech out ------------------------------------------------------------------------------------


def test_replies_to_the_joined_channel_are_spoken_in_order(tmp_path: Path) -> None:
    made = rig(tmp_path)

    async def body() -> None:
        await made.voice.command("join", user=person(ANA), guild=GUILD)
        assert made.voice.speak_later(str(VOICE), "**First** see [docs](https://x.example/a)")
        assert made.voice.speak_later(str(VOICE), "Second\n```\ncode\n```")
        assert not made.voice.speak_later("300", "not a voice channel")
        await until(made.clock, lambda: made.link.last.plays == 2)
        assert made.speech.spoken == ["First see docs", "Second (code not read aloud)"]
        mixer = made.link.last.mixer
        assert mixer is not None
        frames = [mixer.read() for _ in range(10)]
        assert all(len(f) == v.FRAME_BYTES for f in frames) and mixer.read() == b""
        assert list(tmp_path.glob("tts-*")) == []

    drive(body, made)


def test_the_mixer_sums_overlays_and_clamps() -> None:
    mixer = v.VoiceMixer()
    assert mixer.read() == b"" and not mixer.active
    mixer.queue(frame(30000) + b"\x01")  # padded to two whole frames
    mixer.overlay(frame(10000))
    first = mixer.read()
    assert struct.unpack_from("<h", first)[0] == 32767  # clamped
    second = mixer.read()
    assert struct.unpack_from("<h", second)[0] == 1 and len(second) == v.FRAME_BYTES
    assert mixer.read() == b""
    mixer.queue(frame(-30000))
    mixer.overlay(frame(-10000))
    assert struct.unpack_from("<h", mixer.read())[0] == -32768
    mixer.queue(frame())
    mixer.clear()
    assert mixer.read() == b""


# -- RTP -------------------------------------------------------------------------------------------


def rtp(
    payload: bytes,
    *,
    ssrc: int = 77,
    extension: bytes = b"",
    pad: int = 0,
    payload_type: int = v.OPUS_PAYLOAD_TYPE,
    version: int = 2,
) -> bytes:
    first = version << 6 | (0x10 if extension else 0) | (0x20 if pad else 0)
    header = struct.pack(">BBHII", first, payload_type, 1, 960, ssrc)
    if extension:
        header += struct.pack(">HH", 0xBEDE, len(extension) // 4)
    sealed = extension + payload + (bytes([pad]) * pad if pad else b"") + b"T" * v.AEAD_TAG
    return header + sealed + b"\x00\x00\x00\x07"


def clear_opener(packet: v.RtpPacket) -> bytes:
    """Stands in for the AEAD: the body without its tag."""
    return packet.body[: -v.AEAD_TAG]


def test_rtp_packets_are_parsed_and_opened() -> None:
    packet = v.parse_rtp(rtp(b"OPUS", extension=b"EXT1EXT2", pad=3))
    assert packet is not None and packet.ssrc == 77 and packet.extension_words == 2
    assert len(packet.header) == 16 and packet.nonce == b"\x00\x00\x00\x07"
    assert v.rtp_payload(packet, clear_opener(packet)) == b"OPUS"
    assert v.parse_rtp(rtp(b"OPUS", payload_type=72)) is None  # RTCP
    assert v.parse_rtp(rtp(b"OPUS", version=1)) is None
    assert v.parse_rtp(b"\x80\x78\x00") is None


def test_the_receiver_decodes_known_speakers_only() -> None:
    delivered: list[tuple[str, bytes]] = []
    dave: dict[str, Any] = {"session": None, "version": 0}

    class Decoder:
        def decode(self, data: bytes) -> bytes:
            if data == b"BAD":
                raise ValueError("corrupted stream")
            return b"pcm:" + data

    class Session:
        def decrypt(self, user_id: int, media: object, data: bytes) -> bytes:
            assert media == "audio"
            return data.lower()

    receiver = v.VoiceReceiver(
        opener=lambda: clear_opener,
        dave=lambda: (dave["session"], dave["version"]),
        decoder=Decoder,
        deliver=lambda user, pcm: delivered.append((user, pcm)),
        audio_media=lambda: "audio",
        own_ssrc=lambda: 5,
    )
    receiver.on_packet(rtp(b"OPUS"))  # SSRC 77 not mapped yet
    receiver.speaking(77, ANA)
    receiver.on_packet(rtp(b"OPUS"))
    receiver.on_packet(rtp(b"BAD"))
    receiver.on_packet(rtp(b"OPUS", ssrc=5))  # our own
    dave.update(session=Session(), version=1)
    receiver.on_packet(rtp(b"SEALED"))
    receiver.on_packet(rtp(b"SEALED", ssrc=78))  # sealed and unknown
    receiver.forget(ANA)
    receiver.on_packet(rtp(b"OPUS"))
    assert delivered == [(ANA, b"pcm:OPUS"), (ANA, b"pcm:sealed")]
    assert receiver.dropped == 4


# -- the adapter -----------------------------------------------------------------------------------


def voice_world(tmp_path: Path, **speech: Any) -> tuple[World, FakeLink, Speech]:
    made = world(env={ALLOWED_ENV: ANA})
    link = FakeLink()
    talk = Speech(tmp_path, **speech)
    made.adapter = DiscordAdapter(
        clock=made.clock,
        settings=settings(command_sync="off"),
        env={ALLOWED_ENV: ANA},
        sdk=made.wire.sdk,
        voice_deps=talk.deps(link),
    )

    async def sink(event: InboundEvent) -> None:
        made.events.append(event)

    made.adapter.set_sink(sink)
    made.platform.add_chat("300", "group", guild_id="10")
    made.wire.voice_states[ANA] = VOICE
    return made, link, talk


def run_world(made: World, body: Callable[[World], Awaitable[None]]) -> None:
    async def main() -> None:
        try:
            await body(made)
        finally:
            await made.adapter.disconnect()

    asyncio.run(main())


def test_typed_voice_join_joins_hears_and_speaks_replies(tmp_path: Path) -> None:
    made, link, talk = voice_world(tmp_path, texts={ANA: "how is the build"})

    async def body(made: World) -> None:
        await made.settle(made.adapter.connect())
        made.platform.group("300", ANA, "/voice join")
        assert await made.pump() == []  # answered by the adapter, not a turn
        assert made.platform.texts("300")[-1].startswith("Joined #voice-500.")
        (conn,) = link.connections
        assert conn.channel.id == VOICE
        conn.talk(ANA, 1.0)
        await polls(made.clock, 1.0)
        await until(made.clock, lambda: len(made.events) == 1)
        (turn,) = made.events
        assert turn.source.chat_id == str(VOICE) and turn.text == "how is the build"
        assert made.platform.texts(str(VOICE)) == ["200 (voice): how is the build"]
        assert talk.spoken == []  # the echo is not read aloud
        sent = await made.adapter.send(str(VOICE), "Green since noon.")
        assert sent.ok and made.platform.texts(str(VOICE))[-1] == "Green since noon."
        await until(made.clock, lambda: conn.plays == 1)
        assert talk.spoken == ["Green since noon."]
        assert (await made.adapter.send("300", "typed answer")).ok
        await made.clock.run_until_idle()
        assert talk.spoken == ["Green since noon."]

    run_world(made, body)
    assert link.last.closed  # disconnect leaves the voice channel


def test_missing_ffmpeg_answers_and_text_keeps_working(tmp_path: Path) -> None:
    made, link, _ = voice_world(tmp_path, ffmpeg=None)

    async def body(made: World) -> None:
        await made.settle(made.adapter.connect())
        made.platform.group("300", ANA, "/voice join")
        await made.pump()
        assert made.platform.texts("300")[-1] == v.FFMPEG_MISSING
        assert link.connections == []
        made.platform.group("300", ANA, "<@900> still there?", mention=True)
        (event,) = await made.pump()
        assert event.text == "still there?"
        assert (await made.adapter.send("300", "yes")).ok

    run_world(made, body)


def test_the_voice_application_command_joins_and_reports_in_the_channel(tmp_path: Path) -> None:
    made, link, _ = voice_world(tmp_path)

    async def body(made: World) -> None:
        await made.settle(made.adapter.connect())
        interaction = await made.wire.invoke_command("300", ANA, "voice", "join")
        assert interaction.response.sent == [(VOICE_WORKING, True)]
        assert made.platform.texts("300")[-1].startswith("Joined #voice-500.")
        assert len(link.connections) == 1 and made.events == []
        status = await made.wire.invoke_command("300", BEN, "voice", "status")
        assert status.response.sent[0][0].startswith("Listening in #voice-500")
        refused = await made.wire.invoke_command("300", BEN, "voice", "leave")
        assert refused.response.sent == [(VOICE_WORKING, True)]
        assert "not allowed" in made.platform.texts("300")[-1]
        turned = await made.wire.invoke_command("300", ANA, "voice", "on")
        assert turned.response.sent == [("Running /voice…", True)]
        assert made.events[-1].text == "/voice on"

    run_world(made, body)


def test_voice_status_in_a_dm_goes_to_the_shared_command(tmp_path: Path) -> None:
    made, link, _ = voice_world(tmp_path)

    async def body(made: World) -> None:
        await made.settle(made.adapter.connect())
        made.platform.dm(ANA, "/voice status")
        (event,) = await made.pump()
        assert event.text == "/voice status" and link.connections == []
        made.platform.dm(ANA, "/voice join")
        assert await made.pump() == [event]
        assert made.platform.texts(ANA)[-1].startswith("Voice channels work in a server")

    run_world(made, body)


def test_a_global_allowlist_or_allow_all_also_admits_voice() -> None:
    async def check(env: dict[str, str], user: str) -> bool:
        made = world(env=env)
        return await made.adapter.voice_allowed(SimpleNamespace(id=int(user)), GUILD)

    names = policy_env("discord")
    assert asyncio.run(check({GLOBAL_ALLOWED_USERS_ENV: BEN}, BEN))
    assert asyncio.run(check({names.allow_all_users: "true"}, EVE))
    assert not asyncio.run(check({}, EVE))


def test_the_voice_command_hint_names_the_channel_subcommands() -> None:
    made = world(sync=True)

    async def body(made: World) -> None:
        await made.settle(made.adapter.connect())
        task = made.adapter._sync_task
        assert task is not None
        await made.settle(task, step=0.5)

    run_world(made, body)
    stored = [c for c in made.wire.client.http.commands.values() if c["name"] == "voice"]
    assert stored[0]["options"][0]["description"] == v.VOICE_ARGS_HINT
