"""Voice mode for the classic REPL (P10-19): talk instead of typing, optionally hear the answer.

``/voice on`` (or ``indus chat --voice``) first checks that a take can work here: the audio
library of the ``voice`` extra imports, its PortAudio library loads, an input device exists and a
speech-to-text provider (P10-18) is ready. Any failure is one plain sentence and voice stays off.

While voice is on, the record key (``voice.record_key``, default Ctrl+Space) starts a take:
16 kHz, mono, 16-bit, read in 0.1 s blocks from ``sounddevice`` raw streams (no numpy). A take
ends when

- the key is let go — terminals report no key release, so ``KeyGesture`` reads auto-repeat: a
  press soon after the previous one means "held", and repeats stopping means "let go"; a plain
  tap (no repeats) starts a take that the next tap ends;
- the person spoke and then stayed under ``voice.silence_threshold`` (RMS of the 16-bit samples)
  for ``voice.silence_seconds`` (2 s);
- nobody spoke within ``voice.wait_seconds``, or the take reached ``voice.max_seconds``;
- a card opens (approval, clarify, sudo, secret): the take is thrown away. The key is refused
  while a card is open, so nothing typed into or said over a question is ever recorded.

A take that never rose above the threshold is dropped. Otherwise the WAV goes to
``<home>/cache/audio/`` and is transcribed off the prompt's thread; the transcript is sent as a
user turn exactly as if typed (the REPL echoes it first). It is deleted after a good transcript and
kept (its path shown) after a failure. With ``voice.read_aloud`` the final answer of each completed
turn — the text after its last tool call — is cleaned of markdown and spoken through the P10-17
speech engine, then played; a new take stops the playback.

Threads: key handling, ``/voice`` and ``observe`` run on the prompt's loop thread; audio blocks
arrive on the audio library's thread and only append to the take (a stop is posted back to the
loop); transcription and speech run on worker threads (``spawn``) and post their results back.
"""

from __future__ import annotations

import array
import contextlib
import importlib
import logging
import math
import re
import sys
import threading
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings

from indus.cli.slash.context import Handler, SlashContext, SlashResult
from indus.foundation.clock import Clock
from indus.foundation.scope import Scope
from indus.protocol.events import (
    ExitReason,
    MessageDelta,
    ToolStarted,
    TurnDone,
    TurnStarted,
)
from indus.tools.builtin.media.common import AUDIO_DIR, folder, unique_name
from indus.tools.builtin.media.voice_take import (
    BLOCK_SECONDS,
    CHANNELS,
    DEFAULT_KEY,
    SAMPLE_BYTES,
    SAMPLE_RATE,
    Capture,
    StopReason,
    VoiceSettings,
    level,
)

if TYPE_CHECKING:
    from indus.cli.repl.app import Repl
    from indus.service.host_inprocess import InProcessHost
    from indus.tools.builtin.media.stt import Transcript

__all__ = [
    "CHANNELS",
    "DEFAULT_KEY",
    "HANDLERS",
    "MESSAGES",
    "SAMPLE_RATE",
    "AudioDevice",
    "Capture",
    "InputStream",
    "KeyGesture",
    "RecordKey",
    "Resampler",
    "SoundDevice",
    "VoiceMode",
    "VoiceServices",
    "VoiceSettings",
    "attach_voice",
    "cmd_voice",
    "host_services",
    "level",
    "message",
    "parse_record_key",
    "spoken_text",
]

log = logging.getLogger(__name__)

REPEAT_GAP = 0.6
"""A press this soon after the previous one is the key being held (auto-repeat)."""
RELEASE_GAP = 0.35
"""Once held, no repeat for this long means the key was let go."""
STT_TIMEOUT_S = 600.0
TTS_TIMEOUT_S = 300.0
EXTRA_HINT = "pip install 'indus-agent[voice]'"

