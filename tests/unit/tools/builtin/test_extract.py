"""Document extraction for ``read_file`` (P05-17). Fixtures are generated here or written by hand
in ``tests/fixtures/documents/``; nothing is copied from elsewhere."""

from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.tools.builtin import extract, read_tracking
from indus.tools.builtin.extract import ExtractionError, MissingExtra
from indus.tools.builtin.read_file import read_file

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "documents"


class StepClock:
    """Each ``monotonic()`` call is ``step`` seconds after the previous one."""

    def __init__(self, step: float) -> None:
        self.step = step
        self.now = 0.0

    def monotonic(self) -> float:
        self.now += self.step
        return self.now


# -- builders --------------------------------------------------------------------------------------


def make_pdf(pages: list[str]) -> bytes:
    """A minimal PDF with one Helvetica text line per page ("" makes a page with no text)."""
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    kids: list[int] = []
    for text in pages:
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode() if text else b""
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        content_id = len(objects)
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents %d 0 R "
            b"/Resources << /Font << /F1 3 0 R >> >> >>" % content_id
        )
        kids.append(len(objects))
    objects[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
        b" ".join(b"%d 0 R" % k for k in kids),
        len(kids),
    )
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % number + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    )
    return out.getvalue()


def make_zip(parts: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in parts.items():
            archive.writestr(name, text)
    return buffer.getvalue()


A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"


def slide(*texts: str) -> str:
    paras = "".join(f"<a:p><a:r><a:t>{t}</a:t></a:r></a:p>" for t in texts)
    body = f"<p:sp><p:txBody>{paras}</p:txBody></p:sp>"
    return (
        f'<p:sld xmlns:p="{P}" xmlns:a="{A}"><p:cSld><p:spTree>{body}</p:spTree></p:cSld></p:sld>'
    )


ODF = (
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
    'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"'
)


# -- notebooks and RTF -----------------------------------------------------------------------------


def test_notebook_cells_and_outputs() -> None:
    text = extract.extract((FIXTURES / "tiny.ipynb").read_bytes(), "tiny.ipynb")
    assert "=== markdown cell 1 ===\n# Sales notes\nQuarter summary." in text
    assert "=== code cell 1 ===\ntotal = 2 + 3\nprint(total)" in text
    assert "--- output of code cell 1 ---\n5" in text
    assert "image/png output" in text and "iVBOR" not in text
    assert "ZeroDivisionError: division by zero" in text
    assert "\x1b[" not in text


def test_notebook_output_is_capped_per_cell() -> None:
    book = {
        "cells": [
            {
                "cell_type": "code",
                "source": "loop()",
                "outputs": [{"output_type": "stream", "text": "y" * 25_000}],
            }
        ]
    }
    text = extract.extract(json.dumps(book).encode(), "n.ipynb")
    assert "y" * extract.NOTEBOOK_OUTPUT_CHARS in text
    assert "y" * (extract.NOTEBOOK_OUTPUT_CHARS + 1) not in text
    assert "5,000 more output characters" in text


def test_bad_notebook_is_an_extraction_error() -> None:
    with pytest.raises(ExtractionError):
        extract.extract(b"{not json", "x.ipynb")


def test_rtf_text_without_control_groups() -> None:
    text = extract.extract((FIXTURES / "letter.rtf").read_bytes(), "letter.rtf")
    assert "Dear team," in text
    assert "The café opens at nine." in text
    assert "Line\twith a tab and — a dash." in text
    assert "Courier" not in text and "Not shown" not in text and "Some editor" not in text


# -- SQLite ----------------------------------------------------------------------------------------


def make_db(tmp_path: Path) -> bytes:
    path = tmp_path / "shop.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT, blob BLOB)")
    conn.executemany(
        "INSERT INTO items (name, blob) VALUES (?, ?)",
        [(f"item {i}" + ("z" * 200 if i == 1 else ""), b"\x00\x01") for i in range(7)],
    )
    conn.execute("CREATE TABLE empty (x)")
    conn.execute("CREATE INDEX by_name ON items(name)")
    conn.commit()
    conn.close()
    return path.read_bytes()


