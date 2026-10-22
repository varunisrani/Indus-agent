"""Wake word (P24-20): listen for "hey indus" and hand off to voice mode.

Pieces, smallest first:

- ``WakeEngine``: scores one audio frame (16 kHz mono int16 little-endian, ``frame_length``
  samples) from 0 to 1. ``SherpaKeywordEngine`` (the default: the ``wake-sherpa`` extra, D-16.800)
  and ``OpenWakeWordEngine`` (chosen with ``wake_word.engine: openwakeword`` where that package
  installs) are thin adapters over optional packages, imported only when built; no wake model
  ships with Indus — the person trains one with the engine's own tools or supplies one
  (``wake_word.model``).
- ``WakeDetector``: turns scores into detections, one per utterance: ``confirm_frames`` frames in
  a row at or over the threshold fire once; it then stays quiet until ``REARM_FRAMES`` frames fall
  below the threshold again, and never fires twice within ``cooldown_s``.
- ``WakeService``: the one listener of a process. It opens the microphone only when the person
  enabled the feature *and* gave consent (``wake_word.enabled`` and ``wake_word.consent``, both
  written only by an explicit "on"), only for the surface that owns the lease, and only while it
  is started and not paused. On a detection it pauses itself (the surface's own voice capture
  needs the microphone) and calls ``on_detect(owner, phrase)``; the RPC layer turns that into a
  ``wake.detected`` event (``indus.service.wake_rpc``). With client capture the surface sends the
  audio (``feed``) and no microphone is opened here at all.
"""

from __future__ import annotations

import importlib
import logging
import math
import struct
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from indus.protocol.wake import MAX_FEED_BYTES, SAMPLE_RATE

__all__ = [
    "CONFIRM_FRAMES",
    "COOLDOWN_S",
    "DEFAULT_ENGINE",
    "DEFAULT_PHRASE",
    "FRAME_LENGTH",
    "MAX_FEED_BYTES",
    "REARM_FRAMES",
    "SAMPLE_RATE",
    "AudioStream",
    "Microphone",
    "OpenWakeWordEngine",
    "SherpaKeywordEngine",
    "SoundDeviceMicrophone",
    "StartOutcome",
    "WakeDetector",
    "WakeEngine",
    "WakeError",
    "WakeService",
    "WakeSettings",
    "WakeUnavailable",
    "build_engine",
    "check_requirements",
    "split_frames",
]

log = logging.getLogger(__name__)

FRAME_LENGTH = 1_280
"""80 ms at 16 kHz: what both engines expect."""
DEFAULT_PHRASE = "hey indus"
CONFIRM_FRAMES = 3
REARM_FRAMES = 5
COOLDOWN_S = 2.0

type EngineName = Literal["openwakeword", "sherpa"]
ENGINES: tuple[EngineName, ...] = ("openwakeword", "sherpa")
DEFAULT_ENGINE: EngineName = "sherpa"
"""sherpa-onnx ships as the ``wake-sherpa`` extra; openWakeWord cannot be locked on Python 3.13
(D-16.800), so it is the one a person opts into."""

_PACKAGES: Mapping[str, tuple[str, str]] = {
    "openwakeword": ("openwakeword", "pip install openwakeword (or uv add openwakeword)"),
    "sherpa": ("sherpa_onnx", "uv sync --extra wake-sherpa"),
}


class WakeError(Exception):
    """A wake-word action that cannot be done; the message is meant for the person."""


class WakeUnavailable(WakeError):
    """The engine or its model is missing here; the message says what to install or set."""


# -- settings -------------------------------------------------------------------------------------


def _clamp(value: object, lo: float, hi: float, fallback: float) -> float:
    try:
        number = float(cast(Any, value))
    except (TypeError, ValueError):
        return fallback
    if math.isnan(number):
        return fallback
    return max(lo, min(hi, number))


@dataclass(frozen=True)
class WakeSettings:
    enabled: bool = False
    consent: bool = False
    engine: EngineName = DEFAULT_ENGINE
    phrase: str = DEFAULT_PHRASE
    model: str = ""
    threshold: float = 0.5
    confirm_frames: int = CONFIRM_FRAMES
    cooldown_s: float = COOLDOWN_S

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> WakeSettings:
        """The ``wake_word`` section, with out-of-range numbers clamped."""
        engine = str(raw.get("engine") or DEFAULT_ENGINE).strip().lower()
        return cls(
            enabled=raw.get("enabled") is True,
            consent=raw.get("consent") is True,
            engine=cast(EngineName, engine if engine in ENGINES else DEFAULT_ENGINE),
            phrase=" ".join(str(raw.get("phrase") or DEFAULT_PHRASE).split()) or DEFAULT_PHRASE,
            model=str(raw.get("model") or "").strip(),
            threshold=_clamp(raw.get("threshold"), 0.05, 0.99, 0.5),
            confirm_frames=int(_clamp(raw.get("confirm_frames"), 1, 10, CONFIRM_FRAMES)),
            cooldown_s=_clamp(raw.get("cooldown_s"), 0.0, 60.0, COOLDOWN_S),
        )


