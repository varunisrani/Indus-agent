"""P15-23: inbound media caches, images, documents and albums (K24)."""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.gateway.core.events import Attachment, InboundEvent, MessageKind, Source
from indus.gateway.intake.media import (
    INBOUND_MEDIA_MAX_BYTES,
    INLINE_TEXT_MAX_BYTES,
    MAX_ATTACHMENTS,
    AlbumCollector,
    MediaCache,
    MediaIntake,
    MediaRejected,
    classify,
    pipeline_vision_probe,
)
from indus.gateway.intake.voice import VoiceIntake
from indus.tools.builtin.media.stt import Transcript
from tests.fakes.agent import DictConfig
from tests.fakes.clock import FakeClock

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
HTML = b"<!doctype html><html><body>Not Found</body></html>"


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def src(user: str = "200") -> Source:
    return Source(platform="telegram", chat_id=user, chat_type="dm", user_id=user)


def event(
    *media: Attachment, text: str = "", kind: MessageKind = MessageKind.DOCUMENT, **kw: Any
) -> InboundEvent:
    return InboundEvent(source=src(), text=text, kind=kind, media=media, **kw)


async def chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


async def never() -> AsyncIterator[bytes]:
    raise AssertionError("the body must not be read")
    yield b""  # pragma: no cover


async def no_stt(path: Path, provider: str | None) -> Transcript:
    raise AssertionError("no transcription expected")


def intake_for(scope: Scope, **kw: Any) -> MediaIntake:
    kw.setdefault("voice", VoiceIntake(scope, transcriber=no_stt))
    return MediaIntake(scope, **kw)


# -- the caches ------------------------------------------------------------------------------------


def test_downloads_land_owner_only_under_random_names(scope: Scope) -> None:
    cache = MediaCache(scope)
    path = run(cache.save("images", PNG, name="../../etc/holiday photo.PNG"))
    assert path.parent == scope.home / "cache" / "images"
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600
    assert re.fullmatch(r"img_[0-9a-f]{12}\.png", path.name)
    assert path.read_bytes() == PNG
    for kind in ("audio", "video", "documents"):
        assert cache.folder(kind).stat().st_mode & 0o777 == 0o700  # type: ignore[arg-type]


def test_the_128_mib_cap(scope: Scope) -> None:
    cache = MediaCache(scope)
    assert cache.max_bytes == INBOUND_MEDIA_MAX_BYTES == 134217728
    with pytest.raises(MediaRejected, match="too large"):
        run(cache.save("documents", never(), name="big.pdf", declared_size=134217728 + 1))
    folder = scope.home / "cache" / "documents"
    assert not folder.exists() or list(folder.iterdir()) == []

    small = MediaCache(scope, max_bytes=1024)
    with pytest.raises(MediaRejected, match="too large"):
        run(small.save("documents", chunks(b"a" * 600, b"b" * 600), name="lying.txt"))
    with pytest.raises(MediaRejected, match="too large"):
        run(small.save("documents", b"x" * 1025, name="big.txt"))
    assert list(small.folder("documents").iterdir()) == []  # the partial file is gone
    exact = run(small.save("documents", chunks(b"a" * 1024), name="ok.txt"))
    assert exact.stat().st_size == 1024


def test_types_are_checked(scope: Scope) -> None:
    cache = MediaCache(scope)
    with pytest.raises(MediaRejected, match="not an image"):
        run(cache.save("images", HTML, name="photo.jpg"))
    with pytest.raises(MediaRejected, match="not supported"):
        run(cache.save("documents", b"MZ\x90\x00", name="setup.exe"))
    with pytest.raises(MediaRejected, match="not supported"):
        run(cache.save("documents", b"\x7fELF", mime="application/octet-stream"))
    with pytest.raises(MediaRejected, match="empty"):
        run(cache.save("documents", b"", name="empty.txt"))
    assert list(cache.folder("images").iterdir()) == []
    assert list(cache.folder("documents").iterdir()) == []
    assert run(cache.save("audio", b"OggS", mime="audio/ogg")).suffix == ".ogg"
    assert run(cache.save("documents", b"%PDF-1.7", mime="application/pdf")).suffix == ".pdf"


def test_only_regular_files_inside_the_caches_are_admitted(scope: Scope, tmp_path: Path) -> None:
    cache = MediaCache(scope)
    kept = run(cache.save("documents", b"notes", name="a.txt"))
    assert cache.admit(str(kept)) == kept
    outside = tmp_path / "secret.txt"
    outside.write_text("do not read")
    link = cache.folder("documents") / "link.txt"
    link.symlink_to(outside)
    assert cache.admit(str(link)) is None
    linked_dir = cache.folder("images") / "sub"
    linked_dir.symlink_to(tmp_path, target_is_directory=True)
    assert cache.admit(str(linked_dir / "secret.txt")) is None
    assert cache.admit(str(outside)) is None
    assert cache.admit("/etc/passwd") is None
    assert cache.admit("a.txt") is None
    assert cache.admit(str(cache.folder("documents"))) is None
    assert cache.admit(str(kept) + "\x00") is None


