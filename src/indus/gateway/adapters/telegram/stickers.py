"""Telegram sticker descriptions (P16-34, K24).

A sticker carries no text, so the agent gets a one-line note about it instead. A static sticker is
described once by the image describer the media intake uses (P15-23: the vision tool on the side
model) and the description is remembered under the sticker's ``file_unique_id``, which stays the
same across chats and bots: the same sticker again costs nothing.

- `StickerCache`: least recently used first out at 2,000 entries, kept in a JSON file under the
  profile (``<home>/gateway/telegram_stickers.json``, 0600 in a 0700 folder), written atomically
  after each new entry; a missing or unreadable file starts empty (logged).
- `StickerNotes.note_for`: animated and video stickers are never downloaded (an emoji note); a
  cache hit is the note; otherwise the sticker is downloaded through the adapter's own fetcher into
  the intake's image cache, described, remembered and the cached file removed. Concurrent arrivals
  of one sticker share one description. A failed description is not remembered (a later arrival
  tries again) and gives a "could not be described" note.
- Descriptions happen in the adapter, before the gateway's authorization chain sees the message,
  so fresh (uncached) descriptions are bounded: only for private chats and for group messages
  addressed to the bot, and at most ``FRESH_PER_HOUR`` per sender; beyond that the note says the
  sticker was not described.
- The describer and the image cache are the intake's, picked up when the adapter registers its
  downloads (``attach``); without them every sticker gets the emoji note.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

from indus.foundation.blocking import run_blocking
from indus.foundation.clock import Clock
from indus.foundation.fsio import atomic_write_json
from indus.gateway.adapters.telegram.bot_api import FileRef

__all__ = [
    "CACHE_FILE",
    "FRESH_PER_HOUR",
    "STICKER_CACHE_MAX",
    "StickerCache",
    "StickerEntry",
    "StickerNotes",
]

log = logging.getLogger(__name__)

STICKER_CACHE_MAX: Final = 2000
FRESH_PER_HOUR: Final = 10
CACHE_FILE: Final = "telegram_stickers.json"
DESCRIPTION_MAX: Final = 500
_SENDERS_MAX: Final = 4096
_HOUR: Final = 3600.0

type Describer = Callable[[Path], Awaitable[str]]
type Saver = Callable[[AsyncIterator[bytes]], Awaitable[Path]]
type Download = Callable[[FileRef], AsyncIterator[bytes]]


@dataclass(frozen=True)
class StickerEntry:
    description: str
    emoji: str = ""
    set_name: str = ""
    at: float = 0.0


def _one_line(text: str, limit: int) -> str:
    return " ".join(text.replace("[", "(").replace("]", ")").split())[:limit].strip()


class StickerCache:
    """Descriptions by ``file_unique_id`` (see the module notes)."""

    def __init__(self, path: Path | None, *, cap: int = STICKER_CACHE_MAX) -> None:
        self.path = path
        self.cap = cap
        self._entries: OrderedDict[str, StickerEntry] = OrderedDict()
        self._loaded = path is None

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, unique_id: object) -> bool:
        return unique_id in self._entries

    async def load(self) -> None:
        if self._loaded or self.path is None:
            return
        self._loaded = True
        rows = await run_blocking(_read_rows, self.path, label="telegram stickers")
        for unique_id, entry in rows:
            self._entries[unique_id] = entry
        while len(self._entries) > self.cap:
            self._entries.popitem(last=False)

    async def get(self, unique_id: str) -> StickerEntry | None:
        await self.load()
        entry = self._entries.get(unique_id)
        if entry is not None:
            self._entries.move_to_end(unique_id)
        return entry

    async def put(self, unique_id: str, entry: StickerEntry) -> None:
        await self.load()
        self._entries[unique_id] = entry
        self._entries.move_to_end(unique_id)
        while len(self._entries) > self.cap:
            self._entries.popitem(last=False)
        if self.path is None:
            return
        rows = [{"id": k, **v.__dict__} for k, v in self._entries.items()]
        try:
            await run_blocking(_write_rows, self.path, rows, label="telegram stickers")
        except Exception as exc:
            log.warning("telegram: sticker descriptions not saved: %s", type(exc).__name__)


def _read_rows(path: Path) -> list[tuple[str, StickerEntry]]:
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        name = type(exc).__name__
        log.warning("telegram: sticker descriptions unreadable, starting empty: %s", name)
        return []
    stickers = cast(dict[str, object], raw).get("stickers") if isinstance(raw, dict) else None
    if not isinstance(stickers, list):
        return []
    rows: list[tuple[str, StickerEntry]] = []
    for item in cast(list[object], stickers):
        if not isinstance(item, dict):
            continue
        row = cast(dict[str, object], item)
        unique_id, description = row.get("id"), row.get("description")
        if not isinstance(unique_id, str) or not unique_id or not isinstance(description, str):
            continue
        at = row.get("at")
        rows.append(
            (
                unique_id,
                StickerEntry(
                    description=description,
                    emoji=str(row.get("emoji") or ""),
                    set_name=str(row.get("set_name") or ""),
                    at=float(at) if isinstance(at, int | float) and not isinstance(at, bool) else 0,
                ),
            )
        )
    return rows


def _write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write_json(path, {"version": 1, "stickers": rows}, indent=None, mode=0o600)


def _remove(path: Path) -> None:
    path.unlink(missing_ok=True)


def sticker_note(entry: StickerEntry | None, sticker: FileRef) -> str:
    """The agent's line for a sticker (described or not)."""
    emoji = _one_line(sticker.emoji or "", 16)
    set_name = _one_line(sticker.set_name or "", 64)
    what = "an animated sticker" if sticker.is_animated or sticker.is_video else "a sticker"
    head = f"The person sent {what}" + (f" {emoji}" if emoji else "")
    if set_name:
        head += f' from the set "{set_name}"'
    if sticker.is_animated or sticker.is_video:
        return f"[{head}. Animated stickers are not described; the emoji is the hint.]"
    if entry is None:
        return f"[{head}. It could not be described.]"
    return f"[{head}. It shows: {entry.description}]"


