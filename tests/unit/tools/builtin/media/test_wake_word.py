"""P24-20: wake-word detection and consent with a fake engine, fake audio and a spied microphone."""

from __future__ import annotations

import struct
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from indus.tools.builtin.media import wake_word
from indus.tools.builtin.media.wake_word import (
    FRAME_LENGTH,
    REARM_FRAMES,
    WakeDetector,
    WakeError,
    WakeService,
    WakeSettings,
    WakeUnavailable,
    check_requirements,
    split_frames,
)

LOUD = 0.9
QUIET = 0.0


def frame(score: float) -> bytes:
    """A frame whose first sample carries the score the fake engine reads back."""
    first = struct.pack("<h", int(score * 1000))
    return first + b"\x00" * (FRAME_LENGTH * 2 - 2)


def utterance(loud: int = 4, quiet: int = REARM_FRAMES) -> list[bytes]:
    return [frame(LOUD)] * loud + [frame(QUIET)] * quiet


class FakeEngine:
    name = "fake"
    frame_length = FRAME_LENGTH

    def score(self, data: bytes) -> float:
        return struct.unpack("<h", data[:2])[0] / 1000


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@dataclass
class FakeStream:
    frames: list[bytes]
    closed: bool = False
    gate: threading.Event | None = None

    def read(self) -> bytes | None:
        if self.gate is not None:
            self.gate.wait(2)
        if self.closed or not self.frames:
            return None
        return self.frames.pop(0)

    def close(self) -> None:
        self.closed = True


@dataclass
class SpyMicrophone:
    script: list[list[bytes]] = field(default_factory=list[list[bytes]])
    opened: list[FakeStream] = field(default_factory=list[FakeStream])

    def open(self, sample_rate: int, frame_length: int) -> FakeStream:
        assert sample_rate == 16_000 and frame_length == FRAME_LENGTH
        stream = FakeStream(self.script.pop(0) if self.script else [])
        self.opened.append(stream)
        return stream


ON = WakeSettings(enabled=True, consent=True, model="model.onnx")


def detector(settings: WakeSettings = ON, clock: Clock | None = None) -> WakeDetector:
    return WakeDetector(FakeEngine(), settings, clock or Clock())


def fires(d: WakeDetector, frames: Iterable[bytes]) -> int:
    return sum(1 for f in frames if d.feed(f))


# -- the detector ---------------------------------------------------------------------------------


def test_one_detection_per_utterance() -> None:
    clock = Clock()
    d = detector(clock=clock)
    assert fires(d, utterance(loud=12)) == 1  # a long phrase still fires once
    clock.now += 5
    assert fires(d, utterance()) == 1
    clock.now += 5
    assert fires(d, utterance() + utterance()) == 1  # the second falls inside the cooldown
    clock.now += 5
    assert fires(d, [frame(QUIET)] * 3) == 0


def test_confirmation_frames_reject_a_stray_spike() -> None:
    d = detector()
    assert fires(d, [frame(LOUD), frame(QUIET), frame(LOUD), frame(LOUD), frame(QUIET)]) == 0
    assert fires(d, [frame(LOUD)] * 3) == 1
    single = detector(WakeSettings(enabled=True, consent=True, confirm_frames=1))
    assert fires(single, [frame(LOUD)]) == 1


def test_cooldown_and_re_arm() -> None:
    clock = Clock()
    d = detector(clock=clock)
    assert fires(d, utterance()) == 1
    clock.now += 0.5  # within the 2 s cooldown
    assert fires(d, utterance()) == 0
    clock.now += 2.0
    # still loud from before: no re-arm until enough quiet frames
    assert fires(d, [frame(LOUD)] * 3) == 1
    assert fires(d, [frame(LOUD)] * 10) == 0
    clock.now += 3
    assert fires(d, [frame(QUIET)] * (REARM_FRAMES - 1) + [frame(LOUD)] * 3) == 0
    assert fires(d, [frame(QUIET)] * REARM_FRAMES + [frame(LOUD)] * 3) == 1


def test_split_frames_pads_the_last() -> None:
    frames = split_frames(b"\x01\x00" * (FRAME_LENGTH + 10), FRAME_LENGTH)
    assert [len(f) for f in frames] == [FRAME_LENGTH * 2, FRAME_LENGTH * 2]
    assert frames[1][20:] == b"\x00" * (FRAME_LENGTH * 2 - 20)
    assert split_frames(b"", FRAME_LENGTH) == []


def test_settings_clamp_and_defaults() -> None:
    s = WakeSettings.from_mapping({"threshold": 7, "confirm_frames": 0, "engine": "porcupine"})
    assert s.threshold == 0.99 and s.confirm_frames == 1 and s.engine == "sherpa"  # D-16.800
    assert WakeSettings.from_mapping({"engine": "openwakeword"}).engine == "openwakeword"
    assert s.phrase == "hey indus" and not s.enabled and not s.consent