# Our own status strings, mirrored in locales/en.yaml under cli.voice (a test keeps them equal).
MESSAGES: dict[str, str] = {
    "now_on": "Voice mode on: hold {key} and speak (or tap it to start and again to stop).",
    "on_read_aloud": "Answers are read aloud (voice.read_aloud).",
    "now_off": "Voice mode off.",
    "already_on": "Voice mode is already on.",
    "already_off": "Voice mode is already off.",
    "unavailable": "Voice mode stays off: {reason}.",
    "not_here": "Voice mode is not available in this window.",
    "usage": "Usage: /voice [on|off|status]",
    "turn_on_first": "Voice mode is off. Turn it on with /voice on.",
    "card_open": "Answer the question first; nothing is recorded while it is open.",
    "card_cancelled": "Recording dropped: a question is waiting for your answer.",
    "still_working": "Still turning the last recording into text.",
    "listening": "Listening: let go of {key}, tap it again or pause to send.",
    "nothing_heard": "Nothing was heard, so nothing was sent.",
    "transcribing": "Turning the recording into text…",
    "inaudible": "No words could be made out, so nothing was sent.",
    "stt_failed": "Could not turn the recording into text: {reason}. It is kept at {path}.",
    "mic_failed": "Could not start recording: {reason}.",
    "speak_failed": "Could not read the answer aloud: {reason}.",
    "not_playable": "The spoken answer is {fmt}, which cannot be played here; it is at {path}.",
    "bad_key": "voice.record_key '{value}' cannot be used here, so {key} is used.",
    "no_library": "the sounddevice audio library is missing (install the voice extra: {hint})",
    "no_portaudio": "the PortAudio system library is missing (for example libportaudio2)",
    "no_microphone": "no microphone was found",
    "status_mode": "Voice mode: {state}",
    "status_key": "Record key: {key}",
    "status_read_aloud": "Read answers aloud: {state}",
    "status_now": "Now: {activity}",
    "word_on": "on",
    "word_off": "off",
    "word_idle": "idle",
    "word_recording": "recording",
    "word_transcribing": "transcribing",
}


def message(name: str, /, **values: object) -> str:
    """The status string ``name`` with its placeholders filled."""
    return MESSAGES[name].format(**values)


# -- the record key ------------------------------------------------------------------------------


@dataclass(frozen=True)
class RecordKey:
    """A key as prompt_toolkit binds it, and as people read it."""

    keys: tuple[str, ...]
    label: str


_CTRL = frozenset({"ctrl", "control", "ctl", "c"})
_ALT = frozenset({"alt", "option", "opt", "meta", "m", "a"})
# Ctrl keys the REPL or the terminal already needs: quit/interrupt, end of input, newline,
# Enter, Tab, Backspace, Escape, suspend, the editor keys, clear screen and flow control.
_TAKEN_CTRL = frozenset("cdjmihzgxlqs[")


def parse_record_key(text: object) -> RecordKey | None:
    """``ctrl+space``, ``ctrl+b``, ``alt+v``, ``alt+space`` or ``f1``..``f12`` → a ``RecordKey``;
    anything else (or a key the REPL needs) → None."""
    if not isinstance(text, str):
        return None
    words = [w.strip() for w in text.strip().lower().replace("-", "+").split("+")]
    if not words or any(not w for w in words):
        return None
    if len(words) == 1:
        name = words[0]
        if re.fullmatch(r"f([1-9]|1[0-2])", name):
            return RecordKey((name,), name.upper())
        return None
    if len(words) != 2:
        return None
    modifier, name = words
    if name == "space":
        shown = "Space"
    elif len(name) == 1 and name.isalnum():
        shown = name.upper()
    else:
        return None
    if modifier in _CTRL:
        if name == "space":
            return RecordKey(("c-space",), "Ctrl+Space")
        if not name.isalpha() or name in _TAKEN_CTRL:
            return None
        return RecordKey((f"c-{name}",), f"Ctrl+{shown}")
    if modifier in _ALT:
        return RecordKey(("escape", " " if name == "space" else name), f"Alt+{shown}")
    return None


