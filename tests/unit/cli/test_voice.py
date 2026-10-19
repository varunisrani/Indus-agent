"""CLI voice mode (P10-19): a fake microphone, fake speech services and the fake clock."""

from __future__ import annotations

import threading
import wave
from array import array
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from indus.cli import voice as voice_mod
from indus.cli.slash.context import SlashContext, SlashResult
from indus.cli.slash.dispatch import dispatch
from indus.cli.voice import (
    CHANNELS,
    MESSAGES,
    SAMPLE_RATE,
    Capture,
    KeyGesture,
    Resampler,
    SoundDevice,
    VoiceMode,
    VoiceServices,
    VoiceSettings,
    cmd_voice,
    level,
    message,
    parse_record_key,
    spoken_text,
)
from indus.foundation import yamlio
from indus.foundation.scope import Scope
from indus.protocol.events import ExitReason, MessageDelta, ToolStarted, TurnDone, TurnStarted
from indus.protocol.server_requests import ApprovalRequest
from indus.tools.builtin.media.stt import SttError, Transcript
from tests.fakes.agent import DictConfig
from tests.fakes.clock import FakeClock
from tests.fakes.service import SESSION

ROOT = Path(__file__).resolve().parents[3]
BLOCK = int(SAMPLE_RATE * 0.1)
LOUD = 3000
QUIET = 20


def block(amplitude: int) -> bytes:
    """0.1 s of a square wave whose RMS level is ``amplitude``."""
    return array("h", [amplitude if i % 2 else -amplitude for i in range(BLOCK)]).tobytes()


# -- fakes ---------------------------------------------------------------------------------------


class FakeStream:
    def __init__(self) -> None:
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self.closed = True


class FakeDevice:
    """A microphone the test feeds block by block, and a speaker that records what it played."""

    def __init__(self, problem: str | None = None) -> None:
        self.problem_text = problem
        self.open_error: Exception | None = None
        self.streams: list[FakeStream] = []
        self.on_block: Callable[[bytes], None] | None = None
        self.played: list[Path] = []

    def problem(self) -> str | None:
        return self.problem_text

    def open_input(self, on_block: Callable[[bytes], None]) -> FakeStream:
        if self.open_error is not None:
            raise self.open_error
        self.on_block = on_block
        stream = FakeStream()
        self.streams.append(stream)
        return stream

    def play_wav(self, path: Path, stop: threading.Event) -> None:
        assert path.exists()
        self.played.append(path)


@dataclass
class Rig:
    voice: VoiceMode
    device: FakeDevice
    clock: FakeClock
    said: list[str]
    sent: list[str]
    heard: list[bytes] = field(default_factory=list[bytes])
    spoken: list[str] = field(default_factory=list[str])
    card: list[bool] = field(default_factory=lambda: [False])
    reply: list[Transcript | Exception] = field(
        default_factory=lambda: [Transcript("hello there", "en", 1.0)]
    )

    def feed(self, amplitude: int, seconds: float = 0.1) -> None:
        """Advance the clock and deliver blocks, one per 0.1 s (as the audio thread would)."""
        for _ in range(round(seconds / 0.1)):
            self.clock.advance(0.1)
            if self.voice.recording:
                assert self.device.on_block is not None
                self.device.on_block(block(amplitude))


@pytest.fixture
def make_rig(scope: Scope, fake_clock: FakeClock, tmp_path: Path) -> Callable[..., Rig]:
    def build(
        *,
        problem: str | None = None,
        stt_problem: str | None = None,
        settings: VoiceSettings | None = None,
        speech_suffix: str = ".wav",
    ) -> Rig:
        device = FakeDevice(problem)
        said: list[str] = []
        sent: list[str] = []
        holder: dict[str, Rig] = {}

        def transcribe(path: Path) -> Transcript:
            rig = holder["rig"]
            rig.heard.append(path.read_bytes())
            outcome = rig.reply[0]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        def speak(text: str) -> Path:
            holder["rig"].spoken.append(text)
            out = tmp_path / f"answer-{len(holder['rig'].spoken)}{speech_suffix}"
            out.write_bytes(b"RIFF....")
            return out

        voice = VoiceMode(
            scope,
            device=device,
            services=VoiceServices(transcribe, lambda: stt_problem, speak),
            say=said.append,
            submit=sent.append,
            card_open=lambda: holder["rig"].card[0],
            post=lambda fn: fn(),
            spawn=lambda fn: fn(),
            clock=fake_clock,
            settings=settings or VoiceSettings(),
        )
        rig = Rig(voice, device, fake_clock, said, sent)
        holder["rig"] = rig
        return rig

    return build