# -- engines --------------------------------------------------------------------------------------


class WakeEngine(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def frame_length(self) -> int: ...

    def score(self, frame: bytes, /) -> float:
        """How sure the engine is that ``frame`` ends the phrase, 0..1."""
        ...


def _samples(frame: bytes) -> list[int]:
    count = len(frame) // 2
    return list(struct.unpack(f"<{count}h", frame[: count * 2]))


class OpenWakeWordEngine:
    """openWakeWord with the person's own model file (``.onnx`` or ``.tflite``)."""

    name = "openwakeword"
    frame_length = FRAME_LENGTH

    def __init__(self, model_path: Path) -> None:
        module: Any = importlib.import_module("openwakeword.model")
        framework = "onnx" if model_path.suffix.lower() == ".onnx" else "tflite"
        self._model: Any = module.Model(
            wakeword_models=[str(model_path)], inference_framework=framework
        )
        self._np: Any = importlib.import_module("numpy")

    def score(self, frame: bytes) -> float:
        audio = self._np.frombuffer(frame, dtype=self._np.int16)
        scores = cast(Mapping[str, float], self._model.predict(audio))
        return max((float(v) for v in scores.values()), default=0.0)


class SherpaKeywordEngine:
    """sherpa-onnx keyword spotting with a model folder and a keywords file the person supplies.

    The folder holds ``tokens.txt``, ``encoder*.onnx``, ``decoder*.onnx``, ``joiner*.onnx`` and
    ``keywords.txt`` (the phrase in the model's token units). A spotted keyword scores 1.
    """

    name = "sherpa"
    frame_length = FRAME_LENGTH

    def __init__(self, model_dir: Path) -> None:
        sherpa: Any = importlib.import_module("sherpa_onnx")

        def one(pattern: str) -> str:
            found = sorted(model_dir.glob(pattern))
            if not found:
                raise WakeUnavailable(f"the sherpa model folder has no {pattern}")
            return str(found[0])

        self._spotter: Any = sherpa.KeywordSpotter(
            tokens=one("tokens.txt"),
            encoder=one("encoder*.onnx"),
            decoder=one("decoder*.onnx"),
            joiner=one("joiner*.onnx"),
            keywords_file=one("keywords.txt"),
            num_threads=1,
        )
        self._stream: Any = self._spotter.create_stream()

    def score(self, frame: bytes) -> float:
        samples = [s / 32768.0 for s in _samples(frame)]
        self._stream.accept_waveform(SAMPLE_RATE, samples)
        hit = False
        while self._spotter.is_ready(self._stream):
            self._spotter.decode_stream(self._stream)
            if self._spotter.get_result(self._stream):
                hit = True
                self._spotter.reset_stream(self._stream)
        return 1.0 if hit else 0.0


def check_requirements(
    settings: WakeSettings, *, find_module: Callable[[str], bool] | None = None
) -> tuple[bool, str]:
    """``(available, hint)`` without opening anything: package present, model set and found."""
    module, install = _PACKAGES[settings.engine]
    finder = find_module or _module_present
    if not finder(module):
        return False, f"The {settings.engine} engine is not installed: {install}."
    if not settings.model:
        return False, (
            "No wake model is set: train or download a model for the phrase "
            f"“{settings.phrase}” and set wake_word.model to its path."
        )
    if not Path(settings.model).expanduser().exists():
        return False, f"The wake model {settings.model} does not exist."
    return True, ""


def _module_present(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def build_engine(settings: WakeSettings) -> WakeEngine:
    available, hint = check_requirements(settings)
    if not available:
        raise WakeUnavailable(hint)
    path = Path(settings.model).expanduser()
    try:
        if settings.engine == "sherpa":
            return SherpaKeywordEngine(path)
        return OpenWakeWordEngine(path)
    except WakeUnavailable:
        raise
    except Exception as exc:
        raise WakeUnavailable(
            f"The {settings.engine} engine could not load the model: {exc}"
        ) from None


# -- detection ------------------------------------------------------------------------------------


def split_frames(pcm: bytes, frame_length: int) -> list[bytes]:
    """``pcm`` cut into frames of ``frame_length`` samples; the last one zero-padded."""
    size = frame_length * 2
    if size <= 0:
        return []
    usable = len(pcm) - (len(pcm) % 2)
    frames = [pcm[i : i + size] for i in range(0, usable, size)]
    if frames and len(frames[-1]) < size:
        frames[-1] = frames[-1] + b"\x00" * (size - len(frames[-1]))
    return frames


class WakeDetector:
    """Scores in, one detection per utterance out (see the module notes)."""

    def __init__(
        self,
        engine: WakeEngine,
        settings: WakeSettings,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.engine = engine
        self.threshold = settings.threshold
        self.confirm = settings.confirm_frames
        self.cooldown = settings.cooldown_s
        self._clock = clock
        self._streak = 0
        self._quiet = REARM_FRAMES
        self._armed = True
        self._last_fire: float | None = None

    def feed(self, frame: bytes) -> bool:
        """Score one frame; True when this frame is the detection."""
        try:
            score = self.engine.score(frame)
        except Exception:
            log.warning("wake word: the engine failed on a frame", exc_info=True)
            return False
        if score >= self.threshold:
            self._streak += 1
            self._quiet = 0
        else:
            self._streak = 0
            self._quiet += 1
            if self._quiet >= REARM_FRAMES:
                self._armed = True
        if not self._armed or self._streak < self.confirm:
            return False
        now = self._clock()
        if self._last_fire is not None and now - self._last_fire < self.cooldown:
            return False
        self._armed = False
        self._streak = 0
        self._last_fire = now
        return True

    def reset(self) -> None:
        self._streak = 0
        self._quiet = REARM_FRAMES
        self._armed = True


# -- the microphone -------------------------------------------------------------------------------


class AudioStream(Protocol):
    def read(self) -> bytes | None:
        """One frame, or None when the stream ended."""
        ...

    def close(self) -> None: ...


class Microphone(Protocol):
    def open(self, sample_rate: int, frame_length: int) -> AudioStream: ...


class _SoundDeviceStream:
    def __init__(self, stream: Any, frame_length: int) -> None:
        self._stream = stream
        self._frame_length = frame_length
        self._closed = False

    def read(self) -> bytes | None:
        if self._closed:
            return None
        try:
            data, _overflow = self._stream.read(self._frame_length)
        except Exception:
            return None
        return bytes(data)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            log.debug("wake word: closing the input stream failed", exc_info=True)


class SoundDeviceMicrophone:
    """The default input device through ``sounddevice`` (the ``voice`` extra)."""

    def open(self, sample_rate: int, frame_length: int) -> AudioStream:
        try:
            sd: Any = importlib.import_module("sounddevice")
        except ImportError:
            raise WakeUnavailable("No audio input here: uv sync --extra voice.") from None
        stream: Any = sd.RawInputStream(
            samplerate=sample_rate, channels=1, dtype="int16", blocksize=frame_length
        )
        stream.start()
        return _SoundDeviceStream(stream, frame_length)


# -- the service ----------------------------------------------------------------------------------

type StartReason = Literal["disabled", "consent_required", "unavailable", "owned"]


@dataclass(frozen=True)
class StartOutcome:
    started: bool
    reason: StartReason | None = None
    hint: str = ""
    phrase: str = DEFAULT_PHRASE
    engine: str = ""
    capture: Literal["local", "client"] = "local"


@dataclass
class _Lease:
    owner: object
    surface: str
    detector: WakeDetector
    phrase: str
    client_capture: bool
    paused: bool = False
    stream: AudioStream | None = None
    reader: threading.Thread | None = None
    stop: threading.Event = field(default_factory=threading.Event)


class WakeService:
    """The process's one wake-word listener (see the module notes)."""

    def __init__(
        self,
        settings: Callable[[], WakeSettings],
        *,
        on_detect: Callable[[object, str], None],
        microphone: Microphone | None = None,
        engine_factory: Callable[[WakeSettings], WakeEngine] = build_engine,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._on_detect = on_detect
        self._microphone: Microphone = microphone or SoundDeviceMicrophone()
        self._engine_factory = engine_factory
        self._clock = clock
        self._lock = threading.RLock()
        self._lease: _Lease | None = None

    # -- lease ----------------------------------------------------------------------------------

    def owner(self) -> object | None:
        with self._lock:
            return self._lease.owner if self._lease is not None else None

    def owner_surface(self) -> str | None:
        with self._lock:
            return self._lease.surface if self._lease is not None else None

    def start(self, owner: object, surface: str, *, client_capture: bool = False) -> StartOutcome:
        """Arm the listener for ``owner``; the microphone opens only when every rule allows it."""
        settings = self._settings()
        if not settings.enabled:
            return StartOutcome(False, "disabled", phrase=settings.phrase)
        if not settings.consent:
            return StartOutcome(
                False,
                "consent_required",
                "Turn the wake word on from Settings or /wake on to agree to listening.",
                phrase=settings.phrase,
            )
        with self._lock:
            current = self._lease
            if current is not None and current.owner is not owner:
                return StartOutcome(False, "owned", phrase=settings.phrase)
            if current is not None:
                return self._outcome(current, settings)
            try:
                engine = self._engine_factory(settings)
            except WakeUnavailable as exc:
                return StartOutcome(False, "unavailable", str(exc), phrase=settings.phrase)
            lease = _Lease(
                owner=owner,
                surface=surface,
                detector=WakeDetector(engine, settings, self._clock),
                phrase=settings.phrase,
                client_capture=client_capture,
            )
            if not client_capture:
                try:
                    self._open(lease)
                except WakeUnavailable as exc:
                    return StartOutcome(False, "unavailable", str(exc), phrase=settings.phrase)
            self._lease = lease
            return self._outcome(lease, settings)

    def _outcome(self, lease: _Lease, settings: WakeSettings) -> StartOutcome:
        return StartOutcome(
            True,
            phrase=lease.phrase,
            engine=lease.detector.engine.name,
            capture="client" if lease.client_capture else "local",
        )

    def stop(self, owner: object) -> bool:
        with self._lock:
            lease = self._lease
            if lease is None or lease.owner is not owner:
                return False
            self._lease = None
        self._close(lease)
        return True

    def release_owner(self, owner: object) -> bool:
        """The owner went away (its connection closed)."""
        return self.stop(owner)

    def pause(self, owner: object) -> bool:
        """Release the microphone (the surface records now); the lease stays."""
        with self._lock:
            lease = self._lease
            if lease is None or lease.owner is not owner:
                return False
            if lease.paused:
                return True
            lease.paused = True
        self._close(lease)
        return True

    def resume(self, owner: object) -> bool:
        with self._lock:
            lease = self._lease
            if lease is None or lease.owner is not owner:
                return False
            if not lease.paused:
                return True
            settings = self._settings()
            if not (settings.enabled and settings.consent):
                return False
            lease.paused = False
            lease.detector.reset()
            if not lease.client_capture:
                try:
                    self._open(lease)
                except WakeUnavailable:
                    lease.paused = True
                    return False
        return True

    def listening(self, owner: object | None = None) -> bool:
        with self._lock:
            lease = self._lease
            if lease is None or lease.paused:
                return False
            return owner is None or lease.owner is owner

    def feed(self, owner: object, pcm: bytes) -> bool:
        """Client-captured audio for an armed client-capture lease; True when it was used."""
        if len(pcm) > MAX_FEED_BYTES:
            raise WakeError(f"one feed carries at most {MAX_FEED_BYTES} bytes")
        with self._lock:
            lease = self._lease
            if (
                lease is None
                or lease.owner is not owner
                or lease.paused
                or not lease.client_capture
            ):
                return False
        for frame in split_frames(pcm, lease.detector.engine.frame_length):
            if self._frame(lease, frame):
                break
        return True

    # -- audio ----------------------------------------------------------------------------------

    def _open(self, lease: _Lease) -> None:
        stream = self._microphone.open(SAMPLE_RATE, lease.detector.engine.frame_length)
        lease.stop = threading.Event()
        lease.stream = stream
        reader = threading.Thread(
            target=self._read_loop, args=(lease, stream, lease.stop), daemon=True, name="wake-word"
        )
        lease.reader = reader
        reader.start()

    def _close(self, lease: _Lease) -> None:
        lease.stop.set()
        stream, lease.stream = lease.stream, None
        if stream is not None:
            stream.close()
        reader, lease.reader = lease.reader, None
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=2.0)

    def _read_loop(self, lease: _Lease, stream: AudioStream, stop: threading.Event) -> None:
        while not stop.is_set():
            frame = stream.read()
            if frame is None:
                return
            if self._frame(lease, frame):
                return

    def _frame(self, lease: _Lease, frame: bytes) -> bool:
        if not lease.detector.feed(frame):
            return False
        with self._lock:
            if self._lease is not lease or lease.paused:
                return False
            lease.paused = True
        lease.stop.set()
        stream, lease.stream = lease.stream, None
        if stream is not None:
            stream.close()
        try:
            self._on_detect(lease.owner, lease.phrase)
        except Exception:
            log.warning("wake word: the detection hand-off failed", exc_info=True)
        return True

    def wait_idle(self, timeout: float = 2.0) -> None:
        """Join the reader thread (tests)."""
        with self._lock:
            reader = self._lease.reader if self._lease is not None else None
        if reader is not None:
            reader.join(timeout)