def test_sqlite_summary_shape(tmp_path: Path) -> None:
    text = extract.extract(make_db(tmp_path), "shop.db")
    assert "2 table(s); 1 index(es), view(s) or trigger(s)" in text
    assert "## table items (7 rows)" in text
    assert "CREATE TABLE items" in text
    assert "| id | name | blob |" in text
    assert "item 4" in text and "item 5" not in text  # first five rows only
    assert "<2 bytes>" in text
    long_cell = next(line for line in text.splitlines() if "item 1z" in line)
    assert "z" * 100 not in long_cell and "…" in long_cell
    assert "- index by_name" in text


def test_sqlite_table_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extract, "SQLITE_MAX_TABLES", 1)
    assert "[1 more tables not shown]" in extract.extract(make_db(tmp_path), "shop.sqlite")


def test_not_sqlite_is_an_error() -> None:
    with pytest.raises(ExtractionError, match="not a SQLite database"):
        extract.extract(b"plain text pretending", "fake.db")


# -- PDF -------------------------------------------------------------------------------------------


def test_pdf_text_and_gap_map() -> None:
    pytest.importorskip("pypdf")
    data = make_pdf(["Hello quarterly report page one", "", "", "Closing remarks for the year"])
    text = extract.extract(data, "r.pdf")
    assert "=== page 1 ===\nHello quarterly report page one" in text
    assert "Closing remarks for the year" in text
    assert "2 of 4 pages have no text layer" in text and "pages 2-3" in text


def test_pdf_gap_map_is_capped() -> None:
    note = extract._gap_note(list(range(1, 80, 2)), 80)
    assert note.count("page ") == extract.PDF_GAP_ENTRIES
    assert "20 more gaps (20 pages)" in note


def test_pdf_scan_stops_at_the_time_limit() -> None:
    pytest.importorskip("pypdf")
    data = make_pdf(["first page text is here", "second page text", "third page text"])
    text = extract.extract(data, "slow.pdf", clock=StepClock(15.0))  # type: ignore[arg-type]
    assert "stopped after 20 s" in text


def test_missing_extra_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "pypdf", None)
    with pytest.raises(MissingExtra, match=r"extra docs"):
        extract.extract(make_pdf(["x"]), "a.pdf")
    monkeypatch.setitem(sys.modules, "docx", None)
    with pytest.raises(MissingExtra, match="python-docx"):
        extract.extract(b"PK", "a.docx")
    monkeypatch.setitem(sys.modules, "openpyxl", None)
    with pytest.raises(MissingExtra, match="openpyxl"):
        extract.extract(b"PK", "a.xlsx")


# -- office ----------------------------------------------------------------------------------------


def test_docx_paragraphs_and_tables() -> None:
    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_paragraph("Project plan")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = "Owner", "Date"
    table.cell(1, 0).text, table.cell(1, 1).text = "Ana", "May"
    document.add_paragraph("After the table")
    buffer = io.BytesIO()
    document.save(buffer)
    text = extract.extract(buffer.getvalue(), "plan.docx")
    assert text.index("Project plan") < text.index("Owner | Date") < text.index("After the table")
    assert "Ana | May" in text


def test_xlsx_sheets_and_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Budget"
    for row in range(1, 6):
        sheet.append([f"r{row}", row, *range(8)])
    book.create_sheet("Second").append(["only"])
    buffer = io.BytesIO()
    book.save(buffer)
    monkeypatch.setattr(extract, "SHEET_MAX_ROWS", 3)
    monkeypatch.setattr(extract, "SHEET_MAX_COLS", 4)
    text = extract.extract(buffer.getvalue(), "b.xlsx")
    assert "## sheet Budget" in text and "## sheet Second" in text
    assert "r1 | 1 | 0 | 1" in text and "r4" not in text
    assert "[2 more rows not shown (limit 3)]" in text
    assert "[columns after 4 not shown]" in text