DEFAULT_RECORD_KEY = RecordKey(("c-space",), "Ctrl+Space")


class KeyGesture:
    """Reads presses of the record key as hold-and-release or tap-and-tap (see module notes)."""

    def __init__(self, repeat_gap: float = REPEAT_GAP, release_gap: float = RELEASE_GAP) -> None:
        self.repeat_gap = repeat_gap
        self.release_gap = release_gap
        self._last: float | None = None
        self._held = False

    def begin(self, now: float) -> None:
        """The press that started a take."""
        self._last = now
        self._held = False

    def press(self, now: float) -> Literal["repeat", "up"]:
        """A press during a take: still held (auto-repeat), or the press that ends a tap take."""
        last = self._last
        self._last = now
        if last is not None and now - last <= self.repeat_gap:
            self._held = True
            return "repeat"
        return "up"

    def lingering(self, now: float) -> bool:
        """A press with no take running that is only the key still held (or bouncing) after a
        take ended; it must not start another take."""
        last = self._last
        if last is not None and now - last <= self.repeat_gap:
            self._last = now
            return True
        return False

    def released(self, now: float) -> bool:
        """True once a held key has stopped repeating."""
        return self._held and self._last is not None and now - self._last > self.release_gap


# -- audio ---------------------------------------------------------------------------------------


class Resampler:
    """Linear interpolation of 16-bit mono audio from ``source`` Hz to ``target`` Hz, block by
    block (the position and the last sample carry over, so blocks join without a seam)."""

    def __init__(self, source: int, target: int) -> None:
        self.step = source / target
        self._position = 0.0
        self._last: int | None = None

    def feed(self, data: bytes) -> bytes:
        samples = array.array("h")
        samples.frombytes(data[: len(data) - len(data) % SAMPLE_BYTES])
        if sys.byteorder == "big":
            samples.byteswap()
        if not samples:
            return b""
        points = ([self._last] if self._last is not None else []) + samples.tolist()
        shift = len(points) - len(samples)
        out = array.array("h")
        position = self._position
        while True:
            at = position + shift
            index = math.floor(at)
            if index + 1 >= len(points):
                break
            low, high = points[index], points[index + 1]
            out.append(round(low + (high - low) * (at - index)))
            position += self.step
        self._position = position - len(samples)
        self._last = points[-1]
        if sys.byteorder == "big":
            out.byteswap()
        return out.tobytes()


class InputStream(Protocol):
    """An open microphone stream; blocks go to the callback given when it was opened."""

    def start(self) -> None: ...

    def close(self) -> None: ...


class AudioDevice(Protocol):
    """The sound hardware as voice mode uses it."""

    def problem(self) -> str | None:
        """Why recording cannot work here, or None."""
        ...

    def open_input(self, on_block: Callable[[bytes], None]) -> InputStream:
        """A 16 kHz mono 16-bit input stream calling ``on_block`` with raw bytes."""
        ...

    def play_wav(self, path: Path, stop: threading.Event) -> None:
        """Play a 16-bit PCM WAV file to its end, or until ``stop`` is set."""
        ...


