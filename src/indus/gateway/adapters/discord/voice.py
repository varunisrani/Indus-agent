"""Discord voice channels (P16-35): join, hear each speaker, turn speech into turns, speak replies.

``VoiceChannels`` is the Discord adapter's voice part. ``/voice join`` (typed or as the application
command) in a server brings the bot into the caller's voice channel; ``/voice leave`` and
``/voice status`` do what they say. While joined:

- audio arrives per speaker as 48 kHz stereo 16-bit PCM (``VoiceConnection``'s ``on_audio``);
  ``UtteranceGate`` keeps one buffer per speaker and cuts an utterance once that speaker has been
  quiet for ``SILENCE_GAP_S`` (pieces shorter than ``MIN_SPEECH_S`` are noise and dropped; a speaker
  who never pauses is cut every ``MAX_UTTERANCE_S``);
- only people allowed to use the bot are heard: anyone else's utterance is dropped before
  transcription (the gateway's own authorization still checks every turn);
- an utterance becomes 16 kHz mono WAV, the profile's speech-to-text provider transcribes it, the
  text is echoed into the voice channel's chat and emitted as an ``InboundEvent`` keyed on the
  voice channel (chat type ``group``, the speaker as the user, addressed to us, never a command);
- a reply the gateway sends to the voice channel is posted in its chat (by the adapter) and also
  spoken: text to speech, ffmpeg to PCM, then ``VoiceMixer``, one continuous source that plays
  queued replies in order (and can lay a clip over them);
- with no allowed speech and nothing playing for ``platforms.discord.voice_idle_seconds`` (300 s;
  0 = never) the bot leaves and says so.

Voice needs the ``discord-voice`` extra (discord.py's voice dependencies: PyNaCl for the transport
encryption, davey for Discord's end-to-end voice encryption), libopus and ffmpeg. When one is
missing ``/voice join`` names the fix and nothing else changes: text keeps working.

``DiscordVoiceLink`` is the real connection over discord.py: a ``VoiceClient`` whose connection
state carries our voice-websocket hook (SPEAKING events map an SSRC to a user) and a socket listener
that opens each RTP packet (``aead_xchacha20_poly1305_rtpsize`` through PyNaCl's AEAD, then the
connection's DAVE session) and decodes Opus with discord.py's decoder. Tests use their own link.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes.util
import functools
import importlib
import importlib.util
import io
import itertools
import logging
import re
import struct
import sys
import threading
import wave
from array import array
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

from indus.gateway.core.events import InboundEvent, MessageKind, Source, parse_command

if TYPE_CHECKING:
    from indus.foundation.scope import Scope

__all__ = [
    "BYTES_PER_SECOND",
    "FRAME_BYTES",
    "IDLE_LEAVE_S",
    "MAX_UTTERANCE_S",
    "MIN_SPEECH_S",
    "SILENCE_GAP_S",
    "VOICE_ARGS_HINT",
    "VOICE_SUBCOMMANDS",
    "VOICE_VENDOR_NAMES",
    "DiscordVoiceLink",
    "RtpPacket",
    "UtteranceGate",
    "VoiceChannels",
    "VoiceConnection",
    "VoiceDeps",
    "VoiceHost",
    "VoiceLink",
    "VoiceMixer",
    "VoiceReceiver",
    "default_voice_deps",
    "parse_rtp",
    "pcm_to_wav",
    "rtp_payload",
    "speakable",
    "voice_command",
    "voice_libraries_missing",
]

log = logging.getLogger(__name__)

# Discord's voice audio: 48 kHz, two channels, signed 16-bit little endian, 20 ms frames.
SAMPLE_RATE = 48_000
CHANNELS = 2
SAMPLE_WIDTH = 2
FRAME_MS = 20
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH
FRAME_BYTES = BYTES_PER_SECOND * FRAME_MS // 1000
STT_RATE = 16_000

SILENCE_GAP_S = 0.8
MIN_SPEECH_S = 0.5
MAX_UTTERANCE_S = 60.0
IDLE_LEAVE_S = 300.0
POLL_S = 0.2
KEEPALIVE_S = 15.0
DUPLICATE_WINDOW_S = 12.0
DECODE_TIMEOUT_S = 60.0
CONNECT_TIMEOUT_S = 30.0
ECHO_MAX = 1800

# RTP as Discord sends it: version 2, Opus payload type 120; RTCP shares the socket.
RTP_VERSION = 2
OPUS_PAYLOAD_TYPE = 0x78
RTP_HEADER = 12
NONCE_TAIL = 4
AEAD_TAG = 16
RTPSIZE_MODE = "aead_xchacha20_poly1305_rtpsize"
# Voice-websocket opcodes we read: who speaks on which SSRC, and who left.
SPEAKING_OP = 5
CLIENT_DISCONNECT_OP = 13
# Three bytes of Opus silence keep the UDP route open while nobody talks.
KEEPALIVE_PACKET = b"\xf8\xff\xfe"

VOICE_SUBCOMMANDS = frozenset({"join", "channel", "leave", "status"})
VOICE_ARGS_HINT = "join | leave | status | on | off"
EXTRA_ENV = "discord-voice"

# discord.py names the voice part relies on (merged into the adapter's ``vendor_names``).
VOICE_VENDOR_NAMES: Mapping[str, tuple[str, ...]] = {
    "discord:VoiceClient": (
        "create_connection_state",
        "play",
        "is_playing",
        "stop",
        "disconnect",
        "is_connected",
    ),
    "discord:AudioSource": ("read", "is_opus", "cleanup"),
    "discord.abc:Connectable": ("connect",),
    "discord.opus:Decoder": ("decode",),
    "discord.voice_state:VoiceConnectionState": (
        "add_socket_listener",
        "remove_socket_listener",
        "send_packet",
    ),
}

_LITTLE = sys.byteorder == "little"


def _seconds(pcm_bytes: int) -> float:
    return pcm_bytes / BYTES_PER_SECOND


def _span(seconds: float) -> str:
    if seconds >= 60 and seconds % 60 == 0:
        minutes = int(seconds // 60)
        return f"{minutes} minute" + ("" if minutes == 1 else "s")
    return f"{seconds:g} s"


# -- utterances ------------------------------------------------------------------------------------


@dataclass
class _Speech:
    pcm: bytearray
    last: float


class UtteranceGate:
    """One PCM buffer per speaker, cut into utterances by silence on the caller's clock."""

    def __init__(
        self,
        *,
        gap: float = SILENCE_GAP_S,
        minimum: float = MIN_SPEECH_S,
        maximum: float = MAX_UTTERANCE_S,
    ) -> None:
        self.gap = gap
        self.minimum = minimum
        self.maximum = maximum
        self._buffers: dict[str, _Speech] = {}

    def feed(self, speaker: str, pcm: bytes, now: float) -> bytes | None:
        """Add ``pcm`` to ``speaker``'s buffer; a full ``maximum``-long piece comes back cut."""
        found = self._buffers.get(speaker)
        if found is None:
            found = self._buffers[speaker] = _Speech(bytearray(), now)
        found.pcm.extend(pcm)
        found.last = now
        if _seconds(len(found.pcm)) >= self.maximum:
            piece = bytes(found.pcm)
            del self._buffers[speaker]
            return piece
        return None

    def due(self, now: float) -> list[tuple[str, bytes]]:
        """Utterances whose speaker has been quiet for ``gap``; noise shorter than ``minimum``
        is dropped."""
        done: list[tuple[str, bytes]] = []
        for speaker, speech in list(self._buffers.items()):
            if now - speech.last < self.gap:
                continue
            del self._buffers[speaker]
            if _seconds(len(speech.pcm)) >= self.minimum:
                done.append((speaker, bytes(speech.pcm)))
        return done

    def clear(self) -> None:
        self._buffers.clear()

    @property
    def speakers(self) -> int:
        return len(self._buffers)