def test_classification() -> None:
    def att(ref: str, mime: str = "") -> Attachment:
        return Attachment(ref=ref, mime=mime)

    assert classify(att("scan.png", "image/png"), MessageKind.DOCUMENT) == "images"
    assert classify(att("x", "image/jpeg"), MessageKind.DOCUMENT) == "images"
    assert classify(att("AgAD123"), MessageKind.PHOTO) == "images"
    assert classify(att("AgAD123"), MessageKind.VOICE) == "audio"
    assert classify(att("clip.mp4"), MessageKind.DOCUMENT) == "video"
    assert classify(att("report.pdf", "application/pdf"), MessageKind.PHOTO) == "documents"
    assert classify(att("drawing.svg", "image/svg+xml"), MessageKind.DOCUMENT) == "documents"


# -- the enricher ----------------------------------------------------------------------------------


def test_foreign_paths_are_refused_and_never_read(scope: Scope, tmp_path: Path) -> None:
    marker = tmp_path / "private.txt"
    marker.write_text("MARKER-do-not-leak")
    intake = intake_for(scope)
    for ref in (str(marker), "/etc/passwd", "../private.txt", "~/private.txt", "file:///etc/hosts"):
        text = run(intake.enrich(event(Attachment(ref=ref, mime="text/plain"), text="read this")))
        assert "points outside the media cache" in text, ref
        assert "MARKER" not in text and "root:" not in text
        assert text.endswith("read this")


def test_remote_refs_go_through_the_platform_fetcher(scope: Scope) -> None:
    fetched: list[str] = []

    async def fetch(attachment: Attachment) -> AsyncIterator[bytes]:
        fetched.append(attachment.ref)
        yield b"line one\n"
        yield b"line two\n"

    intake = intake_for(scope)
    lonely = run(intake.enrich(event(Attachment(ref="https://files.example/n.txt"))))
    assert "could not be downloaded" in lonely and fetched == []
    undo = intake.register_fetcher("telegram", fetch)
    text = run(intake.enrich(event(Attachment(ref="https://files.example/notes.txt"))))
    assert fetched == ["https://files.example/notes.txt"]
    assert "'notes.txt'" in text and "line one\nline two" in text
    saved = list((scope.home / "cache" / "documents").iterdir())
    assert len(saved) == 1 and saved[0].name.startswith("doc_")
    undo()
    assert "could not be downloaded" in run(intake.enrich(event(Attachment(ref="BQAC-file-id"))))


def test_documents(scope: Scope) -> None:
    cache = MediaCache(scope)
    intake = intake_for(scope, cache=cache)

    async def main() -> None:
        small = await cache.save("documents", b"alpha\n```code```\nbeta\n", name="a.md")
        big = await cache.save("documents", b"x" * (INLINE_TEXT_MAX_BYTES + 1), name="b.txt")
        binary = await cache.save("documents", b"\xff\xfe\x00bin", name="c.txt")
        pdf = await cache.save("documents", b"%PDF-1.7 ...", name="d.pdf")
        text = await intake.enrich(
            event(
                *(Attachment(ref=str(p)) for p in (small, big, binary, pdf)),
                text="summarise these",
            )
        )
        blocks = text.split("\n\n")
        assert blocks[-1] == "summarise these"
        assert f"(saved at {small}). Its content:]\n````\nalpha\n```code```\nbeta\n````" in text
        assert f"(PDF file, 12 bytes), saved at {pdf}" in text
        assert f"saved at {big}. Its content is not included" in text
        assert "x" * 100 not in text
        assert f"saved at {binary}. Its content is not included" in text

    run(main())


def test_image_mode_follows_the_model(scope: Scope) -> None:
    cache = MediaCache(scope)
    described: list[Path] = []

    async def describe(path: Path) -> str:
        described.append(path)
        return "a red bicycle against a brick wall"

    async def broken(path: Path) -> str:
        raise RuntimeError("side model down")

    async def sees(_event: InboundEvent) -> bool:
        return seeing

    async def fails(_event: InboundEvent) -> bool:
        raise RuntimeError("catalogue offline")

    seeing = True

    async def main() -> None:
        nonlocal seeing
        photo = await cache.save("images", PNG, name="bike.png")
        doc_image = await cache.save("images", PNG, mime="image/png")
        photo_event = event(
            Attachment(ref=str(photo), mime="image/png"),
            text="what is this?",
            kind=MessageKind.PHOTO,
        )
        native = await intake_for(scope, cache=cache, describe=describe, sees_images=sees).enrich(
            photo_event
        )
        assert "Look at it with vision_analyze" in native and str(photo) in native
        assert described == []
        seeing = False
        text = await intake_for(scope, cache=cache, describe=describe, sees_images=sees).enrich(
            photo_event
        )
        assert "It shows: a red bicycle against a brick wall" in text and described == [photo]
        assert text.endswith("what is this?")
        fallback = await intake_for(scope, cache=cache, describe=broken, sees_images=fails).enrich(
            photo_event
        )
        assert "could not be described automatically" in fallback
        # an image sent as a document is an image
        as_doc = await intake_for(scope, cache=cache, describe=describe).enrich(
            event(Attachment(ref=str(doc_image), mime="image/png"))
        )
        assert "It shows:" in as_doc

    run(main())