class SoundDevice:
    """``AudioDevice`` over the ``sounddevice`` library (imported only when used)."""

    def _library(self) -> Any:
        return importlib.import_module("sounddevice")

    def problem(self) -> str | None:
        try:
            library = self._library()
        except ImportError:
            return message("no_library", hint=EXTRA_HINT)
        except OSError:
            return message("no_portaudio")
        try:
            found = library.query_devices(kind="input")
        except Exception:
            return message("no_microphone")
        return None if found else message("no_microphone")

    def open_input(self, on_block: Callable[[bytes], None]) -> InputStream:
        """16 kHz when the device takes it; otherwise its own rate, converted to 16 kHz here."""
        library = self._library()
        try:
            return self._input(library, SAMPLE_RATE, on_block)
        except Exception:
            rate = self._default_rate(library)
            if rate is None or rate == SAMPLE_RATE:
                raise
            log.info("the microphone refused %d Hz; recording at %d Hz", SAMPLE_RATE, rate)
            converter = Resampler(rate, SAMPLE_RATE)
            return self._input(library, rate, lambda data: on_block(converter.feed(data)))

    def _default_rate(self, library: Any) -> int | None:
        try:
            info = library.query_devices(kind="input")
            rate = info["default_samplerate"] if isinstance(info, dict) else None
            rate = rate if rate is not None else getattr(info, "default_samplerate", None)
        except Exception:
            return None
        return round(rate) if isinstance(rate, int | float) and rate > 0 else None

    def _input(self, library: Any, rate: int, on_block: Callable[[bytes], None]) -> InputStream:
        def deliver(data: Any, _frames: int, _when: Any, _status: Any) -> None:
            try:
                on_block(bytes(data))
            except Exception:
                log.debug("voice block handling failed", exc_info=True)

        return library.RawInputStream(
            samplerate=rate,
            channels=CHANNELS,
            dtype="int16",
            blocksize=int(rate * BLOCK_SECONDS),
            callback=deliver,
        )

    def play_wav(self, path: Path, stop: threading.Event) -> None:
        library = self._library()
        with wave.open(str(path), "rb") as reader:
            if reader.getsampwidth() != SAMPLE_BYTES:
                raise ValueError("only 16-bit WAV audio can be played")
            rate, channels = reader.getframerate(), reader.getnchannels()
            step = max(1, int(rate * BLOCK_SECONDS))
            with library.RawOutputStream(samplerate=rate, channels=channels, dtype="int16") as out:
                while not stop.is_set():
                    chunk = reader.readframes(step)
                    if not chunk:
                        break
                    out.write(chunk)


# -- spoken answers ------------------------------------------------------------------------------

_FENCE = re.compile(r"```.*?(?:```|$)", re.DOTALL)
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_URL = re.compile(r"\b(?:https?|ftp)://\S+", re.IGNORECASE)
_LINE_MARKS = re.compile(r"^[ \t]*(?:#{1,6}[ \t]+|>[ \t]?|[-*+][ \t]+|\d+[.)][ \t]+)", re.MULTILINE)
_RULE = re.compile(r"^[ \t]*(?:[-*_][ \t]*){3,}$", re.MULTILINE)
_EMPHASIS = re.compile(r"(\*\*|__|\*|_|~~|`)(?=\S)(.+?)(?<=\S)\1")
_TABLE_EDGE = re.compile(r"^[ \t]*\|?[ \t:|-]+\|[ \t:|-]*$", re.MULTILINE)


def spoken_text(markdown: str) -> str:
    """``markdown`` as something worth saying: no code blocks, links read by their text, no bare
    addresses, no markup characters; whitespace folded."""
    text = _FENCE.sub(" ", markdown)
    text = _LINK.sub(r"\1", text)
    text = _URL.sub(" ", text)
    text = _RULE.sub(" ", text)
    text = _TABLE_EDGE.sub(" ", text)
    text = _LINE_MARKS.sub("", text)
    for _ in range(2):  # nested emphasis such as ***word***
        text = _EMPHASIS.sub(r"\2", text)
    text = text.replace("|", " ").replace("`", "")
    return " ".join(text.split())


# -- the controller ------------------------------------------------------------------------------


@dataclass(frozen=True)
class VoiceServices:
    """What voice mode asks of the rest of Indus (blocking calls, run on worker threads)."""

    transcribe: Callable[[Path], Transcript]
    stt_problem: Callable[[], str | None]
    speak: Callable[[str], Path]


def _spawn(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="indus-voice", daemon=True).start()


