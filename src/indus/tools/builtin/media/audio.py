"""Audio helpers shared by speech output and transcription: WAV facts, joining, silences, cuts,
and running ``ffmpeg``.

WAV work uses the standard ``wave`` module on PCM data. Silence finding scans 8- and 16-bit PCM
in short windows and reports the middle of every quiet run that lasts long enough; other sample
widths report no silences (callers then cut at the ideal points). ``ffmpeg`` is looked up on the
profile's ``PATH`` and run with the minimal child environment, never a shell.
"""

from __future__ import annotations

import io
import math
import subprocess
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path

from indus.foundation.scope import Scope
from indus.platform.locate import which
from indus.safety.child_env import child_env

__all__ = [
    "MAX_LOOKBACK_S",
    "WavFacts",
    "cut_points",
    "find_ffmpeg",
    "find_program",
    "is_wav",
    "join_wav",
    "run_tool",
    "silences",
    "split_wav",
    "to_opus",
    "wav_facts",
]

MAX_LOOKBACK_S = 60.0
WINDOW_S = 0.05
QUIET_DBFS = -35.0
MIN_QUIET_S = 0.4
FFMPEG_TIMEOUT_S = 300.0


@dataclass(frozen=True)
class WavFacts:
    channels: int
    sample_width: int
    rate: int
    frames: int

    @property
    def duration(self) -> float:
        return self.frames / self.rate if self.rate else 0.0


def is_wav(data: bytes) -> bool:
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"


def wav_facts(source: Path | bytes) -> WavFacts:
    with wave.open(
        io.BytesIO(source) if isinstance(source, bytes) else str(source), "rb"
    ) as handle:
        return WavFacts(
            handle.getnchannels(), handle.getsampwidth(), handle.getframerate(), handle.getnframes()
        )


def join_wav(pieces: list[bytes]) -> bytes:
    """One WAV from several with the same format (``wave.Error`` when they differ)."""
    out = io.BytesIO()
    params = None
    writer: wave.Wave_write | None = None
    try:
        for piece in pieces:
            with wave.open(io.BytesIO(piece), "rb") as reader:
                current = (reader.getnchannels(), reader.getsampwidth(), reader.getframerate())
                if writer is None:
                    params = current
                    writer = wave.open(out, "wb")  # noqa: SIM115 - closed in finally
                    writer.setnchannels(current[0])
                    writer.setsampwidth(current[1])
                    writer.setframerate(current[2])
                elif current != params:
                    raise wave.Error("the audio pieces have different formats")
                writer.writeframes(reader.readframes(reader.getnframes()))
    finally:
        if writer is not None:
            writer.close()
    return out.getvalue()


def _levels(frames: bytes, width: int, channels: int, per_window: int) -> list[float]:
    """RMS per window as a share of full scale (0..1), over all channels."""
    if width == 2:
        samples = array("h")
        samples.frombytes(frames[: len(frames) - len(frames) % 2])
        full = 32768.0
        values = samples
    elif width == 1:
        values = array("h", (b - 128 for b in frames))
        full = 128.0
    else:
        return []
    step = per_window * channels
    out: list[float] = []
    for start in range(0, len(values), step):
        window = values[start : start + step]
        if not window:
            break
        energy = sum(v * v for v in window) / len(window)
        out.append(math.sqrt(energy) / full)
    return out


def silences(
    path: Path, *, quiet_dbfs: float = QUIET_DBFS, min_quiet_s: float = MIN_QUIET_S
) -> list[float]:
    """Middle times (seconds) of the quiet runs in a PCM WAV, in order."""
    facts = wav_facts(path)
    per_window = max(1, int(facts.rate * WINDOW_S))
    with wave.open(str(path), "rb") as reader:
        frames = reader.readframes(facts.frames)
    levels = _levels(frames, facts.sample_width, facts.channels, per_window)
    threshold = 10 ** (quiet_dbfs / 20)
    window_s = per_window / facts.rate
    found: list[float] = []
    start: int | None = None
    for index, level in enumerate([*levels, 1.0]):  # the sentinel closes a trailing run
        if level < threshold:
            if start is None:
                start = index
            continue
        if start is not None and (index - start) * window_s >= min_quiet_s:
            found.append((start + index) * window_s / 2)
        start = None
    return found


def cut_points(
    duration: float, target: float, quiet: list[float], *, max_lookback: float = MAX_LOOKBACK_S
) -> list[float]:
    """Times to cut at so no piece is longer than ``target`` seconds.

    For each piece the ideal cut is ``target`` after the previous cut; the latest pause no more
    than the lookback window before it is used instead when there is one. The window is a third
    of the target, at most ``max_lookback``.
    """
    if target <= 0:
        raise ValueError("target must be positive")
    lookback = min(max_lookback, target / 3)
    cuts: list[float] = []
    previous = 0.0
    while duration - previous > target:
        ideal = previous + target
        nearby = [t for t in quiet if previous < t <= ideal and t >= ideal - lookback]
        previous = max(nearby) if nearby else ideal
        cuts.append(previous)
    return cuts


def split_wav(path: Path, cuts: list[float], out_dir: Path) -> list[Path]:
    """Write the pieces between ``cuts`` as ``part-NNN.wav`` in ``out_dir``, in order."""
    facts = wav_facts(path)
    bounds = [0, *(int(round(c * facts.rate)) for c in cuts), facts.frames]
    pieces: list[Path] = []
    with wave.open(str(path), "rb") as reader:
        for index in range(len(bounds) - 1):
            reader.setpos(bounds[index])
            frames = reader.readframes(bounds[index + 1] - bounds[index])
            target = out_dir / f"part-{index:03d}.wav"
            with wave.open(str(target), "wb") as writer:
                writer.setnchannels(facts.channels)
                writer.setsampwidth(facts.sample_width)
                writer.setframerate(facts.rate)
                writer.writeframes(frames)
            pieces.append(target)
    return pieces


def find_program(name: str, scope: Scope | None) -> str | None:
    """``name`` on the profile's ``PATH`` (the process ``PATH`` when the profile has none)."""
    return which(name, path=scope.env.get("PATH") if scope is not None else None)


def find_ffmpeg(scope: Scope | None) -> str | None:
    return find_program("ffmpeg", scope)


def run_tool(
    argv: list[str],
    scope: Scope | None,
    *,
    stdin: bytes | None = None,
    timeout: float = FFMPEG_TIMEOUT_S,
) -> subprocess.CompletedProcess[bytes]:
    """Run an external program (no shell) with the minimal child environment."""
    env = child_env(scope, variant="minimal") if scope is not None else {}
    return subprocess.run(  # noqa: S603 - argv list, no shell
        argv,
        input=stdin,
        stdin=None if stdin is not None else subprocess.DEVNULL,
        capture_output=True,
        timeout=timeout,
        env=env,
        check=False,
    )


def to_opus(ffmpeg: str, source: Path, target: Path, scope: Scope | None) -> str | None:
    """Convert ``source`` to Ogg Opus at ``target``; an error text, or None on success."""
    argv = [
        ffmpeg,
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-c:a",
        "libopus",
        "-b:a",
        "48k",
        str(target),
    ]
    try:
        done = run_tool(argv, scope)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"ffmpeg could not run: {exc}"
    if done.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
        detail = done.stderr.decode("utf-8", "replace").strip()[-300:]
        return f"ffmpeg failed ({done.returncode}): {detail or 'no output file'}"
    return None
