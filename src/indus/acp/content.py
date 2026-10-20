"""An editor's prompt blocks → the text a turn is submitted with (P19-08).

Both hosts submit text only, so every block becomes text the model reads and every image becomes a
file staged in the profile's image cache (``<home>/cache/images``) plus a note naming it (the
agent looks at it with ``vision_analyze``):

- ``text`` → the text;
- ``image``: base64 ``data`` → decoded, its signature checked, staged; only a ``uri`` (http/https)
  → fetched through the SSRF-guarded client (every hop checked) and staged; a refusal or failure
  becomes a note and the prompt still runs;
- ``resource_link`` to a local file (``file://``, a bare path, a Windows drive path →
  ``/mnt/<drive>/…``): an image is staged; any other file is read up to ``MAX_BYTES`` and inlined
  under ``[Attached file: name]`` (cut with a note when longer; binary files are omitted with a
  note); a link that is not a local file is a note;
- ``resource`` (embedded): text inlined; a blob decoded and handled like a file of its MIME type;
- every read and every image is capped at ``MAX_BYTES`` (512 KiB); at most ``MAX_IMAGES`` images
  are staged per prompt, the rest are named in one note.

``fingerprint(blocks)`` identifies a prompt as the editor sent it, before any fetch or staging (the
editor's retry of the same prompt matches without downloading anything again; another order is
another prompt).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import secrets
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, cast
from urllib.parse import unquote, urlparse

from indus.acp import paths
from indus.foundation.blocking import run_blocking
from indus.foundation.home import secure_dir
from indus.foundation.scope import Scope

__all__ = [
    "MAX_BYTES",
    "MAX_IMAGES",
    "Fetch",
    "Prepared",
    "fingerprint",
    "local_path",
    "looks_like_image",
    "plain_text",
    "prepare",
]

log = logging.getLogger(__name__)

MAX_BYTES: Final = 512 * 1024
MAX_IMAGES: Final = 10
"""Images staged per prompt (the messaging intake's attachment limit)."""

_TEXTISH: Final = ("graphql", "javascript", "json", "sql", "toml") + (
    "typescript", "x-sh", "x-yaml", "xml", "yaml",
)  # fmt: skip
_TEXT_MIMES: Final = frozenset(f"application/{kind}" for kind in _TEXTISH)
_IMAGE_SUFFIXES: Final = frozenset({".bmp", ".gif", ".jpeg", ".jpg", ".png", ".webp"})

type Fetch = Callable[[str], Awaitable[bytes]]
"""Downloads an image URL (raises on refusal or failure); the default goes through the SSRF
policy (``safety.safe_transport``)."""


class _Refused(Exception):
    pass


@dataclass
class Prepared:
    """The text to submit and the image files it names."""

    text: str
    staged: list[Path] = field(default_factory=list[Path])


# -- identity --------------------------------------------------------------------------------------


def _identity(block: Mapping[str, Any]) -> list[str]:
    kind = str(block.get("type", ""))
    if kind == "text":
        return [kind, str(block.get("text", ""))]
    if kind == "image":
        return [kind, str(block.get("data") or ""), str(block.get("uri") or "")]
    if kind == "resource_link":
        return [kind, str(block.get("uri", ""))]
    if kind == "resource":
        raw = block.get("resource")
        resource = cast(Mapping[str, Any], raw) if isinstance(raw, Mapping) else {}
        return [
            kind,
            str(resource.get("uri", "")),
            str(resource.get("text") or ""),
            str(resource.get("blob") or ""),
        ]
    return [kind, json.dumps(dict(block), sort_keys=True, default=str)]


def fingerprint(blocks: Sequence[Mapping[str, Any]]) -> str:
    """A stable hash of the normalised blocks in order (see the module notes)."""
    digest = hashlib.sha256()
    for block in blocks:
        for part in _identity(block):
            data = part.encode("utf-8", errors="surrogatepass")
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
        digest.update(b"\x00")
    return digest.hexdigest()


def plain_text(blocks: Sequence[Mapping[str, Any]]) -> str | None:
    """The text of a prompt made of text blocks only (None when it carries anything else)."""
    if any(block.get("type") != "text" for block in blocks):
        return None
    return "\n".join(str(block.get("text", "")) for block in blocks)


# -- small helpers ---------------------------------------------------------------------------------


def looks_like_image(head: bytes) -> bool:
    """True when ``head`` starts with a PNG, JPEG, GIF, BMP or WebP signature."""
    return (
        head.startswith(b"\x89PNG\r\n\x1a\n")
        or head.startswith(b"\xff\xd8\xff")
        or head[:6] in (b"GIF87a", b"GIF89a")
        or head.startswith(b"BM")
        or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")
    )


def _suffix_of(head: bytes) -> str:
    if head.startswith(b"\x89PNG"):
        return ".png"
    if head.startswith(b"\xff\xd8"):
        return ".jpg"
    if head[:3] == b"GIF":
        return ".gif"
    if head.startswith(b"BM"):
        return ".bmp"
    return ".webp"


def _mime(value: object) -> str:
    return str(value or "").split(";", 1)[0].strip().lower()


def _is_text_mime(mime: str) -> bool:
    return mime.startswith("text/") or mime in _TEXT_MIMES


def _display(uri: str, name: str | None = None, title: str | None = None) -> str:
    label = (title or "").strip() or (name or "").strip()
    if label:
        return label
    parsed = urlparse(uri)
    candidate = parsed.path if parsed.scheme else uri
    return Path(unquote(candidate)).name or uri or "resource"


def local_path(uri: str) -> Path | None:
    """The local file a URI names (``file://``, a bare path, a Windows drive path), or None."""
    raw = (uri or "").strip()
    if not raw:
        return None
    mounted = paths.drive_to_mount(raw) if os.name != "nt" else None
    if mounted is not None:
        return Path(mounted)
    parsed = urlparse(raw)
    if parsed.scheme == "file":
        if parsed.netloc not in ("", "localhost"):
            return None
        text = unquote(parsed.path or "")
        if len(text) >= 3 and text[0] == "/" and text[2] == ":" and os.name != "nt":
            mounted = paths.drive_to_mount(text[1:])
            if mounted is not None:
                return Path(mounted)
        return Path(text)
    if parsed.scheme:
        return None
    return Path(unquote(raw))


def _decode_text(data: bytes, mime: str) -> str | None:
    """The bytes as text, or None for a binary file."""
    if b"\x00" in data and not _is_text_mime(mime):
        return None
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    if _is_text_mime(mime):
        return data.decode("latin-1")
    return None


def _file_block(uri: str, body: str, *, display: str, note: str | None = None) -> str:
    header = f"[Attached file: {display}]"
    if note:
        header += f" ({note})"
    return f"{header}\nURI: {uri}\n\n{body}"


# -- staging ---------------------------------------------------------------------------------------


def _stage(scope: Scope, data: bytes) -> Path:
    """Write image bytes to ``<home>/cache/images`` (new file, 0600, never through a link)."""
    folder = secure_dir(scope.home / "cache" / "images", home=scope.home, env=scope.env.to_dict())
    name = f"acp_{secrets.token_hex(8)}{_suffix_of(data[:16])}"
    target = folder / name
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(target, flags, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
    finally:
        os.close(fd)
    return target


async def _default_fetch(scope: Scope, url: str) -> bytes:
    from indus.safety.safe_transport import safe_client

    async with (
        safe_client(scope, timeout=20.0) as client,
        client.stream("GET", url) as response,
    ):
        response.raise_for_status()
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_BYTES:
            raise _Refused(f"it is {int(declared):,} bytes, over the {MAX_BYTES:,} byte limit")
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > MAX_BYTES:
                raise _Refused(f"it is over the {MAX_BYTES:,} byte limit")
        return bytes(body)


def _reason(exc: BaseException) -> str:
    verdict = getattr(exc, "verdict", None)
    reason = getattr(verdict, "reason", None)
    if isinstance(reason, str) and reason:
        return reason
    return str(exc) or type(exc).__name__


class _Builder:
    def __init__(self, scope: Scope, fetch: Fetch) -> None:
        self.scope = scope
        self.fetch = fetch
        self.pieces: list[str] = []
        self.staged: list[Path] = []
        self.skipped = 0

    async def image(self, data: bytes, display: str) -> None:
        if len(data) > MAX_BYTES:
            self.pieces.append(
                f"[Image not attached: {display} is {len(data):,} bytes, over the "
                f"{MAX_BYTES:,} byte limit]"
            )
            return
        if not looks_like_image(data[:16]):
            self.pieces.append(
                f"[Image not attached: {display} is not a PNG, JPEG, GIF, WebP or BMP image]"
            )
            return
        if len(self.staged) >= MAX_IMAGES:
            self.skipped += 1
            return
        path = await run_blocking(lambda: _stage(self.scope, data), label="acp image")
        self.staged.append(path)
        self.pieces.append(
            f"[Image attached: {display}, saved at {path}; use vision_analyze to look at it]"
        )

    async def remote_image(self, url: str) -> None:
        if len(self.staged) >= MAX_IMAGES:
            self.skipped += 1
            return
        try:
            data = await self.fetch(url)
        except Exception as exc:
            log.info("an editor image was not fetched: %s", _reason(exc))
            self.pieces.append(f"[Image not fetched: {url} ({_reason(exc)})]")
            return
        await self.image(data, url)

    async def image_block(self, block: Mapping[str, Any]) -> None:
        data, uri = str(block.get("data") or ""), str(block.get("uri") or "")
        if data:
            payload = data.partition(",")[2] if data.startswith("data:") else data
            try:
                raw = base64.b64decode(payload, validate=True)
            except (binascii.Error, ValueError):
                self.pieces.append("[Image not attached: its data is not valid base64]")
                return
            await self.image(raw, _display(uri) if uri else "image")
            return
        if uri.startswith(("http://", "https://")):
            await self.remote_image(uri)
            return
        found = local_path(uri) if uri else None
        if found is not None:
            await self.file(uri, found, display=_display(uri), mime="image/*")
            return
        self.pieces.append("[Image not attached: it carried neither data nor a URL]")

    async def file(self, uri: str, path: Path, *, display: str, mime: str) -> None:
        image = mime.startswith("image/") or path.suffix.lower() in _IMAGE_SUFFIXES

        def read() -> tuple[int, bytes]:
            size = path.stat().st_size
            if image and size > MAX_BYTES:
                return size, b""
            with path.open("rb") as handle:
                return size, handle.read(min(size, MAX_BYTES))

        try:
            size, data = await run_blocking(read, label="acp attachment")
        except OSError as exc:
            body = f"[Could not read the file: {exc.strerror or exc}]"
            self.pieces.append(_file_block(uri, body, display=display))
            return
        if image:
            if size > MAX_BYTES:
                self.pieces.append(
                    f"[Image not attached: {display} is {size:,} bytes, over the "
                    f"{MAX_BYTES:,} byte limit]"
                )
                return
            await self.image(data, display)
            return
        self.text_bytes(uri, data, size, display=display, mime=mime)

    def text_bytes(self, uri: str, data: bytes, size: int, *, display: str, mime: str) -> None:
        text = _decode_text(data[:MAX_BYTES], mime)
        if text is None:
            body = f"[Binary file omitted: {size:,} bytes, type {mime or 'unknown'}]"
            self.pieces.append(_file_block(uri, body, display=display))
            return
        note = f"truncated to {MAX_BYTES} of {size} bytes" if size > MAX_BYTES else None
        self.pieces.append(_file_block(uri, text, display=display, note=note))

    async def link(self, block: Mapping[str, Any]) -> None:
        uri = str(block.get("uri") or "")
        display = _display(uri, block.get("name"), block.get("title"))
        found = local_path(uri)
        if found is None:
            body = "[Link only: this resource is not a local file, so it was not read]"
            self.pieces.append(_file_block(uri, body, display=display))
            return
        await self.file(uri, found, display=display, mime=_mime(block.get("mimeType")))

    async def embedded(self, block: Mapping[str, Any]) -> None:
        raw = block.get("resource")
        if not isinstance(raw, Mapping):
            return
        resource = cast(Mapping[str, Any], raw)
        uri = str(resource.get("uri") or "")
        mime = _mime(resource.get("mimeType"))
        display = _display(uri)
        text = resource.get("text")
        if isinstance(text, str):
            data = text.encode("utf-8")
            self.text_bytes(uri, data, len(data), display=display, mime=mime or "text/plain")
            return
        blob = str(resource.get("blob") or "")
        try:
            data = base64.b64decode(blob, validate=True)
        except (binascii.Error, ValueError):
            body = "[Embedded file omitted: invalid base64]"
            self.pieces.append(_file_block(uri, body, display=display))
            return
        if mime.startswith("image/"):
            await self.image(data, display)
            return
        self.text_bytes(uri, data, len(data), display=display, mime=mime)


async def prepare(
    blocks: Sequence[Mapping[str, Any]], scope: Scope, *, fetch: Fetch | None = None
) -> Prepared:
    """The text a turn is submitted with (see the module notes)."""

    async def fetch_default(url: str) -> bytes:
        return await _default_fetch(scope, url)

    build = _Builder(scope, fetch or fetch_default)
    for block in blocks:
        kind = block.get("type")
        if kind == "text":
            text = str(block.get("text") or "")
            if text:
                build.pieces.append(text)
        elif kind == "image":
            await build.image_block(block)
        elif kind == "resource_link":
            await build.link(block)
        elif kind == "resource":
            await build.embedded(block)
        else:
            build.pieces.append(f"[A {kind or 'content'} block was left out: not supported here]")
    if build.skipped:
        build.pieces.append(
            f"[{build.skipped} more image(s) were not attached: at most {MAX_IMAGES} per message]"
        )
    return Prepared("\n".join(build.pieces), build.staged)
