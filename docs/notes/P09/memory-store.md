# Task: Memory store (P09-01) Phase: 09 Owner: P09 slice A builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/memory_tool_store.py`
(delimiter, render, matching, batch budget, drift guard, lock); `website/docs/user-guide/features/memory.md`
not opened (skim only; the phase file carries the numbers). Closed before writing.
Evidence: none.

What it does (own words): each profile keeps two small plain-text stores under `<home>/memories/`:
`MEMORY.md` (the agent's own notes, target `memory`) and `USER.md` (what the agent knows about the
user, target `user`). A file is a list of entries joined by a line holding only `§`
(`"\n§\n"`); an entry may span lines. The used size of a store is the length of the joined text,
delimiters included, and each store has a character limit (2,200 notes / 1,375 profile, from
`memory.notes_char_limit` / `memory.profile_char_limit`). Reading drops empty entries and exact
duplicates (the first copy wins). Every write happens under an exclusive lock on a sibling
`<file>.lock` (created 0600, opened with `O_NOFOLLOW`, so a planted symlink is refused), re-reads the
file under the lock, applies the change to a fresh list and replaces the file atomically (temp file +
rename through `foundation.fsio`). The change itself is a pure function over the entry list
(`apply_ops`) and the limit is checked on the final list only, so one batch can trim and add at
capacity. `render_snapshot()` gives the text the prompt shows: a header that names the store and
its usage (`"<pct>% — <used>/<limit> chars"`), then the entries separated by `§` lines.

Inputs → outputs: home, limits, enable flags, ops `[{action: add|patch|remove, content, old_text,
pinned_entry?}]` → `Result{ok, entries, message, kind, details}`; files `MEMORY.md`, `USER.md`,
`*.lock`, `*.bak`; rendered snapshot text.

Files: `src/indus/learning/memory/__init__.py`, `src/indus/learning/memory/store.py`,
`tests/unit/learning/memory/test_store.py`. Shared: `src/indus/foundation/config/schema.py`
(`memory` section rows of slice A), its test, `src/indus/learning/__init__.py` (docstring only, see
decisions).

Edge cases / failure modes:
- file missing → empty store; file present but not valid UTF-8 / unreadable → every write refused
  (`unreadable`), nothing rewritten (a write would wipe it);
- a BOM at the start is ignored (`utf-8-sig`);
- file over its limit when loaded (hand edit, older limit) → loaded with one WARNING; adds and growing
  changes are refused until the store is back under the limit; shrinking changes still work;
- one entry alone longer than the whole limit (free text without delimiters) → every rewrite is
  refused, `<file>.bak` holds the current bytes, the result carries repair steps; the file is not
  touched;
- symlinked lock file → the write is refused (`lock_refused`), nothing changes;
- two writers in one process (threads) or two processes → serialised by the lock; each re-reads the
  file under the lock, so no entry is lost;
- one process per home is assumed for the frozen snapshot (the prompt block of P09-04); the lock keeps
  concurrent writers from different processes safe, but a second process's snapshot is its own.

Our design differences / decisions:
- our names: targets `memory` / `user` (model-facing) map to config keys `notes_*` / `profile_*`;
  header text and usage wording are ours (`<saved-notes usage="…">` blocks, see P09-04);
- the drift backup is `<file>.bak` (one file, replaced each time), not a time-stamped name;
- the pure op engine lives here (`apply_ops`), the tool (P09-02) adds wording, retry cap and staging.

Tests we will write:
- round trip with multi-line entries; duplicate dropped on load; limit counts delimiters;
- over-limit file loads with a warning, refuses add, accepts a shrinking remove;
- oversized single entry → `.bak` written, rewrite refused, file bytes unchanged;
- unreadable file refused; BOM stripped;
- 8 threads × 5 adds through separate store objects → every entry present (lock serialises);
- symlinked lock file refused; new lock file mode 0600;
- snapshot rendering (header, usage, `§` lines; empty store renders nothing).

## Decisions for the lead
- Config keys are `memory.notes_char_limit`, `memory.profile_char_limit`, `memory.notes_enabled`,
  `memory.profile_enabled`, `memory.write_approval` (phase file names). The model-facing targets
  stay `memory` / `user`.
- A limit below 1 from config is ignored (default used) and logged once.

Compare checklist (filled after the rewrite; reopened `tools/memory_tool_store.py`):
- [x] delimiter `\n§\n`, used size counts delimiters (test: `test_store.py::test_limit_counts_delimiters`)
- [x] duplicates dropped on load, first wins (test: `test_store.py::test_duplicate_dropped_on_load`)
- [x] over-limit on load: warn, keep entries, refuse adds (test: `test_store.py::test_over_limit_file_loads_and_refuses_add`)
- [x] whole-store-sized entry → backup + refusal, file untouched (test: `test_store.py::test_oversized_entry_writes_bak_and_refuses`)
- [x] unreadable file never treated as empty on write (test: `test_store.py::test_unreadable_file_refuses_writes`)
- [x] lock on a sibling file, `O_NOFOLLOW`, 0600 (test: `test_store.py::test_symlinked_lock_refused`, `test_lock_file_mode`)
- [x] batch checked on the final list (test: `test_store.py::test_batch_trim_and_add_at_capacity`)
- Deliberate differences: one `.bak` file instead of a time-stamped one; a shrinking change is allowed
  on an over-limit store (the reference refuses only adds; same effect for adds); a batch that would
  empty a non-empty store is allowed here (the reference refuses it; we keep `remove` semantics
  uniform — the batch must be explicit about every entry it drops).
- Not taken: a refusal of batches that empty the store (see above); per-call metrics context var.

## Status

Done (2026-10-19). Shared: `foundation/config/schema.py` (+ test) gains the `memory` section rows.

## Integration (lead, P09 wave 1 landing, 2026-10-19)

Compare redone at integration (lead reopened `tools/memory_tool_store.py`, then closed it):
delimiter `\n§\n` and delimiter-counted size, defaults 2,200 / 1,375, first-copy-wins dedupe on load,
over-limit load warns and keeps entries, sibling lock file with `O_NOFOLLOW` and 0600, exact-entry
match ahead of substring hits, batch budget on the final list: all match. The reference also folds
dashes and whitespace when matching `old_text`; the phase asks only for quote folding, so ours stays
narrower (noted, not a gap). Differences recorded as D-16.137.
