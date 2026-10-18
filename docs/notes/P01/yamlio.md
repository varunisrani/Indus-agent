# Task: YAML seam (P01-08) Phase: 01 Owner: S-config builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_yaml.py` (all),
`scripts/check_config_yaml_writers.py` (what the lint looks for), `utils.py` (the round-trip save
helper, for how new values are quoted). Closed before writing.
Evidence: none.

What it does (own words): one module owns the YAML library. Reads of user data use YAML 1.1 rules
(bare `yes/no/on/off` are booleans, as hand-written configs expect), refuse duplicate keys (a
second `approvals:` block silently replacing the first is a security bug), tolerate a UTF-8 BOM
(Windows editors add one), and build a fresh parser per call because the library's objects are not
thread-safe. A round-trip pair loads and dumps user-edited files so comments, key order, quoting
and blank lines survive a save. A plain dumper writes files the product owns wholly.

Inputs → outputs:
- `load_safe(text | bytes | Path) -> Any`; errors → `YamlError(message, line, column)` (1-based
  line, `None` when unknown).
- `load_roundtrip(path | text) -> CommentedMap` (empty map for a missing/blank file),
  `dump_roundtrip(doc) -> str`.
- `to_roundtrip(value)`: plain Python value → round-trip node, quoting any new string a YAML 1.1
  reader would not read back as that same string (`off`, `yes`, `12:30`, `017`, `~` …).
- `dump_plain(data) -> str` (block style, insertion order, 1.1-safe quoting, no `%YAML` header).
- `roundtrip_key(key)`: the same quoting for a new mapping key (`no`, `on`, `y` as keys).
- `is_ambiguous_string(s)`: the 1.1 check above, exported for the config writer.

Files: `src/indus/foundation/yamlio.py`, `tests/unit/foundation/test_yamlio.py`.

Edge cases / failure modes:
- `on: yes` loads as `{True: True}` — documented and tested, not "fixed".
- duplicate key → `YamlError` whose `line` is the second occurrence.
- BOM at the start of text, bytes or a file → ignored.
- invalid UTF-8 bytes → `YamlError` (not `UnicodeDecodeError`).
- round-trip load uses the library's default (1.2) resolution so the original spelling of every
  scalar survives; callers that need meaning read the same text with `load_safe` (the config
  writer compares meanings, see config-writer note).

Our design differences / decisions:
- house style: mapping indent 2, sequences indented 4 with dash offset 2, unlimited width (no
  line folding, which can change a double-quoted value), Unicode kept readable. Files already in
  this style round-trip byte for byte; other indentation is normalised on the first save.
- pure-Python parser only (one behaviour on every platform; ruamel 0.19 needs no C extension).
- ratchet: a test scans `src/` and fails if any module other than `foundation/yamlio.py` imports
  `ruamel` or `yaml` (R04.5); it also proves itself on a temp copy with a planted import.

Tests we will write:
- 1.1 booleans; duplicate key names the line; BOM text/bytes/file; parse error carries the line.
- commented fixture round-trips byte-identically; editing one value keeps every comment.
- `to_roundtrip` quotes `off`/`12:30`/`yes` and a reload with `load_safe` returns strings.
- `dump_plain` output reloads to the same data and has no directive.
- import ratchet on `src/` and on a planted temp copy.

Compare checklist (filled after the rewrite; reopened `hermes_yaml.py`, `scripts/check_config_yaml_writers.py`):
- [x] 1.1 booleans on read (test: `tests/unit/foundation/test_yamlio.py::test_yaml11_booleans`)
- [x] duplicate keys rejected (test: `::test_duplicate_key_names_line`)
- [x] fresh parser per call; no shared YAML object (code review: every function builds its own)
- [x] round trip keeps comments; 1.1-ambiguous new strings quoted (tests: `::test_roundtrip_is_byte_identical`, `::test_new_ambiguous_strings_are_quoted`)
- Deliberate differences: the reference retries a failed C-parser read with the pure parser; we
  only use the pure parser. Our ratchet bans the import itself outside the seam (stricter than a
  lint of dump call sites); the writer-call ratchet lives with P01-11.

## Status

Done (2026-10-18). `pyproject.toml` `[project].dependencies` gained `ruamel.yaml==0.19.1` (shared
file, deps list only).