class VoiceMode:
    """Voice mode of one REPL (see the module notes)."""

    def __init__(
        self,
        scope: Scope,
        *,
        device: AudioDevice,
        services: VoiceServices,
        say: Callable[[str], None],
        submit: Callable[[str], None],
        card_open: Callable[[], bool],
        post: Callable[[Callable[[], None]], None],
        spawn: Callable[[Callable[[], None]], None] = _spawn,
        clock: Clock | None = None,
        settings: VoiceSettings | None = None,
    ) -> None:
        self.scope = scope
        self.device = device
        self.services = services
        self.say = say
        self.submit = submit
        self.card_open = card_open
        self.post = post
        self.spawn = spawn
        self.clock = clock or scope.clock
        self.settings = settings or VoiceSettings.from_scope(scope)
        parsed = parse_record_key(self.settings.record_key)
        self.key = parsed or DEFAULT_RECORD_KEY
        self.key_note = (
            None
            if parsed is not None
            else message("bad_key", value=self.settings.record_key, key=self.key.label)
        )
        self.enabled = False
        self.gesture = KeyGesture()
        self._lock = threading.Lock()
        self._take: Capture | None = None
        self._stream: InputStream | None = None
        self._stopping = False
        self._transcribing = False
        self._answer: list[str] = []
        self._playback: threading.Event | None = None

    # -- state ---------------------------------------------------------------------------------

    @property
    def recording(self) -> bool:
        return self._take is not None

    @property
    def transcribing(self) -> bool:
        return self._transcribing

    def turn_on(self) -> bool:
        """Check the requirements and turn voice on; False (one message said) when it cannot."""
        if self.enabled:
            self.say(message("already_on"))
            return True
        reason = self._problem()
        if reason:
            self.say(message("unavailable", reason=reason))
            return False
        self.enabled = True
        if self.key_note:
            self.say(self.key_note)
        self.say(message("now_on", key=self.key.label))
        if self.settings.read_aloud:
            self.say(message("on_read_aloud"))
        return True

    def _problem(self) -> str | None:
        for check in (self.device.problem, self.services.stt_problem):
            try:
                reason = check()
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
            if reason:
                return reason.rstrip(".")
        return None

    def turn_off(self) -> None:
        """Drop a take in progress, stop speaking and turn voice off."""
        if not self.enabled:
            self.say(message("already_off"))
            return
        self.enabled = False
        self._end("cancel")
        self._stop_playback()
        self.say(message("now_off"))

    def status_lines(self) -> list[str]:
        if self._take is not None:
            now = "word_recording"
        elif self._transcribing:
            now = "word_transcribing"
        else:
            now = "word_idle"
        lines = [
            message("status_mode", state=message("word_on" if self.enabled else "word_off")),
            message("status_key", key=self.key.label),
            message(
                "status_read_aloud",
                state=message("word_on" if self.settings.read_aloud else "word_off"),
            ),
            message("status_now", activity=message(now)),
        ]
        if self.key_note:
            lines.append(self.key_note)
        return lines

    # -- keys ------------------------------------------------------------------------------------

    def key_bindings(self) -> KeyBindings:
        """The record key, live only while voice is on (other keys keep their meaning)."""
        bindings = KeyBindings()

        def pressed(_event: Any) -> None:
            self.key_press()

        bindings.add(*self.key.keys, filter=Condition(lambda: self.enabled), eager=True)(pressed)
        return bindings

    def key_press(self) -> None:
        """One press of the record key as the terminal reports it."""
        now = self.clock.monotonic()
        if self._take is None:
            if not self.gesture.lingering(now):
                self.key_down()
            return
        if self.gesture.press(now) == "up":
            self.key_up()

    def key_down(self) -> None:
        """Start a take (when voice is on, no card is open and no take or transcription runs)."""
        if not self.enabled:
            self.say(message("turn_on_first"))
            return
        if self.card_open():
            self.say(message("card_open"))
            return
        if self._transcribing:
            self.say(message("still_working"))
            return
        if self._take is not None:
            return
        self._stop_playback()
        now = self.clock.monotonic()
        take = Capture(self.settings, now)
        with self._lock:
            self._take = take
            self._stream = None
            self._stopping = False
        self.gesture.begin(now)
        self.spawn(lambda: self._open(take))

    def _open(self, take: Capture) -> None:
        """Worker thread: open the microphone (it can block for a while on some systems)."""
        stream: InputStream | None = None
        try:
            stream = self.device.open_input(self._on_block)
            stream.start()
        except Exception as exc:
            if stream is not None:
                _close_quietly(stream)
            reason = _reason(exc)
            self.post(lambda: self._open_failed(take, reason))
            return
        with self._lock:
            current = self._take is take
            if current:
                self._stream = stream
        if not current:  # the take ended while the microphone was opening
            _close_quietly(stream)
            return
        self.post(lambda: self.say(message("listening", key=self.key.label)))

    def _open_failed(self, take: Capture, reason: str) -> None:
        with self._lock:
            if self._take is not take:
                return
            self._take = self._stream = None
        self.say(message("mic_failed", reason=reason))

    def key_up(self) -> None:
        """The record key was let go: finish the take."""
        self._end("released")

    # -- the take --------------------------------------------------------------------------------

    def _on_block(self, block: bytes) -> None:
        """Audio thread: keep the block; post the end of the take when a rule says so."""
        with self._lock:
            take = self._take
            if take is None or self._stopping:
                return
            now = self.clock.monotonic()
            reason = take.add(block, now)
            if reason is None and self._card_open_safely():
                reason = "card"
            if reason is None and self.gesture.released(now):
                reason = "released"
            if reason is None:
                return
            self._stopping = True
        chosen: StopReason = reason
        self.post(lambda: self._end(chosen))

    def _card_open_safely(self) -> bool:
        try:
            return self.card_open()
        except Exception:
            return False

    def _end(self, reason: StopReason) -> None:
        """Loop thread: close the stream and decide what the take becomes."""
        with self._lock:
            take, stream = self._take, self._stream
            self._take = self._stream = None
            self._stopping = False
        if take is None:
            return
        if stream is not None:
            closing = stream
            self.spawn(lambda: _close_quietly(closing))
        if reason == "cancel":
            return
        if reason == "card":
            self.say(message("card_cancelled"))
            return
        if not take.usable:
            self.say(message("nothing_heard"))
            return
        try:
            target = folder(self.scope, AUDIO_DIR) / unique_name("voice", "wav", self.scope)
            target.write_bytes(take.wav())
        except OSError as exc:
            self.say(message("mic_failed", reason=_reason(exc)))
            return
        self._transcribing = True
        self.say(message("transcribing"))
        self.spawn(lambda: self._transcribe(target))

    def _transcribe(self, path: Path) -> None:
        """Worker thread: turn the take into text, then hand the outcome to the loop."""
        try:
            outcome: Transcript | Exception = self.services.transcribe(path)
        except Exception as exc:
            outcome = exc
        self.post(lambda: self._deliver(path, outcome))

    def _deliver(self, path: Path, outcome: Transcript | Exception) -> None:
        self._transcribing = False
        if isinstance(outcome, Exception):
            self.say(message("stt_failed", reason=_reason(outcome), path=path))
            return
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)
        if not self.enabled:
            return
        text = outcome.text.strip()
        if outcome.inaudible or not text:
            self.say(message("inaudible"))
            return
        self.submit(text)

    # -- reading answers aloud -------------------------------------------------------------------

    def observe(self, item: object) -> None:
        """Feed observer (loop thread): collect the final answer, speak it when the turn ends."""
        if isinstance(item, TurnStarted | ToolStarted):
            self._answer = []
        elif isinstance(item, MessageDelta):
            self._answer.append(item.text)
        elif isinstance(item, TurnDone):
            answer, self._answer = "".join(self._answer), []
            if not (self.enabled and self.settings.read_aloud):
                return
            if item.reason is not ExitReason.COMPLETED or item.failure is not None:
                return
            words = spoken_text(answer)
            if words:
                self.spawn(lambda: self._read_aloud(words))

    def _read_aloud(self, words: str) -> None:
        """Worker thread: make the speech and play it."""
        stop = threading.Event()
        self._playback = stop
        try:
            path = self.services.speak(words)
        except Exception as exc:
            reason = _reason(exc)
            self.post(lambda: self.say(message("speak_failed", reason=reason)))
            return
        if path.suffix.lower() != ".wav":
            fmt = path.suffix.lstrip(".").upper() or "unknown"
            self.post(lambda: self.say(message("not_playable", fmt=fmt, path=path)))
            return
        try:
            if not stop.is_set():
                self.device.play_wav(path, stop)
        except Exception as exc:
            reason = _reason(exc)
            self.post(lambda: self.say(message("speak_failed", reason=reason)))
        finally:
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)

    def _stop_playback(self) -> None:
        playback = self._playback
        if playback is not None:
            playback.set()


