# Task: Command lookup (P01-02) Phase: 01 Owner: P01 S-core builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_platform/resolver/base.py`
(all), `hermes_platform/resolver/core.py` (all), `hermes_platform/resolver/known_dirs.py` (all),
`hermes_platform/resolver/availability.py` (version compare only). Closed before writing.
Evidence: R09.18.

What it does (own words): finds an executable by name without side effects. Three tiers: `locate`
only stats files (explicit path, then the lookup PATH, then the known install dirs), `inspect` may
read a bounded prefix of the winning file, and `probe` alone may open a socket (`NETWORK`) or spawn
(`DIAGNOSTIC`), always fresh. All well-known install directories live in one module; a ratchet test
fails when `shutil.which(` or a known-dir literal shows up elsewhere under `src/`.

Inputs → outputs: `locate(name, LookupContext)` → `Resolution{kind, candidates}`; `inspect(res)` →
`Inspection`; `probe(res, effort=…, endpoint=…, deadline=3.0)` → `Probe` (not truthy).

Files: `src/indus/platform/{shapes,known_dirs,locate}.py`, `tests/unit/platform/test_locate.py`,
`tests/unit/platform/test_lookup_ratchet.py`, `tests/unit/platform/lookup_ratchet_allow.json`.

Edge cases / failure modes:
- relative explicit path (`./x`, `bin/x`) → never trusted (would resolve against cwd).
- a non-executable file of the same name earlier on PATH is skipped.
- `path=""` searches nothing (known dirs still apply).
- Windows: names tried with each `pathext` suffix from the context, never from the process env.
- probe with `LOCAL` must not touch the injected connector or runner.

Our design differences / decisions:
- Own PATH walk instead of `shutil.which` (no PATHEXT read from the process env, every candidate
  recorded).
- `LookupContext.path` is `PathSource.UNSET`, `PathSource.AMBIENT` or a string.
- `inspect` reports the shebang interpreter and size of the winning file (bounded read, 64 KiB).
- Known-dir tables take an `os_family` argument so the ratchet can list every literal for every OS.

Tests we will write: executable vs non-executable on PATH; relative explicit path refused; absolute
explicit path accepted; `path=""`; known-dir hit after PATH miss with both candidates; `bool(Probe)`
raises; LOCAL probe never connects; NETWORK probe uses the connector; ratchet clean on `src/` and
flags a planted `shutil.which` and a planted dir literal.

## Decisions for the lead
- Package-runner resolution (`npx`/`uvx` style) is not in the P01 row and is left out.

Compare checklist (filled after the rewrite; reopened `resolver/core.py`, `base.py`, `known_dirs.py`):
- [x] explicit path only if absolute + executable regular file (test:
  `tests/unit/platform/test_locate.py::test_relative_explicit_path_refused`)
- [x] PATH then known dirs, every candidate kept in order (test: `test_known_dir_after_path_miss`)
- [x] `Probe.__bool__` raises (test: `test_probe_is_not_truthy`)
- [x] known-dir tables: Homebrew (mac), user local bin, cargo, npm/bun/volta, vendored bin, snap,
  flatpak, mac protected folders, Windows user program dirs
- Deliberate differences: no machine-marker, home-glob or service-definition tables yet (no P01
  consumer); vendored bin is `<home>/bin` passed in by the caller, not a literal.
- Not taken: app declarations, availability, liveness probe of app runtime files (later phases).

## Status

Done (2026-10-18).