def test_more_than_the_limit_is_noted(scope: Scope) -> None:
    cache = MediaCache(scope)

    async def main() -> str:
        paths = [await cache.save("documents", b"n", name=f"{n}.txt") for n in range(12)]
        return await intake_for(scope, cache=cache).enrich(
            event(*(Attachment(ref=str(p)) for p in paths))
        )

    text = run(main())
    assert text.count("Its content:") == MAX_ATTACHMENTS
    assert "[2 more attachment(s) were not opened.]" in text


def test_send_time_stamps_only_when_enabled(scope: Scope) -> None:
    sent = datetime(2026, 3, 4, 5, 6, tzinfo=UTC)
    plain = event(text="hi", kind=MessageKind.TEXT, timestamp=sent)
    assert run(intake_for(scope).enrich(plain)) == "hi"
    on = scope.with_config(DictConfig({"gateway": {"message_timestamps": {"enabled": True}}}))
    assert run(intake_for(on).enrich(plain)) == "[Sent 2026-03-04 05:06 UTC]\n\nhi"


def test_the_vision_probe_reads_the_effective_route(scope: Scope) -> None:
    class Profile:
        supports_vision = True

    class Route:
        profile = Profile()

    class Service:
        route = Route()

    class Pipeline:
        def __init__(self) -> None:
            self.scope = scope
            self.service = Service()

        def session_key(self, source: Source) -> str:
            return "k"

        async def effective_model(self, key: str, source: Source) -> None:
            return None

    probe = pipeline_vision_probe(Pipeline())  # type: ignore[arg-type]
    assert run(probe(event(text="x"))) is True
    Profile.supports_vision = False
    assert run(probe(event(text="x"))) is False


# -- albums ----------------------------------------------------------------------------------------


def test_an_album_becomes_one_event(fake_clock: FakeClock) -> None:
    forwarded: list[InboundEvent] = []

    async def forward(item: InboundEvent) -> None:
        forwarded.append(item)

    def photo(n: int, caption: str = "", album: str | None = "g1") -> InboundEvent:
        meta = {"album_id": album} if album else {}
        return event(
            Attachment(ref=f"photo{n}", mime="image/jpeg"),
            text=caption,
            kind=MessageKind.PHOTO,
            metadata=meta,
            message_id=f"p{n}",
        )

    async def main() -> None:
        albums = AlbumCollector(fake_clock, forward)
        assert await albums.front_door(photo(1, "our trip"))
        assert await albums.front_door(photo(2))
        await fake_clock.advance_and_settle(0.5)
        assert await albums.front_door(photo(3, "day two"))
        await fake_clock.advance_and_settle(0.5)
        assert forwarded == []
        await fake_clock.advance_and_settle(0.4)
        assert len(forwarded) == 1
        merged = forwarded[0]
        assert [m.ref for m in merged.media] == ["photo1", "photo2", "photo3"]
        assert merged.text == "our trip\nday two" and merged.message_id == "p1"
        assert not await albums.front_door(merged)  # an assembled album passes through
        # a burst without an album id; text from the sender sends the held burst first
        assert await albums.front_door(photo(4, album=None))
        text = event(text="look at these", kind=MessageKind.TEXT)
        assert not await albums.front_door(text)
        assert len(forwarded) == 2 and [m.ref for m in forwarded[1].media] == ["photo4"]
        # a different album flushes the one held
        assert await albums.front_door(photo(5, album="g2"))
        assert await albums.front_door(photo(6, album="g3"))
        assert [m.ref for m in forwarded[2].media] == ["photo5"]
        await albums.aclose()

    run(main())


def test_files_never_leave_the_profile(scope: Scope) -> None:
    cache = MediaCache(scope)
    path = run(cache.save("video", b"\x00\x00\x00\x18ftypmp42", name="clip.mp4"))
    assert os.path.commonpath([path, scope.home]) == str(scope.home)
    text = run(
        intake_for(scope, cache=cache).enrich(
            event(Attachment(ref=str(path), mime="video/mp4"), kind=MessageKind.VIDEO)
        )
    )
    assert f"sent a video '{path.name}', saved at {path}" in text
