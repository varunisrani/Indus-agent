# Task: `SecretStore`, keychain first (P01-16) Phase: 01 Owner: P01 S-env builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/env_loader.py` lines
96–176 (recording key names only — the name-tracking idea). The reference keeps secrets in `.env`;
this task follows decision P4 / D-16.13 instead. Research `08-desktop.md` §security (secrets at rest,
R08.10), architecture row 12. Closed before writing.
Evidence: none.

What it does (own words): one place per profile to keep secrets out of `config.yaml` and, by default,
out of plain files. `auto` (default) uses the OS keychain through `keyring` when a quick check says it
works, else an owner-only JSON file. The check must never put a dialog in front of the user (a broken
macOS keychain prompts on every touch, R08.10): it runs `get` on a sentinel account in a daemon thread
and waits at most 2.0 s on the injected clock; a fail/null backend, an exception or the deadline
marks the keychain unusable for the rest of the process run (cached, never retried), and we fall back
to the file with one warning. `keychain` mode raises `SecretStoreUnavailable` instead; `file` mode
never probes.

Inputs → outputs:
- `SecretStore(backend, index_path)`: `get`, `set`, `delete`, `names()`, `backend_name`,
  `resolve(name, env)` (store first, then the scope's env view), `migrate_from_env(names, env)`
  (copies values once; recorded in the index), `known_values()` (for redaction; never logged).
- Backends: `KeychainBackend(keyring, profile_id)` — service `indus`, account `<profile_id>:<NAME>`;
  `FileBackend(home)` — `<home>/secrets.json`, 0600, atomic, `FileLock`.
- `<home>/secrets.index.json`: names only, plus the migration record.
- `open_secret_store(home, profile_id, *, mode="auto", clock=None, keyring_backend=None)`;
  `probe_keychain(...)`, `reset_probe_cache()` (tests).

Files: `src/indus/foundation/secrets.py`, `tests/contract/foundation/test_secret_store_contract.py`,
`tests/unit/foundation/test_secret_probe.py`; dep `keyring==25.7.0`.

Edge cases / failure modes:
- `keyring.get_keyring()` itself runs inside the probe thread (backend discovery can talk to D-Bus).
- Probe thread that never returns is abandoned (daemon); its late result is ignored.
- Chainer backend with no usable children counts as unusable.
- Keychain errors after a good probe surface as `SecretStoreError` (no silent fallback mid-run: the
  values would split between two stores).
- Corrupt `secrets.json` / index: `SecretStoreError`, file left untouched for the user.
- Bad names (same rule as `.env`) refused. Unicode values kept exactly.
- `repr` of the store and backends shows backend and count, never values.

Our design differences / decisions:
- Keychain by default (D-16.13), reference default is 0600 files.
- Index kept for both backends so `names()` is the same everywhere; the file backend checks the
  index against its own keys on `names()` (union) in case the index was lost.
- `SecretStoreUnavailable(SecretStoreError)` lives in `secrets.py` with code
  `secrets.unavailable` until P01-20's table lists it.

Tests we will write: contract suite over `FileBackend` and `KeychainBackend` (in-memory keyring):
round trip, unicode, overwrite, delete missing, profile isolation, names after reopen, repr masks.
Probe: blocking keyring → fallback after 2.0 s of fake time, not at 1.9 s, warning once; fail
backend → file; null backend → file; raising backend → file; result cached (no second probe);
`keychain` mode with unusable → `SecretStoreUnavailable`; `file` mode never touches keyring;
`resolve` prefers store; file 0600; migration recorded once.

## Decisions for the lead
- The probe waits by polling a `threading.Event` in 10 ms slices against `clock.monotonic()`; with a
  `FakeClock` the test advances time from another thread. No `time.sleep`.
- `SecretStoreUnavailable` code `secrets.unavailable` needs a row in `ERROR_CODES` (P01-20), or the
  class can move into `errors.py`.
- `migrate_from_env` does not edit `.env`; the P07 setup flow removes the line with `envfile`.

Compare checklist (filled after the rewrite; reopened `hermes_cli/env_loader.py` 96–176):
- [x] names tracked in a side file, values never in it (test: `tests/contract/foundation/test_secret_store_contract.py::test_index_holds_names_only`)
- [x] prompt-free probe with deadline and cache (tests in `tests/unit/foundation/test_secret_probe.py`)
- Deliberate differences: keychain first (P4); the reference has no store, only `.env`.
- Not taken: external secret managers (I19, P3).

## Status

Done (2026-10-18). `errors` (P01-20) and `fsio` (P01-07) come from other slices; test stand-ins.

## Integration (lead)

- `SecretStoreUnavailable` moved to `foundation/errors.py` (code `secrets.unavailable` in
  `ERROR_CODES`, with `secrets.backend_failed` / `secrets.unreadable` / `secrets.bad_mode`);
  `secrets.py` re-exports it (`... import SecretStoreUnavailable as SecretStoreUnavailable`).
- Tests use the real `errors`/`fsio`; the `_stubs.install()` calls are gone.
- `tests/conftest.py` gains an autouse `no_os_keychain` fixture: the system keyring lookup yields
  keyring's `fail` backend and the probe cache is reset per test, so no test (bootstrap included)
  reaches the real OS keychain. The `scope` fixture carries a file-backed store.
- `bootstrap.attach_profile_state` opens the root scope's store with the mode from
  `security.secret_store` (see layout-bootstrap.md).