@pytest.fixture
def rig(make_rig: Callable[..., Rig]) -> Rig:
    made = make_rig()
    assert made.voice.turn_on()
    made.said.clear()
    return made


# -- push to talk ----------------------------------------------------------------------------------


def test_key_down_and_up_submits_the_transcript_as_a_user_turn(rig: Rig) -> None:
    rig.voice.key_down()
    assert rig.voice.recording and rig.device.streams[0].started
    assert rig.said == [message("listening", key="Ctrl+Space")]
    rig.feed(LOUD, 0.5)
    rig.voice.key_up()
    assert rig.sent == ["hello there"]
    assert not rig.voice.recording and rig.device.streams[0].closed
    assert len(rig.heard) == 1
    wav_dir = rig.voice.scope.home / "cache" / "audio"
    assert list(wav_dir.glob("voice-*.wav")) == []  # deleted after a good transcript


def test_the_take_is_16khz_mono_16bit(rig: Rig, tmp_path: Path) -> None:
    rig.voice.key_down()
    rig.feed(LOUD, 0.5)
    rig.voice.key_up()
    copy = tmp_path / "take.wav"
    copy.write_bytes(rig.heard[0])
    with wave.open(str(copy), "rb") as reader:
        assert reader.getframerate() == SAMPLE_RATE == 16_000
        assert reader.getnchannels() == CHANNELS == 1
        assert reader.getsampwidth() == 2
        assert reader.getnframes() == 5 * BLOCK


def test_silence_after_speech_stops_the_take_at_two_seconds(rig: Rig) -> None:
    rig.voice.key_down()
    rig.feed(LOUD, 1.0)
    rig.feed(QUIET, 1.9)
    assert rig.voice.recording and rig.sent == []
    rig.feed(QUIET, 0.1)
    assert not rig.voice.recording
    assert rig.sent == ["hello there"]


def test_nobody_speaking_stops_at_the_wait_limit_and_sends_nothing(rig: Rig) -> None:
    rig.voice.key_down()
    rig.feed(QUIET, 9.9)
    assert rig.voice.recording
    rig.feed(QUIET, 0.1)
    assert not rig.voice.recording
    assert rig.heard == [] and rig.sent == []
    assert rig.said[-1] == message("nothing_heard")


def test_a_quiet_take_released_early_is_dropped(rig: Rig) -> None:
    rig.voice.key_down()
    rig.feed(QUIET, 1.0)
    rig.voice.key_up()
    assert rig.heard == [] and rig.said[-1] == message("nothing_heard")


