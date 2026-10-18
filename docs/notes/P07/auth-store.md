# Task: Auth store (P07-06) Phase: 07 Owner: P07 slice B builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/auth.py` (store
section, about lines 490–900: path guard, root fallback, lock helper, load/save); research
`04-cli-config.md` §3.16.2; traps R04.16, R03.24. Closed before writing.
Evidence: none.

What it does (own words): `<home>/auth.json` is the per-profile record of which providers the
person signed in to and which credentials each provider's pool holds. It is metadata only (labels,
types, sources, priorities, status, expiry times); every token and key lives in the profile's
`SecretStore` (keychain first, P4), under a name derived from the provider and the entry id. A read
that fails at the OS level (permission denied, too many open files, a stalled mount) must raise and
leave the file alone: the store is always read-modify-written, so treating it as empty would erase
every credential on the next save. Only genuinely unparseable content is set aside (copied to
`auth.json.corrupt`) and replaced by an empty store, with a warning.

Inputs → outputs:
- `AuthStore(home, env=…)`: refuses (before touching the disk) when a test run points it at the
  real user's store (`AuthIsolationRefused`, R03.24); refuses `/`, top-level dirs and the install
  tree as the store's folder (`AuthUnsafePath`).
- `read()` → a fresh dict `{version, providers, active_provider?, pool, suppressed_sources?}`.
  Missing file → empty; `OSError` → `AuthStoreUnreadable` (file untouched); bad JSON / bad UTF-8 /
  not an object → `.corrupt` copy, warning, empty.
- `transaction()` → context manager under the cross-process lock (15 s); yields the mutable
  document and writes it back atomically (0600, folder 0700) when it changed. Lock contention past
  the timeout → `AuthStoreLocked` naming the holder PID.
- `inherited(section, key)`: named profiles read the root (default profile) store as a read-only
  fallback for a provider their own store lacks; read errors there count as "nothing".

Files: `src/indus/providers/auth/__init__.py`, `src/indus/providers/auth/store.py`,
`tests/unit/providers/auth/test_store.py`, `tests/fakes/secret_store.py`; shared:
`src/indus/foundation/errors.py` (auth.* codes and classes).

Edge cases / failure modes:
- `chmod 000 auth.json` → raises, bytes and mode unchanged, no `.corrupt` file.
- Truncated JSON, a JSON list, invalid UTF-8 → `.corrupt` copy holds the original bytes.
- A section of the wrong type (`providers: []`) → treated as empty for that section only.
- Two processes adding entries at once → both additions survive (lock + re-read inside it).
- A write that would put a secret-shaped field (`access_token`, `api_key`, …) with a value into
  the JSON raises before anything is written (defence in depth for D-16.78).
- Root fallback is never written and never quarantined.

Our design differences / decisions:
- D-16.78: secrets never in `auth.json` (the reference keeps tokens there).
- Pool section is called `pool` (spec), not the reference's longer name; no legacy shapes are
  migrated (our version starts at 1).
- The lock is P01's `fsio.FileLock` (PID stamped in the lock file), so the contention message
  names the holder.
- The isolation guard reuses P02's test-run detection (`state.connection`), so the auth store and
  the session store agree on what "the real home" and "a test run" mean.

Tests we will write:
- EACCES on read raises and leaves the file; corrupt → `.corrupt` copy and empty store.
- Two subprocesses writing concurrently both land; lock timeout message names the PID.
- A written store never contains the secret value (grep the file); secret-shaped field refused.
- Guard refuses the real root before creating anything; unsafe folder refused.
- Named profile sees the root's provider read-only.

## Decisions for the lead
- Secret names: `SecretStore` only accepts env-style names, so `auth/<provider>/<entry id>` is
  spelled `AUTH_<PROVIDER>_<ENTRYID>` (provider upper-cased, non-alphanumerics → `_`). Entry ids
  are random and unique within the store, so the name is unique. Change in
  `pool_model.secret_name` if the store later accepts paths.

Compare checklist (filled after the rewrite; reopened `hermes_cli/auth.py` 490–900):
- [x] OS read error raises, file untouched (test: `test_store.py::test_os_error_on_read_raises_and_leaves_the_file`)
- [x] Corrupt → copy + empty (test: `test_store.py::test_corrupt_json_is_copied_aside_and_store_starts_empty`)
- [x] Lock timeout names the holder (test: `test_store.py::test_lock_timeout_names_the_holder_pid`)
- [x] Concurrent writers serialise (test: `test_store.py::test_two_processes_writing_at_once_both_land`)
- [x] Real home refused under tests (test: `test_store.py::test_guard_refuses_the_real_store_before_touching_disk`)
- [x] Secret material never written (test: `test_store.py::test_secret_never_lands_in_the_json`, `::test_secret_shaped_field_is_refused`)
- Deliberate differences: see above.
- Not taken: legacy shape migration, write-through of rotated grants into the root store
  (single-use refresh tokens shared across profiles; P08 decides if it needs it).

## Status

Done (2026-10-18). Store, guard, lock and corruption handling landed with tests. Shared changes: `src/indus/foundation/errors.py` (auth.* codes and classes). Gap found in the compare: the reference also stamps an `updated_at`; not taken (nothing reads it). The holder-PID hint relies on P01's `FileLock`, which clears the PID on release, so a dead holder never produces a stale name.

## Integration (lead)

- New read helper `AuthStore.signed_in()`: providers with a non-empty `providers.<id>` record or a
  pool row of `auth_type` `oauth`, this profile's store plus the root's for a named profile. The
  first-run guard, `status` and `dump` use it (no second reader of the JSON).
