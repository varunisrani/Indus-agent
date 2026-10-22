"""Archives and compressed data from outside: traversal, links and bombs (P25-02, rows B-DOC-1,
B-EXT-3, B-WEB-8, B-MEDIA-2).

Every archive here is built in memory by the test. A "bomb" is a member whose declared size is
far larger than its compressed bytes; the budgets are lowered so the test stays small and fast.
"""

from __future__ import annotations

import io
import stat
import struct
import zipfile
import zlib

import pytest

from indus.learning.skills.hub.sources_extra import ClawHubSource
from indus.tools.builtin import extract
from indus.tools.builtin.media import imaging
from indus.web.routes import profiles as profile_routes

MiB = 1024 * 1024


def _zip(members: dict[str, bytes], *, links: dict[str, str] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
        for name, target in (links or {}).items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, target)
    return buffer.getvalue()


def _bomb_deck(unpacked: int) -> bytes:
    """A PowerPoint-shaped zip whose one slide unpacks to ``unpacked`` bytes of spaces."""
    head = b'<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><a:p><a:t>hi</a:t></a:p>'
    slide = head + b" " * unpacked + b"</p:sld>"
    return _zip({"ppt/slides/slide1.xml": slide})


def test_doc_zip_bomb_is_refused_before_any_reader_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """B-DOC-1 (SEC-1): a document whose parts would unpack past the budget is refused from
    the zip's directory alone; no reader decompresses anything."""
    monkeypatch.setattr(extract, "ZIP_MAX_UNPACKED_BYTES", 4 * MiB)
    data = _bomb_deck(16 * MiB)
    assert len(data) < MiB  # small on disk, large unpacked

    def reader_ran(*_args: object) -> str:
        raise AssertionError("a reader opened the bomb")

    monkeypatch.setitem(extract._READERS, ".pptx", reader_ran)
    with pytest.raises(extract.ExtractionError, match="unpack"):
        extract.extract(data, "deck.pptx")


def test_doc_too_many_parts_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """B-DOC-1 (SEC-1): a document with more parts than the budget is refused."""
    monkeypatch.setattr(extract, "ZIP_MAX_MEMBERS", 10)
    data = _zip({f"word/part{i}.xml": b"<x/>" for i in range(11)})
    with pytest.raises(extract.ExtractionError, match="parts"):
        extract.extract(data, "letter.docx")


def test_doc_within_budget_still_reads() -> None:
    """B-DOC-1: an ordinary document is not touched by the budget."""
    text = extract.extract(_bomb_deck(1024), "deck.pptx")
    assert "hi" in text


def test_hub_zip_keeps_only_safe_members() -> None:
    """B-EXT-3: a hub skill zip never yields a member that leaves the skill folder or is a link;
    a zip that unpacks past the hub's cap is refused."""
    data = _zip(
        {
            "SKILL.md": b"---\nname: ok\ndescription: ok\n---\n",
            "references/ok.md": b"fine",
            "../escape.md": b"no",
            "references/../../escape2.md": b"no",
            "/abs/escape3.md": b"no",
        },
        links={"references/link.md": "/etc/passwd"},
    )
    files = ClawHubSource.unzip(data)
    assert sorted(files) == ["SKILL.md", "references/ok.md"]


def test_hub_zip_over_the_cap_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """B-EXT-3: the hub's unpacked cap counts declared sizes before reading."""
    from indus.learning.skills.hub import sources_extra

    monkeypatch.setattr(sources_extra, "MAX_ZIP_BYTES", 64 * 1024)
    data = _zip({"SKILL.md": b"x", **{f"references/r{i}.md": b" " * 40_000 for i in range(4)}})
    with pytest.raises(Exception, match="25 MiB|more than"):
        ClawHubSource.unzip(data)


@pytest.mark.parametrize(
    ("members", "links"),
    [
        ({"../outside.txt": b"x"}, None),
        ({"/etc/cron.d/x": b"x"}, None),
        ({"memories/../../x": b"x"}, None),
        ({"ok.txt": b"x"}, {"link": "/etc/passwd"}),
    ],
)
def test_web_profile_import_refuses_unsafe_members(
    members: dict[str, bytes], links: dict[str, str] | None
) -> None:
    """B-WEB-8: a profile archive imported through the dashboard may not name a place outside
    the new profile or carry a link."""
    data = _zip(members, links=links)
    with zipfile.ZipFile(io.BytesIO(data)) as archive, pytest.raises(ValueError):
        profile_routes._members(archive)


def test_web_profile_import_counts_declared_sizes(monkeypatch: pytest.MonkeyPatch) -> None:
    """B-WEB-8: the import's size cap is applied to declared sizes before any member is read."""
    monkeypatch.setattr(profile_routes, "EXPORT_MAX_BYTES", MiB)
    data = _bomb_deck(2 * MiB)
    with zipfile.ZipFile(io.BytesIO(data)) as archive, pytest.raises(ValueError, match="large"):
        profile_routes._members(archive)


def _png(width: int, height: int, stream: bytes) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB, not interlaced
    chunks = imaging._chunk(b"IHDR", header) + imaging._chunk(b"IDAT", zlib.compress(stream, 9))
    return imaging.PNG_SIGNATURE + chunks + imaging._chunk(b"IEND", b"")


def test_media_png_stream_inflates_only_to_its_declared_size() -> None:
    """B-MEDIA-2 (SEC-11): without Pillow, a 1x1 PNG whose pixel stream inflates to 64 MiB is cut
    at the 4 bytes its header declares; the image still validates as complete."""
    bomb = _png(1, 1, b"\0" * (64 * MiB))
    assert len(bomb) < MiB
    assert len(imaging._png_inflate(bomb)) == 1 * (1 * 3 + 1)
    imaging.png_stream_complete(bomb)
    honest = _png(2, 2, b"\0" + b"\x10" * 6 + b"\0" + b"\x20" * 6)
    raster = imaging.decode_png(honest)
    assert (raster.width, raster.height) == (2, 2)