def test_pptx_slides_in_order() -> None:
    data = make_zip(
        {
            "ppt/slides/slide10.xml": slide("Tenth"),
            "ppt/slides/slide2.xml": slide("Second title", "Second body"),
            "ppt/slides/slide1.xml": slide("Opening"),
        }
    )
    text = extract.extract(data, "deck.pptx")
    assert text.index("=== slide 1 ===") < text.index("=== slide 2 ===") < text.index("slide 10")
    assert "Second title\nSecond body" in text


def test_opendocument_text_and_sheet() -> None:
    odt = make_zip(
        {
            "content.xml": f"<office:document-content {ODF}><office:body><office:text>"
            '<text:h>Heading</text:h><text:p>One<text:s text:c="2"/>two<text:tab/>three</text:p>'
            "</office:text></office:body></office:document-content>"
        }
    )
    text = extract.extract(odt, "doc.odt")
    assert "Heading" in text and "One  two\tthree" in text
    ods = make_zip(
        {
            "content.xml": f"<office:document-content {ODF}><office:body><office:spreadsheet>"
            '<table:table table:name="Costs"><table:table-row>'
            "<table:table-cell><text:p>rent</text:p></table:table-cell>"
            "<table:table-cell><text:p>900</text:p></table:table-cell>"
            "</table:table-row></table:table></office:spreadsheet></office:body>"
            "</office:document-content>"
        }
    )
    text = extract.extract(ods, "sheet.ods")
    assert "## table Costs" in text and "rent | 900" in text


def test_epub_follows_the_spine() -> None:
    data = make_zip(
        {
            "META-INF/container.xml": (
                '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                '<rootfile full-path="OEBPS/book.opf"/></rootfiles></container>'
            ),
            "OEBPS/book.opf": (
                '<package xmlns="http://www.idpf.org/2007/opf"><manifest>'
                '<item id="b" href="b.xhtml"/><item id="a" href="a.xhtml"/></manifest>'
                '<spine><itemref idref="a"/><itemref idref="b"/></spine></package>'
            ),
            "OEBPS/a.xhtml": (
                "<html><head><style>p{}</style></head><body><h1>Chapter A</h1>"
                "<p>First words.</p></body></html>"
            ),
            "OEBPS/b.xhtml": "<html><body><p>Second &amp; last.</p></body></html>",
        }
    )
    text = extract.extract(data, "book.epub")
    assert text.index("Chapter A") < text.index("Second & last.")
    assert "p{}" not in text


def test_damaged_zip_and_size_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ExtractionError, match="bad zip"):
        extract.extract(b"not a zip at all", "x.pptx")
    monkeypatch.setattr(extract, "MAX_DOCUMENT_BYTES", 10)
    with pytest.raises(ExtractionError, match="not extracted"):
        extract.extract(b"x" * 11, "x.rtf")


def test_is_document() -> None:
    assert extract.is_document("/a/B.PDF") and extract.is_document("x.ipynb")
    assert not extract.is_document("x.txt") and not extract.is_document(".pdf")


# -- through read_file -----------------------------------------------------------------------------


def read(scope: Scope, path: Path, **args: Any) -> dict[str, Any]:
    read_tracking.reset_all()
    return json.loads(asyncio.run(read_file({"path": str(path), **args}, scope=scope)))


def test_read_file_pages_extracted_text(scope: Scope, tmp_path: Path) -> None:
    target = tmp_path / "tiny.ipynb"
    target.write_bytes((FIXTURES / "tiny.ipynb").read_bytes())
    body = read(scope, target, limit=3)
    assert body["extracted_document"] is True
    assert body["content"].startswith("1|=== markdown cell 1 ===")
    assert body["next_offset"] == 4


def test_read_file_reports_extraction_failures(
    scope: Scope, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "broken.docx").write_bytes(b"garbage")
    assert "Cannot read" in read(scope, tmp_path / "broken.docx")["error"]
    monkeypatch.setitem(sys.modules, "pypdf", None)
    (tmp_path / "a.pdf").write_bytes(make_pdf(["x"]))
    assert "extra docs" in read(scope, tmp_path / "a.pdf")["error"]
    (tmp_path / "bad.ipynb").write_text("just text, not json\n")
    assert read(scope, tmp_path / "bad.ipynb")["content"] == "1|just text, not json"