def _close_quietly(stream: InputStream) -> None:
    try:
        stream.close()
    except Exception:
        log.debug("closing the microphone stream failed", exc_info=True)


def _reason(exc: BaseException) -> str:
    text = str(exc).strip().rstrip(".")
    return text or type(exc).__name__


# -- wiring --------------------------------------------------------------------------------------


def host_services(host: InProcessHost, scope: Scope) -> VoiceServices:
    """The P10-18 transcription and P10-17 speech calls, run on the host's loop."""
    from indus.tools.builtin.media import stt, tts
    from indus.tools.builtin.media.providers import NoProvider, active

    def transcribe(path: Path) -> Transcript:
        return host.call(stt.transcribe(path, scope=scope), timeout=STT_TIMEOUT_S)

    def stt_problem() -> str | None:
        try:
            provider = active(stt.DEFAULT_REGISTRY, scope, stt.CONFIG_KEY)
        except NoProvider as exc:
            return str(exc)
        return provider.missing(scope)

    def speak(text: str) -> Path:
        target = folder(scope, AUDIO_DIR) / unique_name("answer", "wav", scope)
        made = host.call(
            tts.DEFAULT_TOOL.speak(text, scope, output_path=str(target)), timeout=TTS_TIMEOUT_S
        )
        return made.path

    return VoiceServices(transcribe=transcribe, stt_problem=stt_problem, speak=speak)


