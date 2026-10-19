"""Inbound attachments: the media caches, images, documents, albums (P15-23, K24).

Every downloaded byte lands in a per-kind cache under the profile home (``MediaCache``):
``cache/images``, ``cache/audio``, ``cache/video``, ``cache/documents`` (folders 0700, files 0600,
created exclusively and never through a symlink), named ``<kind>_<random><ext>``: the sender's
file name only contributes an allow-listed extension and a cleaned display name, never a path.
The cap is ``INBOUND_MEDIA_MAX_BYTES`` (128 MiB): a declared size over it is refused before
anything is read, a stream is counted while it is written and the partial file removed when it
goes over. Types: an image must start with an image signature (an HTML error page is not one);
audio, video and documents need an allow-listed extension or MIME type.

An attachment ref that is a local path is used only when it resolves to a regular file inside one
of the caches (an adapter that downloaded it already); any other path is refused and never opened.
Other refs (URLs, platform file ids) go through the fetcher the platform registered. Nothing is
ever executed.

``MediaIntake.enrich`` is the admission pipeline's ``enrich`` hook. It builds the model text: the
optional send-time stamp (``gateway.message_timestamps.enabled``), one note per attachment, then
the message (with its reply quote). Images: ``native`` when the turn's effective model reads
images (the note points the agent at the cached file for ``vision_analyze``, which shows it the
pixels), else ``text`` (a side-model description first); a failed decision or description falls
back to a plain pointer note. Voice and audio messages: ``voice.VoiceIntake``. Documents:
text-like files up to ``INLINE_TEXT_MAX_BYTES`` are inlined, others get a context note; image
documents are handled as images.

``AlbumCollector`` is a front door that holds media messages of one sender in one chat (an album
id, or photos sent in a burst) for a short quiet window and hands them on as one event with the
captions merged.
"""

from __future__ import annotations

import contextlib
import json
import logging
import mimetypes
import os
import re
import secrets
import stat
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import unquote, urlparse

from indus.foundation.blocking import run_blocking
from indus.foundation.clock import Clock
from indus.foundation.home import is_managed, secure_dir
from indus.foundation.scope import Scope
from indus.gateway.core.batching import Batcher, merge_events
from indus.gateway.core.events import (
    Attachment,
    InboundEvent,
    MessageKind,
    model_text,
    with_reply_context,
)
from indus.gateway.core.sent_index import SentIndex
from indus.gateway.intake.voice import VoiceIntake, setting

if TYPE_CHECKING:
    from indus.gateway.core.admission import AdmissionPipeline

__all__ = [
    "ALBUM_CAP_S",
    "ALBUM_QUIET_S",
    "INBOUND_MEDIA_MAX_BYTES",
    "INLINE_TEXT_MAX_BYTES",
    "MAX_ATTACHMENTS",
    "AlbumCollector",
    "CacheKind",
    "Describer",
    "Fetcher",
    "MediaCache",
    "MediaIntake",
    "MediaRejected",
    "VisionProbe",
    "classify",
    "looks_like_image",
    "pipeline_vision_probe",
    "vision_describer",
]

log = logging.getLogger(__name__)

INBOUND_MEDIA_MAX_BYTES = 134_217_728  # 128 MiB
INLINE_TEXT_MAX_BYTES = 100 * 1024
MAX_ATTACHMENTS = 10
ALBUM_QUIET_S = 0.8
ALBUM_CAP_S = 3.0

type CacheKind = Literal["images", "audio", "video", "documents"]
type Fetcher = Callable[[Attachment], AsyncIterator[bytes]]
"""A platform's download of one attachment, as chunks (Telegram ``getFile`` and friends)."""
type Describer = Callable[[Path], Awaitable[str]]
type VisionProbe = Callable[[InboundEvent], Awaitable[bool]]