def test_the_threshold_comes_from_the_settings(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig(settings=VoiceSettings(silence_threshold=10))
    rig.voice.turn_on()
    rig.voice.key_down()
    rig.feed(QUIET, 0.5)  # 20 RMS is speech for a threshold of 10
    rig.voice.key_up()
    assert rig.sent == ["hello there"]


def test_hard_cap_ends_a_long_take(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig(settings=VoiceSettings(max_seconds=3))
    rig.voice.turn_on()
    rig.voice.key_down()
    rig.feed(LOUD, 2.9)
    assert rig.voice.recording
    rig.feed(LOUD, 0.1)
    assert not rig.voice.recording and rig.sent == ["hello there"]


def test_holding_the_key_keeps_recording_and_letting_go_sends(rig: Rig) -> None:
    rig.voice.key_press()  # the press that starts the take
    for _ in range(10):  # the terminal repeats the held key every 0.1 s
        rig.feed(LOUD, 0.1)
        rig.voice.key_press()
    assert rig.voice.recording
    rig.feed(LOUD, 0.3)  # repeats stopped: 0.3 s is still inside the release gap
    assert rig.voice.recording
    rig.feed(LOUD, 0.1)
    assert not rig.voice.recording
    assert rig.sent == ["hello there"]


def test_a_key_still_held_after_a_silence_stop_does_not_start_again(rig: Rig) -> None:
    rig.voice.key_press()
    rig.feed(LOUD, 0.5)
    for _ in range(22):  # still holding the key while the quiet stops the take
        rig.voice.key_press()
        rig.feed(QUIET, 0.1)
    assert rig.sent == ["hello there"] and not rig.voice.recording
    rig.voice.key_press()
    assert not rig.voice.recording and len(rig.device.streams) == 1
    rig.clock.advance(1.0)
    rig.voice.key_press()  # a fresh press later starts a new take
    assert rig.voice.recording and len(rig.device.streams) == 2


def test_a_tap_starts_and_the_next_tap_stops(rig: Rig) -> None:
    rig.voice.key_press()
    rig.feed(LOUD, 1.0)  # no repeats: the take keeps going
    assert rig.voice.recording
    rig.voice.key_press()
    assert not rig.voice.recording
    assert rig.sent == ["hello there"]


def test_key_gesture_rules() -> None:
    gesture = KeyGesture(repeat_gap=0.6, release_gap=0.35)
    gesture.begin(0.0)
    assert not gesture.released(5.0)  # a tap never "releases" by itself
    assert gesture.press(0.5) == "repeat"
    assert not gesture.released(0.8)
    assert gesture.released(0.9)
    gesture.begin(10.0)
    assert gesture.press(11.0) == "up"


def test_a_second_start_while_recording_does_nothing(rig: Rig) -> None:
    rig.voice.key_down()
    rig.voice.key_down()
    assert len(rig.device.streams) == 1


# -- refusals and failures ---------------------------------------------------------------------


def test_no_microphone_gives_one_message_and_voice_stays_off(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(problem=message("no_microphone"))
    assert rig.voice.turn_on() is False
    assert rig.said == ["Voice mode stays off: no microphone was found."]
    assert not rig.voice.enabled
    rig.voice.key_down()
    assert rig.device.streams == []
    assert rig.said[-1] == message("turn_on_first")


def test_speech_to_text_not_ready_keeps_voice_off(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig(stt_problem="faster-whisper is not installed.")
    assert rig.voice.turn_on() is False
    assert rig.said == ["Voice mode stays off: faster-whisper is not installed."]


def test_sound_device_names_the_missing_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(name: str) -> Any:
        raise ImportError(name)

    monkeypatch.setattr(voice_mod.importlib, "import_module", missing)
    reason = SoundDevice().problem()
    assert reason is not None and "voice extra" in reason and "indus-agent[voice]" in reason


def test_sound_device_names_portaudio_and_a_missing_microphone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_library(name: str) -> Any:
        raise OSError("PortAudio library not found")

    monkeypatch.setattr(voice_mod.importlib, "import_module", no_library)
    assert SoundDevice().problem() == message("no_portaudio")

    class NoInputs:
        def query_devices(self, kind: str) -> Any:
            raise ValueError("No input device matching")

    monkeypatch.setattr(voice_mod.importlib, "import_module", lambda name: NoInputs())
    assert SoundDevice().problem() == message("no_microphone")


def test_an_open_card_refuses_recording(rig: Rig) -> None:
    rig.card[0] = True
    rig.voice.key_down()
    assert rig.device.streams == [] and not rig.voice.recording
    assert rig.said == [message("card_open")]


def test_a_card_arriving_during_a_take_drops_it(rig: Rig) -> None:
    rig.voice.key_down()
    rig.feed(LOUD, 0.5)
    rig.card[0] = True
    rig.feed(LOUD, 0.1)
    assert not rig.voice.recording and rig.device.streams[0].closed
    assert rig.heard == [] and rig.sent == []
    assert rig.said[-1] == message("card_cancelled")


def test_the_microphone_failing_to_open_is_one_message(rig: Rig) -> None:
    rig.device.open_error = OSError("Device unavailable")
    rig.voice.key_down()
    assert not rig.voice.recording
    assert rig.said == [message("mic_failed", reason="Device unavailable")]


def test_inaudible_transcript_sends_nothing(rig: Rig) -> None:
    rig.reply[0] = Transcript("(inaudible)", inaudible=True)
    rig.voice.key_down()
    rig.feed(LOUD, 0.5)
    rig.voice.key_up()
    assert rig.sent == [] and rig.said[-1] == message("inaudible")


def test_failed_transcription_keeps_the_recording(rig: Rig) -> None:
    rig.reply[0] = SttError("the provider refused the key")
    rig.voice.key_down()
    rig.feed(LOUD, 0.5)
    rig.voice.key_up()
    kept = list((rig.voice.scope.home / "cache" / "audio").glob("voice-*.wav"))
    assert len(kept) == 1
    assert rig.said[-1] == message(
        "stt_failed", reason="the provider refused the key", path=kept[0]
    )
    assert rig.sent == [] and not rig.voice.transcribing


class Spawner:
    """Runs worker jobs at once, or holds them while ``hold`` is set."""

    def __init__(self) -> None:
        self.hold = False
        self.jobs: list[Callable[[], None]] = []

    def __call__(self, job: Callable[[], None]) -> None:
        if self.hold:
            self.jobs.append(job)
        else:
            job()

    def run_all(self) -> None:
        while self.jobs:
            self.jobs.pop(0)()


def test_a_new_take_waits_for_the_transcription(rig: Rig) -> None:
    spawner = Spawner()
    rig.voice.spawn = spawner
    rig.voice.key_down()
    rig.feed(LOUD, 0.5)
    spawner.hold = True
    rig.voice.key_up()
    assert rig.voice.transcribing
    rig.voice.key_down()
    assert rig.said[-1] == message("still_working") and len(rig.device.streams) == 1
    spawner.run_all()
    assert rig.sent == ["hello there"]


def test_turning_off_drops_the_take_and_a_late_transcript(rig: Rig) -> None:
    rig.voice.key_down()
    rig.feed(LOUD, 0.5)
    rig.voice.turn_off()
    assert not rig.voice.recording and rig.device.streams[0].closed
    assert rig.heard == [] and rig.said[-1] == message("now_off")

    spawner = Spawner()
    rig.voice.spawn = spawner
    rig.voice.turn_on()
    rig.voice.key_down()
    rig.feed(LOUD, 0.5)
    spawner.hold = True
    rig.voice.key_up()
    rig.voice.turn_off()
    spawner.run_all()
    assert len(rig.heard) == 1 and rig.sent == []


def test_a_take_ended_while_the_microphone_opens_closes_it(rig: Rig) -> None:
    spawner = Spawner()
    spawner.hold = True
    rig.voice.spawn = spawner
    rig.voice.key_down()
    assert rig.voice.recording and rig.device.streams == []
    rig.voice.key_up()
    spawner.run_all()
    assert len(rig.device.streams) == 1 and rig.device.streams[0].closed
    assert not rig.voice.recording
    assert message("listening", key="Ctrl+Space") not in rig.said


def test_a_click_is_not_speech(rig: Rig) -> None:
    rig.voice.key_down()
    rig.feed(LOUD, 0.2)  # shorter than the 0.3 s that confirms speech
    rig.feed(QUIET, 0.3)  # the pause resets the count
    rig.feed(LOUD, 0.2)
    rig.feed(QUIET, 2.5)  # no silence stop: speech was never confirmed
    assert rig.voice.recording
    rig.voice.key_up()
    assert rig.heard == [] and rig.said[-1] == message("nothing_heard")


# -- reading answers aloud -------------------------------------------------------------------------


def _turn(*texts: str, reason: ExitReason = ExitReason.COMPLETED) -> list[Any]:
    items: list[Any] = [TurnStarted(session_id="s1", seq=1, turn_id="t1", generation=1)]
    for index, text in enumerate(texts):
        if text == "<tool>":
            items.append(
                ToolStarted(session_id="s1", seq=2 + index, turn_id="t1", call_id="c", name="x")
            )
        else:
            items.append(MessageDelta(session_id="s1", seq=2 + index, turn_id="t1", text=text))
    items.append(TurnDone(session_id="s1", seq=99, turn_id="t1", reason=reason))
    return items


def test_read_aloud_speaks_the_final_answer_once(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig(settings=VoiceSettings(read_aloud=True))
    rig.voice.turn_on()
    assert message("on_read_aloud") in rig.said
    for item in _turn(
        "Let me look.", "<tool>", "**Done**: see ", "[the docs](https://x.example).\n"
    ):
        rig.voice.observe(item)
    assert rig.spoken == ["Done: see the docs."]
    assert len(rig.device.played) == 1 and not rig.device.played[0].exists()


def test_read_aloud_is_off_by_default_and_skips_failed_turns(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    rig.voice.turn_on()
    for item in _turn("Hello."):
        rig.voice.observe(item)
    assert rig.spoken == []
    loud = make_rig(settings=VoiceSettings(read_aloud=True))
    loud.voice.turn_on()
    for item in _turn("Partial", reason=ExitReason.INTERRUPTED_BY_USER):
        loud.voice.observe(item)
    loud.voice.turn_off()
    for item in _turn("Hello."):
        loud.voice.observe(item)
    assert loud.spoken == []


def test_read_aloud_failures_are_one_note(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig(settings=VoiceSettings(read_aloud=True), speech_suffix=".mp3")
    rig.voice.turn_on()
    for item in _turn("Hello."):
        rig.voice.observe(item)
    assert rig.device.played == []
    assert rig.said[-1].startswith("The spoken answer is MP3")

    def broken(text: str) -> Path:
        raise RuntimeError("piper is not installed")

    rig.voice.services = VoiceServices(rig.voice.services.transcribe, lambda: None, broken)
    for item in _turn("Hello."):
        rig.voice.observe(item)
    assert rig.said[-1] == message("speak_failed", reason="piper is not installed")


def test_a_new_take_stops_playback(rig: Rig) -> None:
    stop = threading.Event()
    rig.voice._playback = stop  # pyright: ignore[reportPrivateUsage]
    rig.voice.key_down()
    assert stop.is_set()


@pytest.mark.parametrize(
    ("markdown", "spoken"),
    [
        ("# Title\n\n- one\n- two", "Title one two"),
        ("Run `ls` now:\n```sh\nls -la\n```\nDone.", "Run ls now: Done."),
        ("See https://example.com/x for more.", "See for more."),
        ("***very*** _quiet_ ~~gone~~", "very quiet gone"),
        ("| a | b |\n|---|---|\n| 1 | 2 |", "a b 1 2"),
        ("![chart](img.png)", "chart"),
    ],
)
def test_spoken_text(markdown: str, spoken: str) -> None:
    assert spoken_text(markdown) == spoken


# -- settings, keys and messages -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "keys", "label"),
    [
        ("ctrl+space", ("c-space",), "Ctrl+Space"),
        ("Ctrl+B", ("c-b",), "Ctrl+B"),
        ("control-o", ("c-o",), "Ctrl+O"),
        ("alt+v", ("escape", "v"), "Alt+V"),
        ("option+space", ("escape", " "), "Alt+Space"),
        ("F5", ("f5",), "F5"),
    ],
)
def test_record_key_parsing(text: str, keys: tuple[str, ...], label: str) -> None:
    parsed = parse_record_key(text)
    assert parsed is not None and parsed.keys == keys and parsed.label == label


@pytest.mark.parametrize(
    "text", ["ctrl+c", "ctrl+d", "ctrl+j", "ctrl+m", "ctrl+z", "b", "ctrl+alt+b", "super+b", True]
)
def test_unusable_record_keys(text: object) -> None:
    assert parse_record_key(text) is None


def test_a_bad_record_key_falls_back_with_a_note(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig(settings=VoiceSettings(record_key="ctrl+c"))
    assert rig.voice.key.label == "Ctrl+Space"
    rig.voice.turn_on()
    assert rig.said[0] == message("bad_key", value="ctrl+c", key="Ctrl+Space")


def test_settings_read_the_config_and_ignore_odd_values(scope: Scope) -> None:
    good = scope.with_config(
        DictConfig(
            {
                "voice": {
                    "record_key": "alt+r",
                    "silence_threshold": 120,
                    "silence_seconds": 1.5,
                    "read_aloud": True,
                }
            }
        )
    )
    settings = VoiceSettings.from_scope(good)
    assert settings.record_key == "alt+r"
    assert settings.silence_threshold == 120 and settings.silence_seconds == 1.5
    assert settings.read_aloud is True
    odd = scope.with_config(
        DictConfig(
            {
                "voice": {
                    "silence_threshold": True,
                    "silence_seconds": "two",
                    "max_seconds": -4,
                    "read_aloud": "yes",
                }
            }
        )
    )
    assert VoiceSettings.from_scope(odd) == VoiceSettings()
    assert VoiceSettings.from_scope(scope) == VoiceSettings()


def test_level_is_the_rms_of_the_samples() -> None:
    assert level(block(LOUD)) == pytest.approx(LOUD)
    assert level(b"") == 0.0
    capture = Capture(VoiceSettings(), 0.0)
    for at in (0.1, 0.2, 0.3):
        assert capture.add(block(LOUD), at) is None
    assert capture.heard and capture.peak == pytest.approx(LOUD)


def test_resampler_converts_48k_to_16k_across_blocks() -> None:
    converter = Resampler(48_000, 16_000)
    ramp = list(range(0, 9600 * 3, 3))  # 0.2 s at 48 kHz, two 4,800-sample blocks
    out = array("h")
    for start in (0, 4800):
        out.frombytes(converter.feed(array("h", ramp[start : start + 4800]).tobytes()))
    assert len(out) in (3199, 3200)
    assert list(out[:4]) == [0, 9, 18, 27] and out[1600] == 1600 * 9


class FakeLibrary:
    """Stands in for the sounddevice module."""

    def __init__(self, refuse: set[int] | None = None, default_rate: float = 48_000.0) -> None:
        self.refuse = refuse or set()
        self.default_rate = default_rate
        self.opened: list[dict[str, Any]] = []
        self.written: list[bytes] = []

    def query_devices(self, kind: str) -> dict[str, Any]:
        return {"name": "mic", "default_samplerate": self.default_rate}

    def RawInputStream(self, **options: Any) -> Any:  # noqa: N802 - the library's own name
        if options["samplerate"] in self.refuse:
            raise RuntimeError("Invalid sample rate")
        self.opened.append(options)
        return FakeStream()

    def RawOutputStream(self, **options: Any) -> Any:  # noqa: N802 - the library's own name
        library = self

        class Out:
            def __enter__(self) -> Out:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

            def write(self, data: bytes) -> None:
                library.written.append(data)

        return Out()


def test_sound_device_falls_back_to_the_device_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    library = FakeLibrary(refuse={SAMPLE_RATE})
    monkeypatch.setattr(voice_mod.importlib, "import_module", lambda name: library)
    blocks: list[bytes] = []
    SoundDevice().open_input(blocks.append)
    options = library.opened[0]
    assert options["samplerate"] == 48_000 and options["channels"] == 1
    assert options["dtype"] == "int16" and options["blocksize"] == 4800
    options["callback"](array("h", [100] * 4800).tobytes(), 4800, None, None)
    assert len(blocks[0]) // 2 == 1600


def test_sound_device_records_at_16khz_when_it_can(monkeypatch: pytest.MonkeyPatch) -> None:
    library = FakeLibrary()
    monkeypatch.setattr(voice_mod.importlib, "import_module", lambda name: library)
    blocks: list[bytes] = []
    SoundDevice().open_input(blocks.append)
    assert library.opened[0]["samplerate"] == SAMPLE_RATE
    library.opened[0]["callback"](block(LOUD), BLOCK, None, None)
    assert blocks == [block(LOUD)]
    assert SoundDevice().problem() is None


def test_sound_device_plays_a_wav_until_stopped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    library = FakeLibrary()
    monkeypatch.setattr(voice_mod.importlib, "import_module", lambda name: library)
    path = tmp_path / "answer.wav"
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(22_050)
        writer.writeframes(array("h", [7] * 22_050).tobytes())
    SoundDevice().play_wav(path, threading.Event())
    assert sum(len(chunk) for chunk in library.written) == 22_050 * 2
    library.written.clear()
    stopped = threading.Event()
    stopped.set()
    SoundDevice().play_wav(path, stopped)
    assert library.written == []


def test_messages_match_the_english_catalogue() -> None:
    catalogue = yamlio.load_safe(ROOT / "locales" / "en.yaml")
    assert catalogue["cli"]["voice"] == MESSAGES


# -- /voice and --voice ----------------------------------------------------------------------------


@pytest.fixture
def ctx(scope: Scope) -> Iterator[tuple[SlashContext, list[str]]]:
    lines: list[str] = []

    class NoPort:
        pass

    yield SlashContext(scope=scope, port=NoPort(), session_id="s1", out=lines.append), lines  # pyright: ignore[reportArgumentType]


def test_voice_command_on_off_status(
    ctx: tuple[SlashContext, list[str]], make_rig: Callable[..., Rig]
) -> None:
    context, lines = ctx
    rig = make_rig()
    context.voice = rig.voice
    assert dispatch("/voice on", context) == SlashResult()
    assert rig.voice.enabled and rig.said[-1].startswith("Voice mode on")
    cmd_voice(context, "")
    assert lines[:4] == [
        "Voice mode: on",
        "Record key: Ctrl+Space",
        "Read answers aloud: off",
        "Now: idle",
    ]
    rig.voice.key_down()
    lines.clear()
    cmd_voice(context, "status")
    assert lines[3] == "Now: recording"
    cmd_voice(context, "OFF")
    assert not rig.voice.enabled and rig.said[-1] == message("now_off")
    lines.clear()
    cmd_voice(context, "loud")
    assert lines == [message("usage")]


def test_voice_command_without_voice_mode(ctx: tuple[SlashContext, list[str]]) -> None:
    context, lines = ctx
    cmd_voice(context, "on")
    assert lines == [message("not_here")]


def test_chat_has_a_voice_flag(scope: Scope, monkeypatch: pytest.MonkeyPatch) -> None:
    from indus.cli import app as app_mod
    from indus.cli import chat as chat_mod
    from indus.cli.args import ChatArgs, CliState, GlobalArgs

    seen: list[ChatArgs] = []

    def fake_run_chat(state: CliState, args: GlobalArgs, chat: ChatArgs) -> int:
        seen.append(chat)
        return 0

    monkeypatch.setattr(chat_mod, "run_chat", fake_run_chat)
    assert app_mod.dispatch(["chat", "--voice"], CliState(scope=scope)) == 0
    assert app_mod.dispatch(["chat"], CliState(scope=scope)) == 0
    assert [chat.voice for chat in seen] == [True, False]


def test_launch_turns_voice_on_for_the_voice_flag(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    from indus.cli import launch as launch_mod

    calls: list[dict[str, Any]] = []

    def fake_attach(repl: Any, ctx: Any, services: Any, **kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(voice_mod, "attach_voice", fake_attach)
    monkeypatch.setattr(voice_mod, "host_services", lambda host, scope: None)

    class Launch:
        def __init__(self, voice: bool) -> None:
            self.voice = voice
            self.host = None
            self.scope = scope

    launch_mod.attach_voice_mode(Launch(True), None, None)  # pyright: ignore[reportArgumentType]
    launch_mod.attach_voice_mode(Launch(False), None, None)  # pyright: ignore[reportArgumentType]
    assert calls == [{"turn_on": True}, {"turn_on": False}]


# -- through the real REPL -------------------------------------------------------------------------


def test_record_key_in_the_repl_echoes_and_submits(
    scope: Scope, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prompt_toolkit.input import create_pipe_input

    from tests.unit.cli.repl.conftest import Rig as ReplRig

    device = FakeDevice()
    services = VoiceServices(
        lambda path: Transcript("open the pod bay doors", "en", 1.0),
        lambda: None,
        lambda text: Path("unused.wav"),
    )
    holder: dict[str, VoiceMode] = {}

    def setup(repl: Any) -> None:
        context = SlashContext(scope=scope, port=None, session_id="s1", out=repl.print)  # pyright: ignore[reportArgumentType]
        made = voice_mod.attach_voice(repl, context, services, device=device, turn_on=True)
        made.spawn = lambda fn: fn()
        holder["voice"] = made

    with create_pipe_input() as pipe:
        rig = ReplRig(scope, fake_clock, pipe, setup=setup).start()
        try:
            voice = holder["voice"]
            rig.settle()
            assert voice.enabled
            rig.keys("\x00")  # Ctrl+Space
            rig.settle()
            assert voice.recording
            for _ in range(5):
                fake_clock.advance(0.1)
                assert device.on_block is not None
                device.on_block(block(LOUD))
            fake_clock.advance(1.0)
            rig.keys("\x00")  # the second tap ends the take
            rig.settle()
            rig.settle()
            submitted = rig.host.wait_for("submit")
            assert submitted[0].args[1] == "open the pod bay doors"
            assert "› open the pod bay doors" in rig.text()

            request = ApprovalRequest(
                request_id="r1", session_id=SESSION, command="rm -rf build", description="delete"
            )
            rig.feed(request)
            fake_clock.advance(1.0)
            rig.keys("\x00")
            rig.settle()
            assert not voice.recording
            assert message("card_open") in rig.text()
        finally:
            rig.stop()