def attach_voice(
    repl: Repl,
    ctx: SlashContext,
    services: VoiceServices,
    *,
    device: AudioDevice | None = None,
    turn_on: bool = False,
) -> VoiceMode:
    """Give ``repl`` voice mode: the record key, the read-aloud observer and ``/voice``.
    ``turn_on`` (``indus chat --voice``) turns it on once the prompt is up."""

    def post(fn: Callable[[], None]) -> None:
        repl.post(fn)

    voice = VoiceMode(
        repl.scope,
        device=device or SoundDevice(),
        services=services,
        say=repl.note,
        submit=repl.send,
        card_open=lambda: repl.card is not None,
        post=post,
        clock=repl.clock,
    )
    repl.key_extras.append(voice.key_bindings())
    repl.observers.append(voice.observe)
    ctx.voice = voice
    if turn_on:

        def start() -> None:
            voice.turn_on()

        post(start)
    return voice


# -- /voice --------------------------------------------------------------------------------------


def cmd_voice(ctx: SlashContext, args: str) -> SlashResult:
    """``/voice [on|off|status]``; no argument shows the status."""
    voice = ctx.voice
    words = args.lower().split()
    if voice is None:
        ctx.say(message("not_here"))
        return SlashResult()
    if words == ["on"]:
        voice.turn_on()
    elif words == ["off"]:
        voice.turn_off()
    elif words in ([], ["status"]):
        ctx.say(*voice.status_lines())
    else:
        ctx.say(message("usage"))
    return SlashResult()


HANDLERS: dict[str, Handler] = {"voice": cmd_voice}
