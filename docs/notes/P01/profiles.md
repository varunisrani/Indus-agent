# Task: Profiles (P01-04) Phase: 01 Owner: P01 S-core builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_constants.py` (lines 290–440
profile predicates, markers, tombstones), `hermes_cli/profiles.py` (`normalize_profile_name`,
`validate_profile_name` 352–420, suggestion helper, `get_active_profile`/`set_active_profile`
2002–2030). `website/docs/user-guide/profiles.md` not read (research spec §3.10 covers it).
Closed before writing.
Evidence: R04.25.

What it does (own words): the library side of profiles. Ids are normalised (trim, lowercase,
`Default` → `default`) and checked against `^[a-z0-9][a-z0-9_-]{0,63}$` plus a reserved list. A
directory under `<root>/profiles/` only counts as a profile when it holds an identity marker and has
no tombstone; tombstones sit in `<root>/profiles/.deleted/<id>` so deleting the profile dir cannot
erase them. `mkdir_in_profile` refuses to create anything inside a tombstoned or missing named
profile, so stray background work cannot resurrect a deleted profile. The sticky profile is the
`<root>/active_profile` file.

Inputs → outputs: ids (any object) → canonical id or `InvalidProfileId`; `root` → sorted live ids;
`home, rel` → created dir or `ProfileTombstoned`/`ProfileNotFound`.

Files: `src/indus/foundation/profiles.py`, `tests/unit/foundation/test_profiles.py`.

Edge cases / failure modes:
- non-string id (`7`) → rejected, never `str()`-coerced.
- empty `.env` alone: listed (identity) but not `servable()`.
- dangling symlink marker counts as identity.
- `active_profile` with BOM, trailing newline, uppercase, or garbage (→ default + warning).
- `write_active("default")` removes the file; writing a missing profile → `ProfileNotFound`.

Our design differences / decisions:
- Marker `PERSONA.md` instead of the reference's persona file name.
- Tombstone content is the deletion time (ISO 8601 UTC) from an injectable clock.
- Writes use a private temp + `os.replace` helper; switch to `fsio.atomic_write_text` when P01-07
  lands (same semantics, 0600).
- CLI-subcommand-name collision check for aliases is P07's.

Tests we will write: id table; suggest_id; marker/servable rules; tombstone listing and
`mkdir_in_profile` refusal; BOM sticky read; write/clear active.

## Decisions for the lead
- `read_active` returns `default` (with a warning) for an invalid id in the file rather than
  raising; preparse then treats it as "no sticky profile".
- Private atomic write helper instead of `fsio` (not in this slice's base); swap after merge.

Compare checklist (filled after the rewrite; reopened `hermes_constants.py` 290–440, `profiles.py`):
- [x] id rule, normalisation and reserved names (test:
  `tests/unit/foundation/test_profiles.py::test_id_table`)
- [x] six markers, dangling symlink counts, empty `.env` not servable (tests: `test_markers`,
  `test_empty_env_marker_not_servable`)
- [x] tombstone beside the dir, not listed, mkdir refused (test: `test_tombstoned_profile`)
- [x] sticky file BOM tolerant, `default` clears (tests: `test_read_active_bom`, `test_write_active_default_removes_file`)
- Deliberate differences: tombstone holds a timestamp instead of a fixed word.
- Not taken: home-root sniffing for `profiles` parents (`.deleted` dir / root markers); our root
  is always passed in explicitly.

## Status

Done (2026-10-18).

## Integration (lead)

- The private atomic write is now `fsio.atomic_write_text(path, text, mode=0o600)` (used by
  `write_active` and `tombstone`). `fsio` is imported inside the helper so `preparse`'s import
  closure stays `errors`/`home`/`profiles` (the fresh-interpreter test still passes).
