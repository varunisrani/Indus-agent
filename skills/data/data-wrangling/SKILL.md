---
name: data-wrangling
description: Clean and reshape CSV, JSON and Excel data
version: 1.0.0
author: Indus Agent
license: MIT
metadata:
  indus:
    category: data
    tags: [csv, json, excel, pandas, cleaning]
---

# Data wrangling

Turn messy tabular or nested data into something correct and usable, without silently losing or
inventing rows. Do the work in code with `execute_code`, so every step can be re-run and checked.

## When to use

- The user hands you a CSV, JSON, JSONL or Excel file to clean, merge, filter, reshape or
  summarise.
- A dataset has broken headers, mixed types, duplicates, odd encodings or blank rows.
- You need to convert between formats (Excel to CSV, nested JSON to a flat table).

## Procedure

1. **Leave the original alone.** Work on a copy or write results to a new file. Say where the
   output goes before you write it.
2. **Profile first.** In `execute_code`, load a sample and report: row and column counts, column
   names, inferred types, missing values per column, a few example rows, and obvious problems.
   For Excel, list the sheets before choosing one. For JSON, show the nesting of one record.
3. **Agree on the rules.** Tell the user what you plan to change (drop empty rows, trim spaces,
   parse dates as day-first, merge on `customer_id`) and ask about anything ambiguous with
   `clarify`.
4. **Clean step by step.** One transformation per step, each with a count before and after:
   - normalise headers (trim, lower-case, consistent separators);
   - fix types explicitly (dates with a stated format, numbers with the right decimal separator,
     IDs as text so leading zeros survive);
   - handle missing values on purpose (leave, fill or drop, and say which);
   - remove exact duplicates, then decide on near-duplicates with the user.
5. **Join carefully.** Before a merge, check that keys are unique where they should be. After it,
   compare row counts and list unmatched keys from both sides.
6. **Write the result** in the format the user needs (`to_csv(index=False)`, `to_excel`, JSON
   lines) and keep the cleaning code in a script file so it can be run again on the next export.
7. **Report.** What changed, how many rows were affected by each step, and anything that looked
   wrong but was left alone.

## Pitfalls

- Reading IDs, phone numbers or ZIP codes as numbers: leading zeros vanish. Read them as strings.
- Guessing date formats: `03/04` is March or April depending on the source. Ask or check ranges.
- Encodings: try `utf-8`, then `utf-8-sig` for Excel-made CSVs, then `latin-1`; never ignore
  decode errors silently.
- Spreadsheet formulas: the stored value may differ from what the user sees; read computed values.
- Very large files: process in chunks rather than loading everything into memory.

## Verification

- Row counts reconcile: input rows = output rows + rows you deliberately dropped (with reasons).
- Spot-check a handful of output rows against the source by key.
- Column types in the output match what the user expects.
- The saved script reproduces the output from the original file.
