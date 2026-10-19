"""Composer helpers of the bridge (P12-31): ``paste.collapse``, ``image.attach``,
``input.detect_drop``.

- ``paste.collapse`` keeps a long paste in ``<home>/pastes/paste_<n>_<HHMMSS>.txt`` (owner-only,
  written atomically; ``n`` counts pastes on this connection, the time is local) and returns the
  placeholder the composer shows instead.
- ``image.attach`` checks a file is an image (by its first bytes; at most ``MAX_IMAGE_BYTES``) and
  stages it for the session's next prompt (``prompt.submit`` names the staged files).
- ``input.detect_drop`` tells whether pasted text is one or more dropped paths: one per line, or
  shell-quoted / backslash-escaped words, or ``file://`` URLs, each naming an existing entry.
"""

from __future__ import annotations

import logging
import shlex
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlparse

from indus.foundation import fsio
from indus.foundation.blocking import run_blocking
from indus.foundation.home import expand_path_text, secure_dir
from indus.protocol.bridge import (
    ImageAttachParams,
    ImageAttachResult,
    InputDetectDropParams,
    InputDetectDropResult,
    PasteCollapseParams,
    PasteCollapseResult,
)
from indus.protocol.errors import INVALID_PARAMS
from indus.protocol.methods import ProtocolError

if TYPE_CHECKING:
    from indus.tui_bridge.bridge import Bridge
    from indus.tui_bridge.dispatch import MethodTable

__all__ = ["MAX_IMAGE_BYTES", "PASTE_MODE", "image_kind", "register"]

log = logging.getLogger(__name__)

PASTE_DIR = "pastes"
PASTE_MODE = 0o600
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_DROP_PATHS = 32
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
)


def image_kind(path: Path) -> str | None:
    """The image format by the file's first bytes, or None."""
    try:
        with path.open("rb") as fh:
            head = fh.read(16)
    except OSError:
        return None
    for magic, kind in _SIGNATURES:
        if head.startswith(magic):
            return kind
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


# -- paste -----------------------------------------------------------------------------------------


def _write_paste(home: Path, name: str, text: str) -> Path:
    folder = secure_dir(home / PASTE_DIR, home=home)
    return fsio.atomic_write_text(folder / name, text, mode=PASTE_MODE)


async def paste_collapse(bridge: Bridge, params: PasteCollapseParams) -> PasteCollapseResult:
    bridge.paste_count += 1
    number = bridge.paste_count
    stamp = bridge.clock.now().astimezone().strftime("%H%M%S")
    name = f"paste_{number}_{stamp}.txt"
    path = await run_blocking(_write_paste, bridge.scope.home, name, params.text, label="paste")
    lines = params.text.count("\n") + 1
    placeholder = f"[Pasted text #{number}: {lines} lines, kept in {path}]"
    return PasteCollapseResult(placeholder=placeholder, path=str(path), lines=lines)


# -- images ----------------------------------------------------------------------------------------


def _resolve(bridge: Bridge, raw: str, session_id: str | None) -> Path:
    path = expand_path_text(raw.strip(), bridge.scope.env)
    if not path.is_absolute():
        base: Path | None = None
        service = bridge.built
        if service is not None and session_id is not None:
            try:
                base = service.agent(session_id).options.cwd
            except ProtocolError:
                base = None
        path = (base or Path.cwd()) / path
    return path


def _check_image(path: Path) -> None:
    if not path.is_file():
        raise ProtocolError(INVALID_PARAMS, f"{path} is not a file", path="path")
    if path.stat().st_size > MAX_IMAGE_BYTES:
        raise ProtocolError(INVALID_PARAMS, f"{path.name} is larger than 20 MB", path="path")
    if image_kind(path) is None:
        raise ProtocolError(
            INVALID_PARAMS, f"{path.name} is not a PNG, JPEG, GIF, WebP or BMP image", path="path"
        )


async def image_attach(bridge: Bridge, params: ImageAttachParams) -> ImageAttachResult:
    service = await bridge.service()
    if service.current_id(params.session_id) not in service.attached():
        await service.resume(params.session_id)
    path = _resolve(bridge, params.path, params.session_id)
    await run_blocking(_check_image, path, label="image check")
    view = bridge.watch(params.session_id)
    if path not in view.images:
        view.images.append(path)
    return ImageAttachResult(name=path.name, path=str(path), attached=len(view.images))


# -- dropped paths ---------------------------------------------------------------------------------


def _candidates(text: str) -> list[str]:
    stripped = text.strip()
    if not stripped:
        return []
    if "\n" in stripped:
        return [line.strip() for line in stripped.splitlines() if line.strip()]
    try:
        return shlex.split(stripped)
    except ValueError:
        return [stripped]


def _as_path(bridge: Bridge, word: str) -> Path:
    if word.startswith("file://"):
        return Path(unquote(urlparse(word).path))
    if len(word) >= 2 and word[0] == word[-1] and word[0] in "'\"":
        word = word[1:-1]
    return _resolve(bridge, word, None)


def _detect(bridge: Bridge, text: str) -> InputDetectDropResult:
    words = _candidates(text)
    if not words or len(words) > MAX_DROP_PATHS:
        return InputDetectDropResult(is_drop=False)
    paths: list[Path] = []
    for word in words:
        path = _as_path(bridge, word)
        if not path.exists():
            return InputDetectDropResult(is_drop=False)
        paths.append(path)
    images = [str(p) for p in paths if p.is_file() and image_kind(p) is not None]
    return InputDetectDropResult(is_drop=True, paths=[str(p) for p in paths], images=images)


async def detect_drop(bridge: Bridge, params: InputDetectDropParams) -> InputDetectDropResult:
    return await run_blocking(_detect, bridge, params.text, label="drop check")


def register(table: MethodTable) -> None:
    table.add("paste.collapse", paste_collapse)
    table.add("image.attach", image_attach)
    table.add("input.detect_drop", detect_drop)
