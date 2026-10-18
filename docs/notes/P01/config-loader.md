# Task: config loader (P01-10) Phase: 01 Owner: S-config builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/config.py` (deep merge,
`${VAR}` expansion and its env snapshot, cache signature, the full loader and its last-known-good
fallback), `hermes_cli/config_effective.py` (the no-defaults loader, to fold its job into ours),
`hermes_cli/config_read_errors.py` (the failed-read marker). Closed before writing.
Evidence: none.

What it does (own words): `load(scope)` returns the effective config for one profile: built-in
defaults with the user's `config.yaml` deep-merged on top, the model shorthand canonicalised, and
`${NAME}` / `${env:NAME}` in string values expanded from the scope's env view. A user who breaks the
file mid-edit keeps their last good settings (most importantly approval deny rules) instead of
silently falling back to defaults; if there never was a good parse, the defaults are served wrapped
as a `FailedRead` that the writer refuses to save. One loader serves every surface (D-16.32);
presence-sensitive readers ask the view `is_set(key)` instead of using a second loader.

Inputs → outputs:
- `load(scope) -> ConfigView`; `invalidate(path=None)`; `config_path(scope)`.
- `ConfigView`: `get(key, default)`, `is_set(key)`, `source(key) -> "user" | "default" |
  "env-expanded" | None`, `as_dict()` (a fresh deep copy for writers; from a `FailedRead` it is a
  `FailedReadDict` the writer refuses), `user_raw()`, `path`, `failed`, `error`.
- `scope`: anything with `home: Path`, `env: Mapping[str, str]`, `clock` (`now()`).

Files: `src/indus/foundation/config/loader.py`, `tests/unit/foundation/config/test_loader.py`,
`tests/fixtures/config/commented.yaml`, `broken.yaml`, `templated.yaml`, `dotted_models.yaml`.

Algorithm: cache hit when (mtime_ns, size) of the file and every `${NAME}` value used last time are
unchanged → the same immutable view. Otherwise under a process-wide re-entrant lock: read bytes,
`yamlio.load_safe`; empty/missing → `{}`; a non-mapping root counts as a parse failure; canonicalise
(`model: "x"` → `{default: x}`, `model.api_base` → `model.base_url` unless set); merge over
`defaults_tree()` (mappings recurse, a null over a mapping section is ignored, anything else
replaces); expand; validate (warnings logged once per load miss); record a `good` backup. On
failure: one error log naming the line; take a `corrupt` backup; serve the in-memory last good
raw tree, else the newest `good` backup, else `FailedRead(defaults)`; cache the result under the
broken file's signature so the error is not logged on every call.

Edge cases / failure modes:
- `${file:x}` and other non-env sources are left verbatim with one warning; unset names stay
  literal; keys are never expanded; `${}` / `${env:}` left alone.
- a value changed in the scope env invalidates the cache even though the file did not change.
- two scopes (profiles) never share cache entries: the key is the config path.
- the view is immutable: `get` returns deep copies of containers.

Our design differences / decisions:
- no admin overlay (`/etc/indus`, I20): documented hook point in `_merge_admin_overlay`, not built.
- no root `max_turns` move or other legacy key moves (D-16.3).
- no "ensure home skeleton" side effect on load (P01-21 `ensure_home` owns that).

Tests we will write: merge keeps default siblings; broken after good → good values incl. `deny`;
broken first load → `FailedRead`; fresh process (cleared memory) → newest good backup;
`${OPENAI_BASE_URL}` and `${env:...}` expanded and literal when unset; `is_set`; cache invalidated
on mtime/size change and on env change; `source()`; BOM file; null section ignored; non-mapping
root treated as broken.

Compare checklist (filled after the rewrite; reopened the loader functions listed above):
- [x] cache signature + env snapshot (tests: `tests/unit/foundation/config/test_loader.py::test_cache_invalidated_on_file_change`, `::test_cache_invalidated_on_env_change`)
- [x] last-known-good: memory, then newest good backup, then failed-read defaults (tests: `::test_broken_after_good_serves_last_known_good`, `::test_fresh_process_uses_newest_good_backup`, `::test_broken_first_load_is_failed_read`)
- [x] null over a mapping section ignored (test: `::test_null_section_keeps_defaults`)
- [x] `${env:NAME}` and bare names; other sources verbatim (test: `::test_env_expansion`)
- Not taken: the reference warns when a `${env:NAME}` reference is unset; we keep the literal
  silently (the doctor surface in P07 can report unresolved references through `source()`).
- Deliberate differences: one loader with `is_set` instead of three loaders (D-16.32); no admin
  overlay yet (I20); no legacy key moves (D-16.3); expansion reads the scope's env view, never
  `os.environ`.

## Status

Done (2026-10-18). Uses `indus.foundation.errors` (P01-20, core slice) through a test stand-in
until that lands.

## Integration (lead)

- Tests run on the real `Scope` (shared `scope` fixture via `cfg_scope`) and the real
  `foundation.errors`; `ConfigView` satisfies `scope.ConfigSource` (its `get` is positional-only
  there) and is attached to the root scope by `bootstrap.attach_profile_state`.