# Extensions each cache takes. MIME types come from the standard library's built-in table (no
# system files read) plus the few common chat types it lacks.
IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"})
AUDIO_EXTS = frozenset({".ogg", ".oga", ".opus", ".mp3", ".m4a", ".wav", ".flac", ".aac", ".weba"})
VIDEO_EXTS = frozenset({".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi"})
TEXT_EXTS = frozenset(
    [
        ".txt",
        ".md",
        ".markdown",
        ".csv",
        ".tsv",
        ".log",
        ".json",
        ".jsonl",
        ".xml",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".conf",
        ".html",
        ".htm",
        ".css",
        ".svg",
        ".py",
        ".js",
        ".ts",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".rb",
        ".php",
        ".sh",
        ".sql",
        ".diff",
        ".patch",
        ".tex",
        ".rst",
    ]
)
OFFICE_EXTS = frozenset(
    [
        ".pdf",
        ".rtf",
        ".epub",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".odt",
        ".ods",
        ".odp",
    ]
)
DOCUMENT_EXTS = TEXT_EXTS | OFFICE_EXTS
_EXTS: Mapping[CacheKind, frozenset[str]] = {
    "images": IMAGE_EXTS,
    "audio": AUDIO_EXTS,
    "video": VIDEO_EXTS,
    "documents": DOCUMENT_EXTS,
}
_PREFIX: Mapping[CacheKind, str] = {
    "images": "img",
    "audio": "audio",
    "video": "video",
    "documents": "doc",
}
_MIMES = mimetypes.MimeTypes()
_CHAT_MIMES: Mapping[str, tuple[str, ...]] = {
    "image/jpg": (".jpg",),
    "audio/ogg": (".ogg",),
    "audio/wav": (".wav",),
    "audio/wave": (".wav",),
    "audio/mp4": (".m4a",),
    "audio/x-m4a": (".m4a",),
    "audio/flac": (".flac",),
    "video/x-matroska": (".mkv",),
    "application/x-yaml": (".yaml",),
}


def _exts_for(mime: str) -> tuple[str, ...]:
    """Extensions a MIME type stands for: ours first, then the stdlib's (sorted, stable)."""
    return (*_CHAT_MIMES.get(mime, ()), *sorted(_MIMES.guess_all_extensions(mime)))


_DISPLAY = re.compile(r"[^\w.\- ]")


