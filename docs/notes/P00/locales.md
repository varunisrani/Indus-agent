# Task: locale catalogue and key-parity check (P00-14) Phase: P00 Owner: builder (pins + locales)

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `locales/en.yaml` (top-level section
names only, no values), `tests/agent/test_i18n_tui_packs.py` (what "parity" means for a surface pack
measured against an exported key list). Closed before writing.
Evidence: R10.28, D-16.51.

What it does (own words): `locales/en.yaml` is the English source catalogue. Every other
`locales/<lang>.yaml` must carry exactly the same set of leaf keys, with the same `{placeholder}`
names in each string, and every value must be a string. Surfaces whose English text lives in code
(TUI, desktop) export their key list as `locales/_keys.<surface>.json`; their packs
`locales/<lang>.<surface>.yaml` are compared against that list the same way. Today only English
exists, so the check passes with one file; it starts biting the day a second language lands.
Without it, a translation silently drops or invents keys and a placeholder rename breaks
formatting at run time.

Inputs → outputs: `locales/*.yaml`, `locales/_keys.*.json` → `Result`; each violation names the
file, the key and what is wrong (missing, extra, placeholder sets, non-string value, syntax with
line number). Exit 0 clean, 1 violation. No `locales/` directory → note, exit 0.

Parser: stdlib only (guards stay dependency-free). A strict subset of YAML:
- block mappings by indentation (spaces only), keys plain (`[A-Za-z0-9_.-]`) or quoted;
- values: plain scalars, single-quoted (`''` escape), double-quoted (`\" \\ \n \t \uXXXX`) on one
  line; a trailing ` # comment`; full-line comments and blank lines;
- refused with a message naming the line: tabs, anchors `&`, aliases `*`, tags `!`, block scalars
  `|`/`>`, flow `[`/`{` (so a value that begins with `{name}` must be quoted), sequences `- `,
  documents `---`/`...`, directives `%`, `? ` keys, `key: a: b`, unterminated quotes, bad indent;
- duplicate keys at one level are an error; so is a dotted flat key that collides with a nested
  one after flattening.
- Non-strings: an empty value, or a plain scalar YAML would type (`null`, `~`, `true`, `yes`,
  `off`, numbers, `.inf`, …) → "quote it".

Placeholders: `{name}` with an identifier inside; `{{`/`}}` are literal braces. Compared as sets.

Files: `locales/en.yaml`, `tooling/checks/locales.py`, `tooling/checks/tests/test_locales.py`,
registration in `tooling/checks/__init__.py` (`NAMES`, shared with P00-09).

Edge cases / failure modes:
- `locales/` present but `en.yaml` missing → violation.
- `<lang>.<surface>.yaml` with no `_keys.<surface>.json` → violation (cannot be judged).
- `_keys.<surface>.json` is a JSON list of keys, or an object key → English string (then
  placeholders are compared too); anything else → violation.
- Files that are not `<lang>.yaml` / `<lang>.<surface>.yaml` / `_keys.<surface>.json` → ignored
  with a note.
- UTF-8 BOM tolerated; invalid UTF-8 → violation.

Our design differences / decisions:
- Our own section names (`common`, `cli`, `errors`) and fresh English strings.
- The reference checks parity with the full YAML library inside the product's tests; ours is a
  repo guard with its own small reader, so the check runs before any dependency is installed.
- Missing-key fallback to English at run time (the other half of R10.28) belongs to P12.

Tests we will write: temp repos for missing key, extra key, `{count}` vs `{n}`, non-string,
duplicate key, anchors and flow refused with a message, surface pack against `_keys`, English-only
passes, real repo passes; parser unit cases (quotes, comments, escapes).

## Decisions for the lead
- Note name: the brief says `locales.md`, the phase file `locales-parity.md`; used the brief's.
- Language tag shape accepted: `[a-z]{2,3}` optionally `-`/`_` + region/script (`pt-BR`, `zh_Hans`).

Compare checklist (filled after the rewrite; reopened `tests/agent/test_i18n_tui_packs.py`):
- [x] Missing and extra keys both reported, per file (tests: `test_locales.py::test_missing_key_fails`, `::test_extra_key_fails`).
- [x] Surface packs judged against an exported key list (test: `test_locales.py::test_surface_pack_against_key_list`).
- Deliberate differences: the reference parametrises one test per language in the product suite;
  ours is one guard over every file, and also checks placeholders and value types.
- Not taken: overlay/user packs and runtime lookup (P12).

## Status

Done (2026-10-18). `tooling/checks/__init__.py` touched as shared (registration).
