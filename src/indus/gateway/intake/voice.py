"""Voice messages people send: a transcript, or an honest note (P15-23, R06.23).

``VoiceIntake.notes_for`` turns cached voice clips into lines for the model text:

- a transcript from the configured speech-to-text provider (P10 ``transcribe``), or from the local
  provider when the configured one fails, goes in as a plain quoted line and nothing else (a
  wrapper sentence reads like an instruction and makes the model talk about voice mode);
- an empty, inaudible or failed transcript becomes one note saying that nothing could be made out
  and the agent should ask for the message again instead of guessing;
- with ``gateway.stt_enabled`` off, a note with the clip's path and its length when known.

Clips are memoised by path, size and modification time, so a merged burst, a queued follow-up or a
retry never transcribes the same clip twice (concurrent asks for one clip share one call).
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from indus.foundation.blocking import run_blocking
from indus.foundation.config import loader as config_loader
from indus.foundation.scope import Scope
from indus.tools.builtin.media import audio
from indus.tools.builtin.media.stt import Transcript, transcribe

__all__ = [
    "LOCAL_PROVIDER",
    "MEMO_SIZE",
    "Transcriber",
    "VoiceIntake",
    "inaudible_note",
    "setting",
    "untranscribed_note",
]

log = logging.getLogger(__name__)

LOCAL_PROVIDER = "local"
MEMO_SIZE = 256

type Transcriber = Callable[[Path, str | None], Awaitable[Transcript]]
"""``(clip, provider or None for the configured one) -> Transcript``; raises on failure."""


def setting(scope: Scope, key: str, default: Any) -> Any:
    """The profile's value at ``key`` (read on every call), else ``default``."""
    try:
        view: Any = scope.config if scope.config is not None else config_loader.load(scope)
        value = view.get(key)
    except Exception:
        return default
    return default if value is None else value


def inaudible_note(path: Path) -> str:
    return (
        "[The person sent a voice message, but no words could be made out (it may be silent, "
        "cut off, or the transcription failed). Do not guess what it said; ask them to send it "
        f"again or type it. The audio is saved at {path}.]"
    )


def untranscribed_note(path: Path, duration: float | None) -> str:
    length = f" ({round(duration)} s)" if duration else ""
    return f"[The person sent a voice message{length}: {path}]"


def _wav_duration(path: Path) -> float | None:
    try:
        with path.open("rb") as handle:
            if not audio.is_wav(handle.read(12)):
                return None
        return audio.wav_facts(path).duration
    except Exception:
        return None


class VoiceIntake:
    """Transcripts and notes for voice clips (see the module notes)."""

    def __init__(
        self,
        scope: Scope,
        *,
        transcriber: Transcriber | None = None,
        enabled: Callable[[], bool] | None = None,
    ) -> None:
        self.scope = scope
        self._transcriber = transcriber or self._configured
        self._enabled = enabled or (
            lambda: setting(scope, "gateway.stt_enabled", True) is not False
        )
        self._memo: OrderedDict[tuple[str, int, int], str] = OrderedDict()
        self._running: dict[tuple[str, int, int], asyncio.Future[str]] = {}

    async def _configured(self, path: Path, provider: str | None) -> Transcript:
        return await transcribe(path, scope=self.scope, provider=provider)

    async def notes_for(
        self, paths: Sequence[Path], durations: Sequence[float | None] = ()
    ) -> list[str]:
        """One line per clip, in order (duplicates of one clip are noted once)."""
        notes: list[str] = []
        seen: set[str] = set()
        enabled = self._enabled()
        for n, path in enumerate(paths):
            if str(path) in seen:
                continue
            seen.add(str(path))
            if not enabled:
                duration = durations[n] if n < len(durations) else None
                if duration is None:
                    duration = await run_blocking(_wav_duration, path, label="voice length")
                notes.append(untranscribed_note(path, duration))
                continue
            notes.append(await self.note_for(path))
        return notes

    async def note_for(self, path: Path) -> str:
        """The clip's line, from the memo when this clip was transcribed before."""
        try:
            info = os.stat(path)
            key = (str(path), info.st_size, info.st_mtime_ns)
        except OSError:
            return inaudible_note(path)
        found = self._memo.get(key)
        if found is not None:
            self._memo.move_to_end(key)
            return found
        running = self._running.get(key)
        if running is not None:
            return await asyncio.shield(running)
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._running[key] = future
        try:
            note = await self._transcribe(path)
            future.set_result(note)
        except asyncio.CancelledError:
            future.cancel()
            raise
        except Exception as exc:
            future.set_exception(exc)
            future.exception()  # read here so nobody is warned about it
            raise
        finally:
            del self._running[key]
        self._memo[key] = note
        while len(self._memo) > MEMO_SIZE:
            self._memo.popitem(last=False)
        return note

    async def _transcribe(self, path: Path) -> str:
        try:
            found = await self._transcriber(path, None)
        except Exception as exc:
            log.info("voice transcription failed (%s); trying the local provider", exc)
            configured = str(setting(self.scope, "stt.provider", "") or "").strip().lower()
            if configured == LOCAL_PROVIDER:
                return inaudible_note(path)
            try:
                found = await self._transcriber(path, LOCAL_PROVIDER)
            except Exception as again:
                log.info("local transcription failed too: %s", again)
                return inaudible_note(path)
        text = (found.text or "").strip()
        if found.inaudible or not text:
            return inaudible_note(path)
        return f'"{text}"'