class MediaRejected(Exception):
    """An attachment that is not used; ``reason`` is short and safe to show."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def looks_like_image(head: bytes) -> bool:
    """True when ``head`` starts with a PNG, JPEG, GIF, BMP or WebP signature."""
    return (
        head.startswith(b"\x89PNG\r\n\x1a\n")
        or head.startswith(b"\xff\xd8\xff")
        or head[:6] in (b"GIF87a", b"GIF89a")
        or head.startswith(b"BM")
        or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")
    )


def _suffix(name: str) -> str:
    return os.path.splitext(name)[1].lower()


def _name_of(ref: str) -> str:
    """The file name part of a ref (a URL's last path segment), or ""."""
    path = urlparse(ref).path if "://" in ref else ref
    return os.path.basename(unquote(path).rstrip("/"))


def display_name(name: str, fallback: str) -> str:
    cleaned = _DISPLAY.sub("_", os.path.basename(name or "")).strip()[:80]
    return cleaned or fallback


def classify(attachment: Attachment, event_kind: MessageKind) -> CacheKind:
    """Which cache an attachment belongs to (per-attachment type wins over the message kind)."""
    mime = (attachment.mime or "").split(";")[0].strip().lower()
    ext = _suffix(_name_of(attachment.ref))
    if ext in IMAGE_EXTS or (mime.startswith("image/") and set(_exts_for(mime)) & IMAGE_EXTS):
        return "images"
    if mime.startswith("audio/") or ext in AUDIO_EXTS:
        return "audio"
    if mime.startswith("video/") or ext in VIDEO_EXTS:
        return "video"
    if not mime and not ext:
        by_kind: Mapping[MessageKind, CacheKind] = {
            MessageKind.PHOTO: "images",
            MessageKind.VOICE: "audio",
            MessageKind.AUDIO: "audio",
            MessageKind.VIDEO: "video",
        }
        return by_kind.get(event_kind, "documents")
    return "documents"


def _human_size(size: int) -> str:
    if size < 1024:
        return f"{size} bytes"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _open_new(path: Path) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    return os.open(path, flags, 0o600)


def _head(path: Path, size: int = 16) -> bytes:
    with path.open("rb") as handle:
        return handle.read(size)


# -- the caches ------------------------------------------------------------------------------------


class MediaCache:
    """The profile's inbound media caches (see the module notes)."""

    def __init__(self, scope: Scope, *, max_bytes: int = INBOUND_MEDIA_MAX_BYTES) -> None:
        self.scope = scope
        self.max_bytes = max_bytes

    def root(self, kind: CacheKind) -> Path:
        return self.scope.home / "cache" / kind

    def folder(self, kind: CacheKind) -> Path:
        """The cache folder, created owner-only (managed installs keep their own modes)."""
        path = self.root(kind)
        secure_dir(path.parent, env=self.scope.env)
        path.mkdir(mode=0o700, exist_ok=True)
        if os.name == "posix" and not is_managed(self.scope.home, self.scope.env):
            os.chmod(path, 0o700)
        return path

    def extension(self, kind: CacheKind, name: str, mime: str) -> str:
        """The cached file's extension; ``MediaRejected`` for a type we do not take."""
        allowed = _EXTS[kind]
        ext = _suffix(name)
        if ext in allowed:
            return ext
        clean = (mime or "").split(";")[0].strip().lower()
        for known in _exts_for(clean) if clean else ():
            if known in allowed:
                return known
        if kind == "images" and not ext:
            return ".jpg"  # checked by its signature after the download
        what = ext or clean or "unknown"
        raise MediaRejected(f"{what} files are not supported here")

    def too_large(self, size: int) -> MediaRejected:
        limit = self.max_bytes // (1024 * 1024)
        return MediaRejected(f"it is too large ({_human_size(size)}; the limit is {limit} MB)")

    async def save(
        self,
        kind: CacheKind,
        data: bytes | AsyncIterable[bytes],
        *,
        name: str = "",
        mime: str = "",
        declared_size: int | None = None,
    ) -> Path:
        """Write one download into the cache; ``MediaRejected`` when it is not taken."""
        if declared_size is not None and declared_size > self.max_bytes:
            raise self.too_large(declared_size)
        if isinstance(data, bytes) and len(data) > self.max_bytes:
            raise self.too_large(len(data))
        ext = self.extension(kind, name, mime)
        folder = await run_blocking(self.folder, kind, label="media cache")
        path = folder / f"{_PREFIX[kind]}_{secrets.token_hex(6)}{ext}"
        fd = await run_blocking(_open_new, path, label="media cache")
        total = 0
        try:
            try:
                if isinstance(data, bytes):
                    total = len(data)
                    await run_blocking(_write_all, fd, data, label="media cache")
                else:
                    async for chunk in data:
                        total += len(chunk)
                        if total > self.max_bytes:
                            raise self.too_large(total)
                        await run_blocking(_write_all, fd, bytes(chunk), label="media cache")
            finally:
                os.close(fd)
            if total == 0:
                raise MediaRejected("it is empty")
            if kind == "images" and not looks_like_image(
                await run_blocking(_head, path, label="media cache")
            ):
                raise MediaRejected("it is not an image")
        except BaseException:
            with contextlib.suppress(OSError):
                path.unlink()
            raise
        return path

    def admit(self, ref: str) -> Path | None:
        """A local ref that is a regular file inside one of the caches (resolved), else None."""
        if not ref or "\x00" in ref or not os.path.isabs(ref):
            return None
        try:
            info = os.lstat(ref)
        except (OSError, ValueError):
            return None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            return None
        real = os.path.realpath(ref)
        for kind in _EXTS:
            root = os.path.realpath(self.root(kind))
            if real.startswith(root + os.sep):
                return Path(real)
        return None


def _looks_local(ref: str) -> bool:
    """A ref naming something on this machine rather than a URL or a platform file id."""
    text = ref.strip()
    if text.startswith(("/", "~", ".", "\\", "file:")):
        return True
    if "://" in text:
        return False
    return "/" in text or "\\" in text or (len(text) > 1 and text[1] == ":")


# -- image routing ---------------------------------------------------------------------------------

DESCRIBE_QUESTION = (
    "Describe this image in two to four sentences: the main subject, any visible text, numbers or "
    "code, and the overall context. For a chart or diagram give its labels and key values."
)


def vision_describer(scope: Scope, aux: Any) -> Describer:
    """A description through P10's ``vision_analyze`` text route (the side model)."""
    from indus.tools.builtin.media.vision import VisionTool

    tool = VisionTool()

    async def describe(path: Path) -> str:
        raw = await tool.analyze(
            {"image_url": str(path), "question": DESCRIBE_QUESTION},
            scope=scope,
            aux=aux,
            native_vision=False,
        )
        data: Any = json.loads(raw) if isinstance(raw, str) else {}
        if not isinstance(data, dict) or not data.get("success"):
            error = data.get("error") if isinstance(data, dict) else None
            raise RuntimeError(str(error or "no description"))
        return str(data.get("analysis") or "")

    return describe


def pipeline_vision_probe(pipeline: AdmissionPipeline) -> VisionProbe:
    """Does this turn's effective model read images? (one-turn → session → channel → default)."""

    async def probe(event: InboundEvent) -> bool:
        source = event.source
        choice = await pipeline.effective_model(pipeline.session_key(source), source)
        if choice is None:
            route: Any = pipeline.service.route
        else:
            from indus.providers.route import resolve

            route = await run_blocking(
                lambda: resolve(
                    pipeline.scope,
                    model=choice.model,
                    provider=choice.provider,
                    base_url=choice.base_url,
                ),
                label="model route",
            )
        profile = getattr(route, "profile", None)
        return bool(getattr(profile, "supports_vision", False))

    return probe


# -- the enricher ----------------------------------------------------------------------------------


def _fence_for(content: str) -> str:
    longest = max((len(m) for m in re.findall(r"`+", content)), default=0)
    return "`" * max(3, longest + 1)


def _read_inline(path: Path) -> str | None:
    data = path.read_bytes()
    if len(data) > INLINE_TEXT_MAX_BYTES or b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


class MediaIntake:
    """The pipeline's ``enrich`` hook (see the module notes)."""

    def __init__(
        self,
        scope: Scope,
        *,
        cache: MediaCache | None = None,
        voice: VoiceIntake | None = None,
        describe: Describer | None = None,
        sees_images: VisionProbe | None = None,
        fetchers: Mapping[str, Fetcher] | None = None,
        sent_index: SentIndex | None = None,
    ) -> None:
        self.scope = scope
        self.cache = cache or MediaCache(scope)
        self.voice = voice or VoiceIntake(scope)
        self.describe = describe
        self.sees_images = sees_images
        self.sent_index = sent_index
        self._fetchers: dict[str, Fetcher] = {k.lower(): v for k, v in (fetchers or {}).items()}

    def register_fetcher(self, platform: str, fetcher: Fetcher) -> Callable[[], None]:
        key = platform.strip().lower()
        self._fetchers[key] = fetcher

        def undo() -> None:
            if self._fetchers.get(key) is fetcher:
                del self._fetchers[key]

        return undo

    async def enrich(self, event: InboundEvent) -> str:
        """The text the agent gets for ``event`` (see the module notes)."""
        if self.sent_index is not None:
            event = with_reply_context(event, self.sent_index)
        text = model_text(event)
        notes = await self._notes(event) if event.media else []
        stamp = self._stamp(event)
        return "\n\n".join(part for part in (stamp, *notes, text) if part and part.strip())

    def _stamp(self, event: InboundEvent) -> str:
        if event.timestamp is None:
            return ""
        if setting(self.scope, "gateway.message_timestamps.enabled", False) is not True:
            return ""
        when = event.timestamp.astimezone(UTC)
        return f"[Sent {when:%Y-%m-%d %H:%M} UTC]"

    async def _notes(self, event: InboundEvent) -> list[str]:
        images: list[Path] = []
        voices: list[Path] = []
        files: list[str] = []
        refused: list[str] = []
        for attachment in event.media[:MAX_ATTACHMENTS]:
            kind = classify(attachment, event.kind)
            try:
                path, shown = await self._localise(event, attachment, kind)
            except MediaRejected as exc:
                refused.append(f"[An attachment could not be used: {exc.reason}.]")
                continue
            if kind == "images":
                images.append(path)
            elif kind == "audio" and event.kind in (MessageKind.VOICE, MessageKind.AUDIO):
                voices.append(path)
            elif kind in ("audio", "video"):
                files.append(self._media_note(path, shown, kind))
            else:
                files.append(await run_blocking(self._document_note, path, shown, label="document"))
        skipped = len(event.media) - MAX_ATTACHMENTS
        if skipped > 0:
            refused.append(f"[{skipped} more attachment(s) were not opened.]")
        notes = await self._image_notes(event, images) if images else []
        if voices:
            duration = event.metadata.get("duration")
            durations = [float(duration)] if isinstance(duration, int | float) else []
            notes += await self.voice.notes_for(voices, durations)
        return [*notes, *files, *refused]

    async def _localise(
        self, event: InboundEvent, attachment: Attachment, kind: CacheKind
    ) -> tuple[Path, str]:
        ref = attachment.ref
        name = _name_of(ref)
        local = self.cache.admit(ref)
        if local is not None:
            size = local.stat().st_size
            if size > self.cache.max_bytes:
                raise self.cache.too_large(size)
            if kind == "images" and not looks_like_image(
                await run_blocking(_head, local, label="media cache")
            ):
                raise MediaRejected("it is not an image")
            return local, display_name(name, local.name)
        if _looks_local(ref):
            raise MediaRejected("it points outside the media cache")
        fetcher = self._fetchers.get(str(event.source.platform).lower())
        if fetcher is None:
            raise MediaRejected("it could not be downloaded")
        try:
            path = await self.cache.save(kind, fetcher(attachment), name=name, mime=attachment.mime)
        except MediaRejected:
            raise
        except Exception as exc:
            log.info("downloading an attachment failed: %s", type(exc).__name__)
            raise MediaRejected("it could not be downloaded") from exc
        return path, display_name(name, path.name)

    async def _image_notes(self, event: InboundEvent, paths: list[Path]) -> list[str]:
        native = False
        if self.sees_images is not None:
            try:
                native = await self.sees_images(event)
            except Exception:
                log.info("the image routing decision failed; using text", exc_info=True)
        if native:
            return [
                f"[The person sent an image, saved at {path}. Look at it with vision_analyze "
                f"(image_url: {path}); it shows you the image itself.]"
                for path in paths
            ]
        notes: list[str] = []
        for path in paths:
            description = ""
            if self.describe is not None:
                try:
                    description = (await self.describe(path)).strip()
                except Exception as exc:
                    log.info("describing an image failed: %s", exc)
            if description:
                notes.append(
                    f"[The person sent an image. It shows: {description}]\n"
                    f"[For a closer look, use vision_analyze with image_url: {path}]"
                )
            else:
                notes.append(
                    f"[The person sent an image, saved at {path}; it could not be described "
                    f"automatically. Use vision_analyze with image_url: {path} to look at it.]"
                )
        return notes

    @staticmethod
    def _media_note(path: Path, shown: str, kind: CacheKind) -> str:
        noun = "a video" if kind == "video" else "an audio file"
        return (
            f"[The person sent {noun} '{shown}', saved at {path}. Its content is not included "
            "here; process it with your tools if the request needs it.]"
        )

    @staticmethod
    def _document_note(path: Path, shown: str) -> str:
        ext = path.suffix.lower()
        size = path.stat().st_size
        if ext in TEXT_EXTS and size <= INLINE_TEXT_MAX_BYTES:
            content = _read_inline(path)
            if content is not None:
                fence = _fence_for(content)
                return (
                    f"[The person sent the file '{shown}' (saved at {path}). Its content:]\n"
                    f"{fence}\n{content.rstrip()}\n{fence}"
                )
        kind = f"{ext.lstrip('.').upper()} file" if ext else "file"
        return (
            f"[The person sent the file '{shown}' ({kind}, {_human_size(size)}), saved at {path}. "
            "Its content is not included here; read it with your tools if the request needs it.]"
        )


# -- albums and photo bursts -----------------------------------------------------------------------


class AlbumCollector:
    """A front door merging one sender's album or photo burst into one event."""

    def __init__(
        self,
        clock: Clock,
        forward: Callable[[InboundEvent], Awaitable[None]],
        *,
        quiet: float = ALBUM_QUIET_S,
        cap: float = ALBUM_CAP_S,
    ) -> None:
        self._forward = forward
        self._batcher: Batcher[InboundEvent] = Batcher(
            clock, flush=self._flush, merge=merge_events, delay_for=lambda _e: quiet, cap=cap
        )

    @staticmethod
    def _key(event: InboundEvent) -> str:
        source = event.source
        return "|".join(
            str(part or "")
            for part in (source.platform, source.chat_id, source.thread_id, source.user_id)
        )

    @staticmethod
    def _album(event: InboundEvent) -> str | None:
        found = event.metadata.get("album_id") or event.metadata.get("media_group_id")
        return str(found) if found else None

    def _collects(self, event: InboundEvent) -> bool:
        if not event.media or event.is_command:
            return False
        return self._album(event) is not None or event.kind is MessageKind.PHOTO

    async def front_door(self, event: InboundEvent) -> bool:
        if event.internal or event.metadata.get("burst_done"):
            return False
        key = self._key(event)
        if not self._collects(event):
            await self._batcher.flush_now(key)  # a held burst goes first
            return False
        held = self._batcher.pending(key)
        if held is not None and self._album(held) != self._album(event):
            await self._batcher.flush_now(key)
        self._batcher.add(key, event)
        return True

    async def _flush(self, key: str, event: InboundEvent) -> None:
        metadata = {**event.metadata, "burst_done": True}
        await self._forward(event.model_copy(update={"metadata": metadata}))

    async def aclose(self) -> None:
        await self._batcher.aclose()
