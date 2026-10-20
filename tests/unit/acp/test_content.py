"""P19-08: editor prompt blocks → submit text, staged images and the retry fingerprint."""

from __future__ import annotations

import base64
import os
import stat
from pathlib import Path
from typing import Any

from indus.acp import content
from indus.foundation.scope import Scope
from tests.unit.backends._aio import aio

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _staged(scope: Scope) -> list[Path]:
    folder = scope.home / "cache" / "images"
    return sorted(folder.iterdir()) if folder.exists() else []


@aio
async def test_text_blocks_join(scope: Scope) -> None:
    blocks = [{"type": "text", "text": "fix"}, {"type": "text", "text": "this"}]
    prepared = await content.prepare(blocks, scope)
    assert prepared.text == "fix\nthis"
    assert prepared.staged == []
    assert content.plain_text(blocks) == "fix\nthis"


@aio
async def test_large_text_file_is_cut_with_a_note(scope: Scope, tmp_path: Path) -> None:
    big = tmp_path / "big.log"
    big.write_bytes(b"a" * (600 * 1024))
    block = {"type": "resource_link", "uri": big.as_uri(), "name": "big.log"}
    prepared = await content.prepare([block], scope)
    header, _, body = prepared.text.partition("\n\n")
    assert header.startswith("[Attached file: big.log] (truncated to 524288 of 614400 bytes)")
    assert f"URI: {big.as_uri()}" in header
    assert len(body) == content.MAX_BYTES


@aio
async def test_small_text_file_is_inlined(scope: Scope, tmp_path: Path) -> None:
    note = tmp_path / "notes.md"
    note.write_text("# hello\n", encoding="utf-8")
    prepared = await content.prepare(
        [{"type": "resource_link", "uri": str(note), "name": "n"}], scope
    )
    assert prepared.text.endswith("# hello\n")
    assert "truncated" not in prepared.text


@aio
async def test_binary_files_and_remote_links_are_notes(scope: Scope, tmp_path: Path) -> None:
    blob = tmp_path / "tool.bin"
    blob.write_bytes(b"\x7fELF\x00\x01\x02")
    prepared = await content.prepare(
        [
            {"type": "resource_link", "uri": blob.as_uri(), "name": "tool.bin"},
            {"type": "resource_link", "uri": "https://example.com/spec", "name": "spec"},
            {"type": "resource_link", "uri": (tmp_path / "gone.txt").as_uri(), "name": "gone"},
        ],
        scope,
    )
    assert "[Binary file omitted: 7 bytes" in prepared.text
    assert "[Link only: this resource is not a local file" in prepared.text
    assert "[Could not read the file:" in prepared.text


@aio
async def test_image_links_are_staged_under_the_home(scope: Scope, tmp_path: Path) -> None:
    shot = tmp_path / "shot.png"
    shot.write_bytes(PNG)
    prepared = await content.prepare(
        [{"type": "resource_link", "uri": shot.as_uri(), "name": "shot.png"}], scope
    )
    assert len(prepared.staged) == 1
    staged = prepared.staged[0]
    assert staged.parent == scope.home / "cache" / "images"
    assert staged.read_bytes() == PNG
    if os.name != "nt":
        assert stat.S_IMODE(staged.stat().st_mode) == 0o600
    assert f"saved at {staged}" in prepared.text
    assert "vision_analyze" in prepared.text


@aio
async def test_oversize_and_fake_images_become_notices(scope: Scope, tmp_path: Path) -> None:
    huge = tmp_path / "huge.png"
    huge.write_bytes(PNG + b"\x00" * content.MAX_BYTES)
    blocks: list[dict[str, Any]] = [
        {"type": "resource_link", "uri": huge.as_uri(), "name": "huge.png"},
        {"type": "image", "data": _b64(b"<html>not an image</html>"), "mimeType": "image/png"},
        {"type": "image", "data": "%%%not-base64%%%", "mimeType": "image/png"},
    ]
    prepared = await content.prepare(blocks, scope)
    assert "over the 524,288 byte limit" in prepared.text
    assert "is not a PNG, JPEG, GIF, WebP or BMP image" in prepared.text
    assert "not valid base64" in prepared.text
    assert prepared.staged == []
    assert _staged(scope) == []


@aio
async def test_base64_images_and_embedded_resources(scope: Scope) -> None:
    blocks: list[dict[str, Any]] = [
        {"type": "image", "data": _b64(PNG), "mimeType": "image/png"},
        {
            "type": "resource",
            "resource": {"uri": "file:///w/a.txt", "text": "inline text", "mimeType": "text/plain"},
        },
        {
            "type": "resource",
            "resource": {"uri": "file:///w/b.png", "blob": _b64(PNG), "mimeType": "image/png"},
        },
        {
            "type": "resource",
            "resource": {
                "uri": "file:///w/c.cfg",
                "blob": _b64(b"k = v\n"),
                "mimeType": "text/x-cfg",
            },
        },
    ]
    prepared = await content.prepare(blocks, scope)
    assert len(prepared.staged) == 2
    assert "[Attached file: a.txt]\nURI: file:///w/a.txt\n\ninline text" in prepared.text
    assert "k = v" in prepared.text


@aio
async def test_private_image_url_is_refused_by_the_policy(scope: Scope) -> None:
    for url in ("http://127.0.0.1/x.png", "http://10.1.2.3/y.png", "http://169.254.169.254/z"):
        prepared = await content.prepare(
            [{"type": "image", "data": "", "mimeType": "image/png", "uri": url}], scope
        )
        assert prepared.text.startswith(f"[Image not fetched: {url} (")
        assert "private or internal" in prepared.text or "metadata" in prepared.text
    assert _staged(scope) == []


@aio
async def test_remote_images_are_fetched_through_the_given_fetcher(scope: Scope) -> None:
    asked: list[str] = []

    async def fetch(url: str) -> bytes:
        asked.append(url)
        return PNG

    url = "https://images.example.com/cat.png"
    prepared = await content.prepare(
        [{"type": "image", "data": "", "mimeType": "image/png", "uri": url}], scope, fetch=fetch
    )
    assert asked == [url]
    assert len(prepared.staged) == 1


@aio
async def test_more_than_ten_images_are_capped(scope: Scope) -> None:
    blocks = [{"type": "image", "data": _b64(PNG), "mimeType": "image/png"} for _ in range(12)]
    prepared = await content.prepare(blocks, scope)
    assert len(prepared.staged) == content.MAX_IMAGES
    assert "[2 more image(s) were not attached: at most 10 per message]" in prepared.text


def test_fingerprint_is_stable_and_order_sensitive() -> None:
    one = {"type": "text", "text": "fix the bug"}
    two = {"type": "resource_link", "uri": "file:///w/a.py", "name": "a.py"}
    img = {"type": "image", "data": _b64(PNG), "mimeType": "image/png"}
    first = content.fingerprint([one, two, img])
    assert first == content.fingerprint([dict(one), dict(two), dict(img)])
    assert first != content.fingerprint([two, one, img])
    assert first != content.fingerprint([one, two])
    assert content.fingerprint([{"type": "text", "text": "ab"}]) != content.fingerprint(
        [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
    )


def test_windows_drive_link_maps_to_mnt() -> None:
    if os.name == "nt":
        return
    assert content.local_path("C:\\Users\\me\\a.txt") == Path("/mnt/c/Users/me/a.txt")
    assert content.local_path("file:///D:/src/x.py") == Path("/mnt/d/src/x.py")
    assert content.local_path("file://server/share/x") is None
    assert content.local_path("https://example.com/x") is None
    assert content.local_path("/w/plain.txt") == Path("/w/plain.txt")