class StickerNotes:
    """Notes for incoming stickers (see the module notes)."""

    def __init__(
        self,
        cache: StickerCache,
        clock: Clock,
        *,
        enabled: bool = True,
        fresh_per_hour: int = FRESH_PER_HOUR,
    ) -> None:
        self.cache = cache
        self.clock = clock
        self.enabled = enabled
        self.fresh_per_hour = fresh_per_hour
        self.describe: Describer | None = None
        self.save: Saver | None = None
        self.calls = 0
        self._inflight: dict[str, asyncio.Future[StickerEntry | None]] = {}
        self._fresh: dict[str, deque[float]] = {}

    def attach(self, seat: object) -> Callable[[], None]:
        """Use the media intake's describer and image cache (``seat`` is the intake the adapter
        registers its downloads with); returns the undo."""
        describe = getattr(seat, "describe", None)
        save = getattr(getattr(seat, "cache", None), "save", None)
        if callable(describe) and callable(save):
            self.describe = cast(Describer, describe)

            async def into_cache(stream: AsyncIterator[bytes]) -> Path:
                saving = cast(Callable[..., Awaitable[Path]], save)
                return await saving("images", stream, name="sticker.webp", mime="image/webp")

            self.save = into_cache

        def undo() -> None:
            self.describe = self.save = None

        return undo

    def _budget(self, sender: str) -> bool:
        """One more fresh description for ``sender`` this hour?"""
        now = self.clock.monotonic()
        stamps = self._fresh.get(sender)
        if stamps is None:
            if len(self._fresh) >= _SENDERS_MAX:
                self._fresh.pop(next(iter(self._fresh)))
            stamps = self._fresh[sender] = deque()
        while stamps and now - stamps[0] >= _HOUR:
            stamps.popleft()
        if len(stamps) >= self.fresh_per_hour:
            return False
        stamps.append(now)
        return True

    async def note_for(
        self, sticker: FileRef, *, sender: str, addressed: bool, download: Download
    ) -> str:
        """The note for ``sticker`` from ``sender`` (``addressed``: a DM, or a group message
        to the bot)."""
        if sticker.is_animated or sticker.is_video or not self.enabled:
            return sticker_note(None, sticker)
        unique_id = sticker.file_unique_id
        entry = await self.cache.get(unique_id) if unique_id else None
        if entry is not None:
            return sticker_note(entry, sticker)
        pending = self._inflight.get(unique_id) if unique_id else None
        if pending is not None:
            return sticker_note(await asyncio.shield(pending), sticker)
        if not unique_id or self.describe is None or self.save is None or not addressed:
            return sticker_note(None, sticker)
        if not self._budget(sender):
            log.info("telegram: sticker description budget used up for a sender this hour")
            return sticker_note(None, sticker)
        future: asyncio.Future[StickerEntry | None] = asyncio.get_running_loop().create_future()
        self._inflight[unique_id] = future
        try:
            entry = await self._describe(sticker, download)
            if entry is not None:
                await self.cache.put(unique_id, entry)
        finally:
            del self._inflight[unique_id]
            if not future.done():
                future.set_result(entry)
        return sticker_note(entry, sticker)

    async def _describe(self, sticker: FileRef, download: Download) -> StickerEntry | None:
        describe, save = self.describe, self.save
        if describe is None or save is None:
            return None
        path: Path | None = None
        try:
            path = await save(download(sticker))
            self.calls += 1
            text = _one_line(await describe(path), DESCRIPTION_MAX)
        except Exception as exc:
            log.info("telegram: describing a sticker failed: %s", type(exc).__name__)
            return None
        finally:
            if path is not None:
                with contextlib.suppress(OSError):
                    await run_blocking(_remove, path, label="telegram stickers")
        if not text:
            return None
        return StickerEntry(
            description=text,
            emoji=sticker.emoji or "",
            set_name=sticker.set_name or "",
            at=self.clock.now().timestamp(),
        )
