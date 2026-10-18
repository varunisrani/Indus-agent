# Task: config writer, the single seam (P01-11) Phase: 01 Owner: S-config builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/config.py` (template
preservation, default stripping, `save_config`, the fail-closed raw read, the atomic write/replace
wrappers), `utils.py` (the round-trip save that merges a new state onto the parsed document),
`scripts/check_config_yaml_writers.py`. Closed before writing.
Evidence: none.

What it does (own words): `save(scope, data, *, replace=False)` is the only code that writes
`config.yaml`. It refuses in a managed install, refuses to persist a failed read, and re-reads the
file fail-closed: an unreadable or unparsable file is an error, never `{}` (an empty read would let
the default-stripping pass delete user sections, R04.3). The change is applied onto the round-trip
document so comments, key order and quoting survive (R04.5); `${VAR}` templates are kept when the
value equals their expansion (no secrets leak into the file); leaves equal to their default are not
written unless the user already had that path (R04.4); `_config_version` is always kept. A new
file gets the commented example from P01-09 after the user's values, never a default tree.

Inputs → outputs:
- `save(scope, data: ConfigView | Mapping, *, replace=False) -> Path`. Additive (default): `data`
  is deep-merged over the current file, so omitting a section keeps it; `replace=True`: `data` is
  the complete desired state and omitted keys are deleted. A `ConfigView` is saved as its
  `as_dict()`.
- errors (from `indus.foundation.errors` `ConfigError`, our subclasses in
  `config/errors.py`): `ConfigManaged` (`config.managed`), `ConfigFailedRead`
  (`config.failed_read`), `ConfigUnreadable` (`config.unreadable`).

Files: `src/indus/foundation/config/writer.py`, `src/indus/foundation/config/errors.py` (new, small),
`tests/unit/foundation/config/test_writer.py`.

Algorithm (under the loader's process lock and a `FileLock` on `config.yaml.lock`): refuse managed
/ failed read → read raw bytes fail-closed → `pre-write` backup → canonicalise both sides → merge
(additive) or take as is (replace) → restore templates → strip defaults (explicit raw paths kept;
emptied sections dropped) → stamp `_config_version` if absent → if the file had the scalar
`model: x` and only `default` remains, write the scalar back → apply onto the round-trip document
comparing meanings (a node whose YAML 1.1 value is unchanged keeps its text, comments and quoting;
`on` stays `on`) → dump → append the example on create → `fsio.atomic_write_text` (new 0600,
existing mode kept) → invalidate the loader cache.

Edge cases / failure modes:
- chmod 000 file / invalid YAML / list root → `ConfigUnreadable`, bytes on disk unchanged.
- a key spelled differently in the file (`on:` as a key) is matched by position against the 1.1
  reading of the same text, so it is not duplicated.
- bool vs int (`True == 1`) is treated as a change.
- empty existing file counts as create (example appended).

Our design differences / decisions:
- additive mode merges instead of raising on omissions: same safety (nothing is deleted by
  omission), friendlier for patch callers; deletions need `replace=True`.
- existing file mode preserved (the reference forces 0600 after write); new files 0600.
- a cross-process `FileLock` around the read-modify-write (the reference relies on an in-process
  lock only).
- ratchet: a test fails when any module under `src/` other than this writer calls a `yamlio.dump_*`
  function in code that names `config.yaml` / a config path (R04.5); proven on a planted temp copy.
- managed check reads `INDUS_MANAGED` from the scope env and `<home>/.managed`; when P01-03's helper
  lands the lead may swap `_is_managed` for it.

Tests we will write: commented fixture set one key → all comments/order kept; default-equal value
not written unless present before; `${KEY}` template kept; chmod 000 and invalid YAML →
`ConfigUnreadable` and file untouched; `FailedRead` (view and dict) refused; managed env and marker
refused; additive save missing a section keeps it; replace deletes it; create writes example; mode
0640 kept; version stamped; scalar model shorthand kept; ratchet on `src/` and planted copy.

## Decisions for the lead
- `config/errors.py` holds `ConfigUnreadable`, `ConfigFailedRead`, `ConfigManaged` as subclasses of
  P01-20's `ConfigError` with the codes from its table. If the core slice already defines these
  names in `foundation/errors.py`, replace this module by re-exports.

Compare checklist (filled after the rewrite; reopened the save path listed above):
- [x] managed refusal, failed-read refusal, fail-closed re-read (tests: `tests/unit/foundation/config/test_writer.py::test_managed_marker_refused`, `::test_failed_read_refused`, `::test_unreadable_file_refused_untouched`, `::test_invalid_yaml_refused_untouched`)
- [x] template preservation (test: `::test_env_template_preserved`)
- [x] default stripping with explicit-path preservation and `_config_version` kept (tests: `::test_default_equal_value_not_written`, `::test_default_equal_value_kept_when_present_before`)
- [x] round-trip merge keeps unchanged nodes; bool/int flip is a change (tests: `::test_set_one_key_keeps_comments_and_order`, `::test_bool_int_flip_is_a_change`)
- [x] example appended on create only (test: `::test_create_writes_example`)
- Deliberate differences: no admin-pinned key stripping (no overlay yet, I20); additive mode merges
  instead of raising; existing mode kept; cross-process lock added; a symlinked `config.yaml`
  (dotfile managers) is written through to its target, a dangling one is replaced.
- Not taken: matching list items by their `name` field when restoring templates (the reference
  does this for its custom-provider list; our `providers` is a mapping, so positional matching
  covers every list we have); comparing against the expansion made at load time instead of the
  scope's current env.

## Status

Done (2026-10-18). New file `src/indus/foundation/config/errors.py` (see decision above).

## Integration (lead)

- `config/errors.py` now only re-exports `ConfigError`, `ConfigUnreadable`, `ConfigFailedRead`,
  `ConfigManaged` from `foundation/errors.py` (same codes; `Cls(message, detail=, hint=)` still
  works through `IndusError`'s default-code form).
- The private `_is_managed` is replaced by `home.is_managed(scope.home, scope.env)` (same rule:
  truthy `INDUS_MANAGED` or `<home>/.managed`).
- Config tests: the `_stubs` module and `TestScope` are gone. `cfg_scope` (config `conftest.py`) is a
  thin wrapper over the shared `scope` fixture: empty `EnvView`, the suite's `clock`, home 0700;
  `cfg_home` is its home. Env variants use `cfg_scope.with_env(EnvView({...}))`.