def pcm_to_wav(pcm: bytes) -> bytes:
    """48 kHz stereo s16le → 16 kHz mono WAV (both channels and three frames averaged)."""
    usable = len(pcm) - len(pcm) % (SAMPLE_WIDTH * CHANNELS * 3)
    samples = array("h")
    samples.frombytes(pcm[:usable])
    if not _LITTLE:
        samples.byteswap()
    out = array("h", (sum(samples[i : i + 6]) // 6 for i in range(0, len(samples), 6)))
    if not _LITTLE:
        out.byteswap()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(SAMPLE_WIDTH)
        handle.setframerate(STT_RATE)
        handle.writeframes(out.tobytes())
    return buffer.getvalue()


# -- speech out ------------------------------------------------------------------------------------


def _frames(pcm: bytes) -> bytes:
    return pcm + b"\x00" * (-len(pcm) % FRAME_BYTES)


class VoiceMixer:
    """The one audio source of a voice connection: queued replies in order, overlays on top.

    ``read`` (called by discord.py's player thread every 20 ms) returns one frame of 48 kHz
    stereo s16le, or ``b""`` when nothing is left (the player then stops; the next reply starts it
    again). Overlays are summed onto the frame and clamped to 16 bits. Thread-safe.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queue: deque[bytes] = deque()
        self._current = b""
        self._pos = 0
        self._overlays: list[list[Any]] = []

    def queue(self, pcm: bytes) -> None:
        """Play ``pcm`` after everything queued before it."""
        if pcm:
            with self._lock:
                self._queue.append(_frames(pcm))

    def overlay(self, pcm: bytes) -> None:
        """Play ``pcm`` now, mixed over whatever else is playing."""
        if pcm:
            with self._lock:
                self._overlays.append([_frames(pcm), 0])

    @property
    def active(self) -> bool:
        with self._lock:
            return bool(self._queue or self._overlays or self._pos < len(self._current))

    def clear(self) -> None:
        with self._lock:
            self._queue.clear()
            self._overlays.clear()
            self._current, self._pos = b"", 0

    def read(self) -> bytes:
        with self._lock:
            parts: list[bytes] = []
            if self._pos >= len(self._current) and self._queue:
                self._current, self._pos = self._queue.popleft(), 0
            if self._pos < len(self._current):
                parts.append(self._current[self._pos : self._pos + FRAME_BYTES])
                self._pos += FRAME_BYTES
            live: list[list[Any]] = []
            for overlay in self._overlays:
                clip, pos = overlay
                parts.append(clip[pos : pos + FRAME_BYTES])
                overlay[1] = pos + FRAME_BYTES
                if overlay[1] < len(clip):
                    live.append(overlay)
            self._overlays = live
        if not parts:
            return b""
        if len(parts) == 1:
            return parts[0]
        return _mix(parts)


def _mix(frames: list[bytes]) -> bytes:
    total = [0] * (FRAME_BYTES // SAMPLE_WIDTH)
    for frame in frames:
        samples = array("h")
        samples.frombytes(frame)
        if not _LITTLE:
            samples.byteswap()
        for i, value in enumerate(samples):
            total[i] += value
    out = array("h", (max(-32768, min(32767, v)) for v in total))
    if not _LITTLE:
        out.byteswap()
    return out.tobytes()


_FENCE = re.compile(r"```.*?(```|$)", re.DOTALL)
_LINK = re.compile(r"\[([^\]]+)\]\((?:[^)]+)\)")
_URL = re.compile(r"https?://\S+")
_MARKS = re.compile(r"[*_~`#>|]+")


def speakable(text: str) -> str:
    """``text`` as something to say aloud: no code blocks, link targets or Markdown marks."""
    said = _FENCE.sub(" (code not read aloud) ", text)
    said = _LINK.sub(r"\1", said)
    said = _URL.sub("a link", said)
    said = _MARKS.sub("", said)
    return " ".join(said.split())


# -- RTP -------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RtpPacket:
    """One voice packet: ``header`` is the authenticated clear part (fixed header, CSRCs and the
    extension header), ``body`` the sealed rest without the nonce tail."""

    ssrc: int
    sequence: int
    timestamp: int
    header: bytes
    body: bytes
    nonce: bytes
    extension_words: int = 0
    padded: bool = False


def parse_rtp(data: bytes) -> RtpPacket | None:
    """An Opus RTP packet in the ``rtpsize`` layout, or None (RTCP, other payloads, malformed)."""
    if len(data) < RTP_HEADER + NONCE_TAIL:
        return None
    first, second = data[0], data[1]
    if first >> 6 != RTP_VERSION or second & 0x7F != OPUS_PAYLOAD_TYPE:
        return None
    offset = RTP_HEADER + 4 * (first & 0x0F)
    words = 0
    if first & 0x10:
        if len(data) < offset + 4:
            return None
        words = struct.unpack_from(">H", data, offset + 2)[0]
        offset += 4
    if len(data) < offset + AEAD_TAG + NONCE_TAIL:
        return None
    sequence, timestamp, ssrc = struct.unpack_from(">HII", data, 2)
    return RtpPacket(
        ssrc=ssrc,
        sequence=sequence,
        timestamp=timestamp,
        header=bytes(data[:offset]),
        body=bytes(data[offset:-NONCE_TAIL]),
        nonce=bytes(data[-NONCE_TAIL:]),
        extension_words=words,
        padded=bool(first & 0x20),
    )


def rtp_payload(packet: RtpPacket, opened: bytes) -> bytes | None:
    """The Opus frame inside an opened packet: the extension body and any padding removed."""
    payload = opened[4 * packet.extension_words :]
    if packet.padded and payload:
        pad = payload[-1]
        if pad == 0 or pad > len(payload):
            return None
        payload = payload[:-pad]
    return payload or None


type Opener = Callable[[RtpPacket], bytes]


class VoiceReceiver:
    """Raw voice packets → per-speaker PCM. ``on_packet`` runs on discord.py's socket thread.

    ``opener()`` gives the current transport opener (None while no key is known), ``dave()`` the
    connection's end-to-end session and protocol version, ``decoder()`` a new Opus decoder;
    ``deliver(user_id, pcm)`` is called for every decoded frame of a known speaker.
    """

    def __init__(
        self,
        *,
        opener: Callable[[], Opener | None],
        dave: Callable[[], tuple[Any, int]],
        decoder: Callable[[], Any],
        deliver: Callable[[str, bytes], None],
        audio_media: Callable[[], Any] = lambda: None,
        own_ssrc: Callable[[], int | None] = lambda: None,
    ) -> None:
        self._opener = opener
        self._dave = dave
        self._decoder = decoder
        self._deliver = deliver
        self._media = audio_media
        self._own = own_ssrc
        self._lock = threading.Lock()
        self._users: dict[int, str] = {}
        self._decoders: dict[int, Any] = {}
        self.dropped = 0
        self.decoded = 0

    def speaking(self, ssrc: int, user_id: str) -> None:
        with self._lock:
            self._users[int(ssrc)] = str(user_id)

    def forget(self, user_id: str) -> None:
        with self._lock:
            for ssrc in [s for s, u in self._users.items() if u == str(user_id)]:
                self._users.pop(ssrc, None)
                self._decoders.pop(ssrc, None)

    def user_of(self, ssrc: int) -> str | None:
        with self._lock:
            return self._users.get(ssrc)

    def _drop(self) -> None:
        self.dropped += 1

    def on_packet(self, data: bytes) -> None:
        packet = parse_rtp(data)
        if packet is None or packet.ssrc == self._own():
            return
        opener = self._opener()
        if opener is None:
            return self._drop()
        try:
            payload = rtp_payload(packet, opener(packet))
        except Exception:
            return self._drop()
        if payload is None:
            return self._drop()
        user = self.user_of(packet.ssrc)
        session, version = self._dave()
        if version > 0 and session is not None:
            if user is None:
                return self._drop()  # still end-to-end sealed, and we cannot say whose it is
            try:
                payload = session.decrypt(int(user), self._media(), payload)
            except Exception:
                return self._drop()
        if user is None:
            return self._drop()
        with self._lock:
            decoder = self._decoders.get(packet.ssrc)
            if decoder is None:
                decoder = self._decoders[packet.ssrc] = self._decoder()
        try:
            pcm = decoder.decode(payload)
        except Exception:
            with self._lock:
                self._decoders.pop(packet.ssrc, None)
            return self._drop()
        self.decoded += 1
        self._deliver(user, pcm)


# -- connections -----------------------------------------------------------------------------------


class VoiceConnection(Protocol):
    """One joined voice channel."""

    def play(self, mixer: VoiceMixer) -> None:
        """Make sure ``mixer`` is being played (start the player when it is idle)."""
        ...

    def keepalive(self) -> None: ...

    async def close(self) -> None: ...


class VoiceLink(Protocol):
    async def connect(
        self, channel: Any, on_audio: Callable[[str, bytes], None]
    ) -> VoiceConnection: ...


def _voice_state_class(sdk: Any) -> Any:
    found = getattr(getattr(sdk, "voice_state", None), "VoiceConnectionState", None)
    if found is not None:
        return found
    return importlib.import_module(f"{sdk.__name__}.voice_state").VoiceConnectionState


class _DiscordConnection:
    def __init__(self, sdk: Any, client: Any, receiver: VoiceReceiver, loop: Any) -> None:
        self._sdk = sdk
        self._client = client
        self._receiver = receiver
        self._loop = loop
        self._source_cls: Any = None

    def _source(self, mixer: VoiceMixer) -> Any:
        if self._source_cls is None:

            class MixerSource(self._sdk.AudioSource):  # pyright: ignore[reportUntypedBaseClass]
                def __init__(self, feed: VoiceMixer) -> None:
                    self.feed = feed

                def read(self) -> bytes:
                    return self.feed.read()

                def is_opus(self) -> bool:
                    return False

                def cleanup(self) -> None:
                    return None

            self._source_cls = MixerSource
        return self._source_cls(mixer)

    def play(self, mixer: VoiceMixer) -> None:
        client = self._client
        if not client.is_connected() or client.is_playing() or not mixer.active:
            return

        def ended(error: BaseException | None) -> None:
            if error is not None:
                log.warning("discord voice: playback stopped: %s", type(error).__name__)
            self._loop.call_soon_threadsafe(self.play, mixer)

        client.play(self._source(mixer), after=ended)

    def keepalive(self) -> None:
        with contextlib.suppress(Exception):
            self._client._connection.send_packet(KEEPALIVE_PACKET)  # noqa: SLF001

    async def close(self) -> None:
        state = getattr(self._client, "_connection", None)
        with contextlib.suppress(Exception):
            state.remove_socket_listener(self._receiver.on_packet)  # pyright: ignore[reportOptionalMemberAccess]
        with contextlib.suppress(Exception):
            if self._client.is_playing():
                self._client.stop()
        with contextlib.suppress(Exception):
            await self._client.disconnect(force=True)


class DiscordVoiceLink:
    """``VoiceLink`` over discord.py (the ``discord-voice`` extra)."""

    def __init__(self, sdk: Any) -> None:
        self._sdk = sdk

    def _opener(self, state: Any) -> Opener | None:
        key = getattr(state, "secret_key", None)
        if not key or getattr(state, "mode", None) != RTPSIZE_MODE:
            return None
        aead = importlib.import_module("nacl.secret").Aead(bytes(key))

        def open_packet(packet: RtpPacket) -> bytes:
            nonce = packet.nonce + bytes(24 - NONCE_TAIL)
            return bytes(aead.decrypt(packet.body, packet.header, nonce))

        return open_packet

    async def connect(
        self, channel: Any, on_audio: Callable[[str, bytes], None]
    ) -> VoiceConnection:
        sdk = self._sdk
        loop = asyncio.get_running_loop()
        holder: dict[str, Any] = {}
        openers: dict[bytes, Opener | None] = {}

        def opener() -> Opener | None:
            state = holder.get("state")
            key = bytes(getattr(state, "secret_key", None) or b"")
            if key not in openers:
                openers.clear()
                openers[key] = self._opener(state)
            return openers[key]

        def dave() -> tuple[Any, int]:
            state = holder.get("state")
            return getattr(state, "dave_session", None), int(
                getattr(state, "dave_protocol_version", 0) or 0
            )

        def audio_media() -> Any:
            return importlib.import_module("davey").MediaType.audio

        def deliver(user: str, pcm: bytes) -> None:
            loop.call_soon_threadsafe(on_audio, user, pcm)

        receiver = VoiceReceiver(
            opener=opener,
            dave=dave,
            decoder=lambda: sdk.opus.Decoder(),
            deliver=deliver,
            audio_media=audio_media,
            own_ssrc=lambda: getattr(holder.get("state"), "ssrc", None),
        )

        async def hook(_ws: Any, message: Any) -> None:
            if not isinstance(message, Mapping):
                return
            op = message.get("op")
            data: Mapping[str, Any] = message.get("d") or {}
            try:
                if op == SPEAKING_OP and "ssrc" in data and "user_id" in data:
                    receiver.speaking(int(data["ssrc"]), str(data["user_id"]))
                elif op == CLIENT_DISCONNECT_OP and "user_id" in data:
                    receiver.forget(str(data["user_id"]))
            except (TypeError, ValueError):
                return

        state_cls = _voice_state_class(sdk)

        class ListeningVoiceClient(sdk.VoiceClient):  # pyright: ignore[reportUntypedBaseClass]
            def create_connection_state(self) -> Any:
                state = state_cls(self, hook=hook)
                holder["state"] = state
                return state

        client = await channel.connect(
            cls=ListeningVoiceClient, self_deaf=False, timeout=CONNECT_TIMEOUT_S
        )
        holder["state"] = client._connection  # noqa: SLF001
        client._connection.add_socket_listener(receiver.on_packet)  # noqa: SLF001
        return _DiscordConnection(sdk, client, receiver, loop)


# -- prerequisites and defaults --------------------------------------------------------------------


def voice_libraries_missing() -> str | None:
    """Why discord.py cannot do voice here (the extra or libopus missing), or None."""
    missing = [
        label
        for label, module in (("PyNaCl", "nacl"), ("davey", "davey"))
        if importlib.util.find_spec(module) is None
    ]
    if missing:
        return (
            f"Voice needs the {EXTRA_ENV} extra ({' and '.join(missing)} not installed): "
            f"pip install 'indus-agent[{EXTRA_ENV}]'. Text chat keeps working."
        )
    if ctypes.util.find_library("opus") is None:
        return (
            "Voice needs the Opus audio library (libopus), which is not installed on the "
            "gateway's machine (for example `apt install libopus0` or `brew install opus`). "
            "Text chat keeps working."
        )
    return None


FFMPEG_MISSING = (
    "Voice needs ffmpeg on the gateway's machine to play spoken replies, and it is not installed "
    "(for example `apt install ffmpeg` or `brew install ffmpeg`). Text chat keeps working."
)


@dataclass(frozen=True)
class VoiceDeps:
    """What voice needs from the profile; tests pass fakes."""

    transcribe: Callable[[Path], Awaitable[str]]
    synthesize: Callable[[str], Awaitable[Path]]
    decode: Callable[[Path], Awaitable[bytes]]
    ffmpeg: Callable[[], str | None]
    workdir: Callable[[], Path]
    libraries: Callable[[], str | None] = voice_libraries_missing
    link: VoiceLink | None = None


def default_voice_deps(scope: Scope) -> VoiceDeps:
    """Speech to text, text to speech and ffmpeg from ``scope``'s settings (imported on use)."""

    async def transcribe(path: Path) -> str:
        from indus.tools.builtin.media.stt import transcribe as stt

        found = await stt(path, scope=scope)
        return "" if found.inaudible else found.text.strip()

    async def synthesize(text: str) -> Path:
        from indus.tools.builtin.media.tts import SpeechTool

        speech = await SpeechTool().speak(text, scope, platform="discord")
        return speech.path

    def ffmpeg() -> str | None:
        from indus.tools.builtin.media import audio

        return audio.find_ffmpeg(scope)

    async def decode(path: Path) -> bytes:
        from indus.foundation.blocking import run_blocking
        from indus.tools.builtin.media import audio

        program = audio.find_ffmpeg(scope)
        if program is None:
            raise RuntimeError("ffmpeg is not installed")
        argv = [
            program,
            "-nostdin",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-f",
            "s16le",
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            str(CHANNELS),
            "pipe:1",
        ]
        run = functools.partial(audio.run_tool, argv, scope, timeout=DECODE_TIMEOUT_S)
        done = await run_blocking(run, label="discord voice decode")
        if done.returncode != 0 or not done.stdout:
            raise RuntimeError(f"ffmpeg could not decode the speech (exit {done.returncode})")
        return bytes(done.stdout)

    def workdir() -> Path:
        from indus.tools.builtin.media.common import folder

        return folder(scope, Path("cache") / "discord-voice")

    return VoiceDeps(
        transcribe=transcribe, synthesize=synthesize, decode=decode, ffmpeg=ffmpeg, workdir=workdir
    )


def voice_command(text: str) -> str | None:
    """The voice-channel subcommand in ``text`` (``join``, ``leave``, ``status``), else None."""
    parsed = parse_command(text)
    if parsed is None or parsed.name != "voice":
        return None
    sub = (parsed.args.split() or [""])[0].lower()
    if sub not in VOICE_SUBCOMMANDS:
        return None
    return "join" if sub == "channel" else sub


# -- the manager -----------------------------------------------------------------------------------


class VoiceHost(Protocol):
    """What ``VoiceChannels`` needs from the Discord adapter."""

    platform: ClassVar[str]

    @property
    def clock(self) -> Any: ...

    @property
    def sdk(self) -> Any: ...

    async def voice_allowed(self, user: Any, guild: Any) -> bool: ...

    def role_granted(self, user: Any, guild: Any) -> bool: ...

    async def emit(self, event: InboundEvent) -> None: ...

    async def post_voice_text(self, channel_id: str, text: str) -> None: ...

    def scrub(self, text: str) -> str: ...


def _label(channel: Any) -> str:
    name = str(getattr(channel, "name", "") or getattr(channel, "id", "") or "the voice channel")
    return f"#{name}" if not name.startswith("#") else name


def _display(member: Any, user_id: str) -> str:
    for attr in ("display_name", "global_name", "name"):
        value = getattr(member, attr, None)
        if isinstance(value, str) and value:
            return value
    return user_id


def _normalised(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", "", text.lower()).split())


@dataclass
class _Session:
    guild: Any
    guild_id: str
    channel: Any
    channel_id: str
    connection: VoiceConnection
    joined_at: float
    last_activity: float
    started: datetime
    mixer: VoiceMixer = field(default_factory=VoiceMixer)
    gate: UtteranceGate = field(default_factory=UtteranceGate)
    pending: asyncio.Queue[tuple[str, bytes]] = field(
        default_factory=asyncio.Queue[tuple[str, bytes]]
    )
    allowed: dict[str, bool] = field(default_factory=dict[str, bool])
    recent: dict[str, deque[tuple[float, str]]] = field(
        default_factory=dict[str, deque[tuple[float, str]]]
    )
    tasks: set[asyncio.Task[Any]] = field(default_factory=set[asyncio.Task[Any]])
    speech_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    turns: int = 0


class VoiceChannels:
    """The voice channels one Discord adapter has joined (at most one per server)."""

    def __init__(
        self,
        host: VoiceHost,
        deps: VoiceDeps | None,
        *,
        idle_seconds: float = IDLE_LEAVE_S,
        me: Callable[[], str] = lambda: "",
    ) -> None:
        self._host = host
        self._deps = deps
        self.idle_seconds = idle_seconds
        self._me = me
        self._sessions: dict[str, _Session] = {}
        self._joining: dict[str, asyncio.Lock] = {}
        self._numbers = itertools.count(1)

    # -- reading ---------------------------------------------------------------------------------

    def joined(self, guild_id: str) -> bool:
        return str(guild_id) in self._sessions

    def channel_ids(self) -> set[str]:
        return {s.channel_id for s in self._sessions.values()}

    def _by_channel(self, channel_id: str) -> _Session | None:
        return next((s for s in self._sessions.values() if s.channel_id == str(channel_id)), None)

    def status(self, guild_id: str) -> str:
        session = self._sessions.get(str(guild_id))
        if session is None:
            return "I'm not in a voice channel in this server. Join one and run /voice join."
        now = self._host.clock.monotonic()
        minutes = max(0, int((now - session.joined_at) // 60))
        listened = f"{minutes} minute" + ("" if minutes == 1 else "s")
        turns = f"{session.turns} spoken turn" + ("" if session.turns == 1 else "s")
        text = f"Listening in {_label(session.channel)} for {listened}; {turns} so far."
        if self.idle_seconds > 0:
            left = max(0.0, self.idle_seconds - (now - session.last_activity))
            text += (
                f" I leave after {_span(self.idle_seconds)} without speech "
                f"({int(left)} s from now unless someone talks)."
            )
        return text

    # -- commands --------------------------------------------------------------------------------

    async def command(self, sub: str, *, user: Any, guild: Any) -> str:
        """Run ``/voice <sub>`` for ``user`` in ``guild`` (None in a DM); the answer to show."""
        if guild is None:
            return "Voice channels work in a server, not in direct messages."
        guild_id = str(getattr(guild, "id", ""))
        if sub == "status":
            return self.status(guild_id)
        if not await self._host.voice_allowed(user, guild):
            return "You are not allowed to use voice with this bot."
        if sub == "leave":
            if await self.leave(guild_id):
                return "Left the voice channel."
            return "I'm not in a voice channel in this server."
        return await self.join(user, guild)

    def _prerequisites(self, deps: VoiceDeps) -> str | None:
        if deps.ffmpeg() is None:
            return FFMPEG_MISSING
        return deps.libraries()

    async def join(self, user: Any, guild: Any) -> str:
        deps = self._deps
        if deps is None:
            return "Voice is not available for this gateway. Text chat keeps working."
        problem = self._prerequisites(deps)
        if problem is not None:
            return problem
        state = getattr(user, "voice", None)
        channel = getattr(state, "channel", None)
        if channel is None:
            return "Join a voice channel first, then run /voice join."
        guild_id = str(getattr(guild, "id", ""))
        lock = self._joining.setdefault(guild_id, asyncio.Lock())
        async with lock:
            current = self._sessions.get(guild_id)
            if current is not None and current.channel_id == str(getattr(channel, "id", "")):
                self._touch(current)
                return f"I'm already listening in {_label(channel)}."
            if current is not None:
                await self.leave(guild_id)
            link = deps.link if deps.link is not None else DiscordVoiceLink(self._host.sdk)
            try:
                connection = await link.connect(
                    channel, lambda speaker, pcm: self._on_audio(guild_id, speaker, pcm)
                )
            except Exception as exc:
                why = self._host.scrub(f"{type(exc).__name__}: {exc}")
                log.warning("discord voice: could not join %s: %s", guild_id, why)
                return f"I could not join {_label(channel)}: {why}"
            clock = self._host.clock
            now = clock.monotonic()
            session = _Session(
                guild=guild,
                guild_id=guild_id,
                channel=channel,
                channel_id=str(getattr(channel, "id", "")),
                connection=connection,
                joined_at=now,
                last_activity=now,
                started=clock.now(),
            )
            self._sessions[guild_id] = session
            self._spawn(session, self._listen(session))
            self._spawn(session, self._work(session))
        idle = (
            f" I leave after {_span(self.idle_seconds)} without speech."
            if self.idle_seconds > 0
            else ""
        )
        return (
            f"Joined {_label(channel)}. I'm listening: talk to me there and I answer in its chat "
            f"and out loud.{idle}"
        )

    async def leave(self, guild_id: str, *, notice: str | None = None) -> bool:
        session = self._sessions.pop(str(guild_id), None)
        if session is None:
            return False
        me = asyncio.current_task()
        for task in list(session.tasks):
            if task is not me:
                task.cancel()
        for task in list(session.tasks):
            if task is not me:
                with contextlib.suppress(BaseException):
                    await task
        session.gate.clear()
        session.mixer.clear()
        try:
            await session.connection.close()
        except Exception as exc:
            log.warning("discord voice: leaving failed: %s", self._host.scrub(type(exc).__name__))
        if notice:
            await self._post(session.channel_id, notice)
        return True

    async def close(self) -> None:
        for guild_id in list(self._sessions):
            await self.leave(guild_id)

    # -- audio in --------------------------------------------------------------------------------

    def _spawn(self, session: _Session, work: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(work)
        session.tasks.add(task)
        task.add_done_callback(session.tasks.discard)

    def _touch(self, session: _Session) -> None:
        session.last_activity = self._host.clock.monotonic()

    def _on_audio(self, guild_id: str, speaker: str, pcm: bytes) -> None:
        session = self._sessions.get(guild_id)
        if session is None or not pcm or (self._me() and speaker == self._me()):
            return
        now = self._host.clock.monotonic()
        if session.allowed.get(speaker):
            session.last_activity = now
        piece = session.gate.feed(speaker, pcm, now)
        if piece is not None:
            session.pending.put_nowait((speaker, piece))

    async def _listen(self, session: _Session) -> None:
        clock = self._host.clock
        last_keepalive = clock.monotonic()
        while True:
            await clock.sleep(POLL_S)
            now = clock.monotonic()
            if now - last_keepalive >= KEEPALIVE_S:
                last_keepalive = now
                session.connection.keepalive()
            for item in session.gate.due(now):
                session.pending.put_nowait(item)
            if (
                self.idle_seconds > 0
                and not session.mixer.active
                and session.pending.empty()
                and now - session.last_activity >= self.idle_seconds
            ):
                log.info("discord voice: no speech for %gs; leaving", self.idle_seconds)
                await self.leave(
                    session.guild_id,
                    notice=(
                        f"Left {_label(session.channel)} after {_span(self.idle_seconds)} "
                        "without speech."
                    ),
                )
                return

    async def _work(self, session: _Session) -> None:
        while True:
            speaker, pcm = await session.pending.get()
            try:
                await self._utterance(session, speaker, pcm)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "discord voice: an utterance was lost: %s",
                    self._host.scrub(f"{type(exc).__name__}: {exc}"),
                )

    def _member(self, session: _Session, speaker: str) -> Any:
        getter = getattr(session.guild, "get_member", None)
        member = None
        if callable(getter):
            with contextlib.suppress(Exception):
                member = getter(int(speaker))
        return member

    async def _may_speak(self, session: _Session, speaker: str, member: Any) -> bool:
        known = session.allowed.get(speaker)
        if known is None:
            user = member if member is not None else _Anonymous(speaker)
            try:
                known = await self._host.voice_allowed(user, session.guild)
            except Exception:
                known = False
            session.allowed[speaker] = known
        return known

    def _duplicate(self, session: _Session, speaker: str, text: str, now: float) -> bool:
        said = _normalised(text)
        if not said:
            return True
        recent = session.recent.setdefault(speaker, deque(maxlen=5))
        while recent and now - recent[0][0] > DUPLICATE_WINDOW_S:
            recent.popleft()
        if any(prior == said for _, prior in recent):
            return True
        recent.append((now, said))
        return False

    async def _utterance(self, session: _Session, speaker: str, pcm: bytes) -> None:
        member = self._member(session, speaker)
        if not await self._may_speak(session, speaker, member):
            log.debug("discord voice: ignored speech from %s (not allowed)", speaker)
            return
        self._touch(session)
        deps = self._deps
        assert deps is not None  # a session exists only after a join with deps
        from indus.foundation.blocking import run_blocking

        folder = await run_blocking(deps.workdir, label="discord voice folder")
        path = folder / f"utterance-{session.guild_id}-{speaker}-{next(self._numbers)}.wav"
        wav = await run_blocking(pcm_to_wav, pcm, label="discord voice wav")
        await run_blocking(path.write_bytes, wav, label="discord voice wav")
        try:
            text = (await deps.transcribe(path)).strip()
        finally:
            with contextlib.suppress(OSError):
                path.unlink()
        if not text:
            log.debug("discord voice: nothing could be made out from %s", speaker)
            return
        if self._sessions.get(session.guild_id) is not session:
            return  # left (or moved) while transcribing: never into another conversation
        now = self._host.clock.monotonic()
        if self._duplicate(session, speaker, text, now):
            log.info("discord voice: dropped a repeated transcript from %s", speaker)
            return
        name = _display(member, speaker)
        session.turns += 1
        await self._post(session.channel_id, f"{name} (voice): {text[:ECHO_MAX]}")
        guild_name = str(getattr(session.guild, "name", "") or "")
        channel_name = str(getattr(session.channel, "name", "") or session.channel_id)
        source = Source(
            platform=self._host.platform,
            chat_id=session.channel_id,
            chat_type="group",
            chat_name=f"{guild_name} / {channel_name}" if guild_name else channel_name,
            user_id=speaker,
            user_name=name,
            guild_id=session.guild_id,
            role_authorized=self._host.role_granted(member, session.guild),
        )
        await self._host.emit(
            InboundEvent(
                source=source,
                text=text,
                kind=MessageKind.TEXT,
                allow_control=False,
                reply_expected=True,
                metadata={"mentioned": True, "voice_channel": True},
                timestamp=self._host.clock.now(),
            )
        )

    async def _post(self, channel_id: str, text: str) -> None:
        try:
            await self._host.post_voice_text(channel_id, text)
        except Exception as exc:
            log.debug("discord voice: could not post to the channel: %s", type(exc).__name__)

    # -- speech out ------------------------------------------------------------------------------

    def speak_later(self, channel_id: str, text: str) -> bool:
        """Say ``text`` in the joined voice channel ``channel_id``; False when not joined."""
        session = self._by_channel(channel_id)
        if session is None or self._deps is None:
            return False
        said = speakable(text)
        if not said:
            return True
        self._touch(session)
        self._spawn(session, self._speak(session, said))
        return True

    async def _speak(self, session: _Session, text: str) -> None:
        deps = self._deps
        assert deps is not None
        async with session.speech_lock:  # replies are spoken in the order they were sent
            try:
                path = await deps.synthesize(text)
                try:
                    pcm = await deps.decode(path)
                finally:
                    with contextlib.suppress(OSError):
                        path.unlink()
            except Exception as exc:
                log.warning(
                    "discord voice: could not speak a reply: %s",
                    self._host.scrub(f"{type(exc).__name__}: {exc}"),
                )
                return
            if self._sessions.get(session.guild_id) is not session:
                return
            session.mixer.queue(pcm)
            self._touch(session)
            session.connection.play(session.mixer)


@dataclass(frozen=True)
class _Anonymous:
    """A speaker the guild's member cache does not know: only the id is checked."""

    id: str
    roles: tuple[Any, ...] | None = None
