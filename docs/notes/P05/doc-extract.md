# Task: Document extraction for `read_file` (P05-17) Phase: 05 Owner: wave-2 builder A

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/read_extract.py`
(per-format limits, notebook output rules, SQLite summary, PDF empty-page gap map). Closed before
writing.
Evidence: none.

What it does (own words): turns documents into plain text that `read_file` then pages with the
same `offset/limit` contract. Notebooks show each cell with a label and the text of code-cell
outputs (≤20,000 chars per cell; images, HTML and widgets become placeholders). PDF uses the text
layer (`pypdf`); pages with under 20 characters are "empty" and a gap map (≤20 entries) tells
the model which page ranges have no text (likely scans). Office: `docx` paragraphs and tables,
`xlsx` sheets (≤5,000 rows per sheet, ≤256 columns), `pptx` slide text. OpenDocument (odt, ods,
odp), RTF, EPUB. SQLite: schema, row counts and the first 5 rows of up to 200 tables, cells ≤80
chars, opened read-only. Documents over 50 MB are refused. A reader whose library is missing
answers with a named error telling the user to install `indus[docs]`.

Inputs → outputs: `extract(data: bytes, name: str) -> str`; `is_document(name)`;
`ExtractionError`, `MissingExtra(ExtractionError)`.

Files: `src/indus/tools/builtin/extract.py`, `tests/unit/tools/builtin/test_extract.py`,
`tests/fixtures/documents/` (README only: fixtures are generated inside the tests);
shared: `pyproject.toml` extra `docs`.

Edge cases / failure modes:
- malformed zip / JSON / not-SQLite `.db` → `ExtractionError` naming the problem.
- encrypted PDF → error; PDF scan over 20 s → stop and say so.
- a library raising anything → `ExtractionError`, never a traceback.
- SQLite read through a temp copy (bytes may come from a sandbox), `mode=ro`.

Our design differences / decisions:
- `pypdf`, `python-docx`, `openpyxl` (pinned in 00-layout §4) behind the `docs` extra.
- `pptx`, OpenDocument and EPUB are zip + XML: read with the stdlib (`zipfile`,
  `xml.etree`), no extra needed; RTF with our own small control-word stripper. The phase row also
  names python-pptx, odfpy, ebooklib, striprtf for the extra, but 00-layout §4 has no pins for them;
  the stdlib readers make them unnecessary.

Tests we will write:
- generated fixtures per format (notebook JSON, SQLite file, docx/xlsx built with the libraries
  when installed, pptx/odt/epub zips built by hand, RTF text, a tiny PDF written by hand): limits
  per format (xlsx rows/cols, notebook output cap, SQLite tables/cells), missing extra → named
  error (monkeypatched import), PDF gap map.

## Decisions for the lead
- Extra `docs = ["pypdf==6.19.0", "python-docx==1.2.0", "openpyxl==3.1.5"]` only; uv.lock is
  yours to re-lock. Licence rows exist in 00-layout §4 (BSD-3-Clause, MIT, MIT).

Compare checklist (filled after the rewrite; reopened `tools/read_extract.py`):
- [x] Notebook labels, output cap, placeholders (`tests/unit/tools/builtin/test_extract.py::test_notebook_cells_and_outputs`,
  `::test_notebook_output_is_capped_per_cell`)
- [x] SQLite shape and limits (`::test_sqlite_summary_shape`, `::test_sqlite_table_limit`)
- [x] PDF empty pages, gap map cap, scan time limit (`::test_pdf_text_and_gap_map`,
  `::test_pdf_gap_map_is_capped`, `::test_pdf_scan_stops_at_the_time_limit`)
- [x] xlsx row/column limits, docx order, pptx/ODF/EPUB/RTF (`::test_xlsx_sheets_and_limits`,
  `::test_docx_paragraphs_and_tables`, `::test_pptx_slides_in_order`,
  `::test_opendocument_text_and_sheet`, `::test_epub_follows_the_spine`,
  `::test_rtf_text_without_control_groups`)
- [x] Missing extra named (`::test_missing_extra_is_named`)
- Deliberate differences: the reference reads docx/xlsx with its own zip+XML code and PDF through
  an external converter or `pdftotext`; we use the pinned libraries for PDF/Word/Excel and the
  stdlib for the zip formats. The PDF coverage note fires on any empty page (the reference waits
  for a ratio). The install hint names the real distribution (`indus-agent[docs]`).
- Not taken: legacy binary Office (.doc/.xls/.ppt) — refused as binary; hosted OCR.

## Status

Done (2026-10-18). Shared: `pyproject.toml` (`docs` extra), `uv.lock` regenerated here to test
(lead re-locks). New transitive packages: lxml (BSD-3-Clause), et-xmlfile (MIT).

## Integration (lead)

Merged in the P05 wave 2 integration (2026-10-18, lead's verify tree).
- THIRD_PARTY_NOTICES rows added for openpyxl 3.1.5 (MIT), et-xmlfile 2.0.0 (MIT), pypdf 6.19.0
  (BSD-3-Clause), python-docx 1.2.0 (MIT), lxml 6.1.3 (BSD-3-Clause); licences from installed
  metadata. The licence check gates `[project].dependencies` only, so extras stay a by-hand row.
- `tests/fixtures` is excluded from ruff (`extend-exclude`): ruff formatted the `tiny.ipynb`
  fixture's cells.