def test_requirements_name_what_is_missing(tmp_path: Path) -> None:
    absent = WakeSettings(engine="sherpa")
    ok, hint = check_requirements(absent, find_module=lambda name: False)
    assert not ok and "wake-sherpa" in hint
    ok, hint = check_requirements(WakeSettings(), find_module=lambda name: True)
    assert not ok and "wake_word.model" in hint and "hey indus" in hint
    ok, hint = check_requirements(
        WakeSettings(model=str(tmp_path / "x.onnx")), find_module=lambda name: True
    )
    assert not ok and "does not exist" in hint
    model = tmp_path / "hey_indus.onnx"
    model.write_bytes(b"model")
    assert check_requirements(WakeSettings(model=str(model)), find_module=lambda n: True) == (
        True,
        "",
    )


def test_no_model_ships_with_the_package() -> None:
    package = Path(wake_word.__file__).parent
    assert not [p for p in package.rglob("*") if p.suffix in {".onnx", ".tflite", ".ppn"}]


# -- the service ----------------------------------------------------------------------------------


class Harness:
    def __init__(
        self, settings: WakeSettings = ON, script: list[list[bytes]] | None = None
    ) -> None:
        self.settings = settings
        self.mic = SpyMicrophone(script=list(script or []))
        self.detected: list[tuple[object, str]] = []
        self.done = threading.Event()
        self.service = WakeService(
            lambda: self.settings,
            on_detect=self._on,
            microphone=self.mic,
            engine_factory=lambda s: FakeEngine(),
            clock=Clock(),
        )

    def _on(self, owner: object, phrase: str) -> None:
        self.detected.append((owner, phrase))
        self.done.set()


def test_disabled_never_opens_the_microphone() -> None:
    h = Harness(WakeSettings(enabled=False, consent=True))
    outcome = h.service.start("tui", "tui")
    assert not outcome.started and outcome.reason == "disabled"
    assert h.mic.opened == []
    assert not h.service.listening()


def test_no_consent_never_opens_the_microphone() -> None:
    h = Harness(WakeSettings(enabled=True, consent=False))
    outcome = h.service.start("gui", "gui")
    assert outcome.reason == "consent_required" and h.mic.opened == []


def test_unavailable_engine_opens_nothing() -> None:
    h = Harness()

    def broken(settings: WakeSettings) -> FakeEngine:
        raise WakeUnavailable("The openwakeword engine is not installed")

    h.service = WakeService(lambda: ON, on_detect=h._on, microphone=h.mic, engine_factory=broken)  # pyright: ignore[reportPrivateUsage]
    outcome = h.service.start("gui", "gui")
    assert outcome.reason == "unavailable" and "not installed" in outcome.hint
    assert h.mic.opened == []


def test_detection_hands_off_once_and_releases_the_microphone() -> None:
    owner = object()
    h = Harness(script=[[frame(QUIET)] * 3 + utterance() + utterance()])
    outcome = h.service.start(owner, "gui")
    assert outcome.started and outcome.phrase == "hey indus" and outcome.capture == "local"
    assert h.done.wait(2)
    h.service.wait_idle()
    assert h.detected == [(owner, "hey indus")]
    assert h.mic.opened[0].closed  # paused for the surface's own capture
    assert not h.service.listening(owner)
    # the surface finished its voice turn: listening again on a fresh stream
    assert h.service.resume(owner)
    assert len(h.mic.opened) == 2 and h.service.listening(owner)
    assert h.service.stop(owner)
    assert h.mic.opened[1].closed


def test_owner_lease_refusals() -> None:
    h = Harness(script=[[frame(QUIET)] * 2])
    a, b = object(), object()
    assert h.service.start(a, "gui").started
    again = h.service.start(a, "gui")
    assert again.started and len(h.mic.opened) == 1  # idempotent
    assert h.service.start(b, "tui").reason == "owned"
    assert not h.service.stop(b) and not h.service.pause(b) and not h.service.resume(b)
    assert h.service.owner_surface() == "gui"
    assert h.service.release_owner(a)
    assert h.service.owner() is None


def test_pause_releases_and_resume_reopens() -> None:
    owner = object()
    h = Harness(script=[[], []])
    h.service.start(owner, "tui")
    assert h.service.pause(owner) and h.service.pause(owner)  # twice is fine
    assert h.mic.opened[0].closed and not h.service.listening(owner)
    h.settings = WakeSettings(enabled=False, consent=True)
    assert not h.service.resume(owner)  # turned off meanwhile: stays closed
    assert len(h.mic.opened) == 1
    h.settings = ON
    assert h.service.resume(owner) and len(h.mic.opened) == 2


def test_client_capture_feeds_without_a_microphone() -> None:
    owner = object()
    h = Harness()
    outcome = h.service.start(owner, "gui", client_capture=True)
    assert outcome.started and outcome.capture == "client"
    assert h.mic.opened == []
    pcm = b"".join(utterance())
    assert h.service.feed(owner, pcm)
    assert h.detected == [(owner, "hey indus")]
    assert not h.service.feed(owner, pcm)  # paused after the detection
    assert not h.service.feed(object(), pcm)
    with pytest.raises(WakeError, match="64000"):
        h.service.feed(owner, b"\x00" * 64_002)
