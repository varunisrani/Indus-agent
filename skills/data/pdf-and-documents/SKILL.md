---
name: pdf-and-documents
description: Extract from and assemble PDF and Office documents
version: 1.0.0
author: Indus Agent
license: MIT
metadata:
  indus:
    category: data
    tags: [pdf, docx, documents, extraction]
---

# PDF and documents

Get text, tables and structure out of PDFs, Word and similar files, and put documents together
(merge, split, convert, fill in) without damaging the originals.

## When to use

- The user shares a PDF, `.docx`, `.xlsx` or similar file and asks what it says, or for a summary,
  table or field values from it.
- The user wants documents merged, split, reordered, converted or generated from a template.
- A scanned document needs its text recovered.

## Procedure

1. **Look at the file first.** `read_file` returns the text of common document types. Check the
   page count, whether the text is real (selectable) or the pages are images, and how tables come
   through.
2. **Choose the approach.**
   - Plain reading or summarising: work from the text `read_file` returns.
   - Precise extraction (tables, form fields, page ranges) or batch work: write a short script and
     run it with `execute_code`, using a library that is available (check with an import first,
     for example `pypdf` for PDFs, `python-docx` for Word, `openpyxl` for Excel).
   - Scans: text recognition needs an OCR tool such as `tesseract`; check it is installed
     (`terminal`: `tesseract --version`) and tell the user if it is not.
3. **Extract with structure.** Keep page numbers with every piece of text you pull out, so
   answers can cite "page 4". Rebuild tables row by row and check column alignment on a sample.
4. **Assemble safely.** Write every new document to a new path. For merges and splits, list the
   page ranges you will use and confirm them with the user when the order matters.
5. **Templates.** To fill a Word or text template, replace clearly marked placeholders and leave
   everything else untouched; report any placeholder you could not fill.
6. **Hand back.** Give the output path, page or section counts, and a short note of anything that
   could not be read (images, handwriting, protected pages).

## Pitfalls

- Multi-column layouts and headers or footers interleave in extracted text. Check reading order on
  a page before trusting a long extraction.
- Encrypted or password-protected PDFs: ask the user for access; never try to break protection.
- Silent loss: hyphenation at line ends, ligatures (`ﬁ`) and non-breaking spaces change words. Clean
  them when exact matching matters.
- Overwriting the source file during a merge or edit.
- Large documents: summarise section by section rather than all at once.

## Verification

- Page counts of outputs match the plan (for merges: the sum of the inputs).
- A few extracted values are checked against the rendered page they came from.
- The original files are unchanged (same size and modification time).
