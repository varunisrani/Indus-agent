# Task: `indus pm`: lock, store and ensure (P24-23) Phase: 24 Owner: wave-37 builder X1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `pm/lock.py` (lock and facts
shapes, pinned identity), `pm/downloader.py` (sources and fallbacks, resume sidecar, hash check,
publication), `pm/artifact_mirror.py` (content-addressed mirror URL), `pm/install.py` (skim: the
`ensure` / `_install` / `env_for` / `drift` flow), `pm/__init__.py` (lazy facade), `pm/AGENTS.md`
(boundary rule); research 10 §3.9. Ours: `safety/child_env.py` (the env builder, R02.22),
`platform/facts.py` (OS family, arch, Termux), `foundation/fsio.py` (atomic writes, file lock),
the import-linter layers in `pyproject.toml` (`indus.pm` sits beside `indus.safety`). Closed
before writing.
Evidence: none.

What it does (own words): one place that knows every external helper program the product may
need (ripgrep, ffmpeg, uv, Node, gh, Chromium for the browser tool, whisper.cpp and llama.cpp
builds where used). Three files describe it:

- package definitions (`src/indus/pm/packages.toml`, ours): what each package is — kind
  (required, default, optional), version, URL template per target, archive format, executable
  path, the folders it adds to `PATH`, dependencies and how newer versions are found;
- the lock (`<root>/pm/lock.json`, machine-written): `{schema, packages: {name: {version,
  artifacts: {target: {url, sha256}}}}}` with targets `any`, `darwin-arm64`, `darwin-x64`,
  `linux-arm64`, `linux-arm64-bionic`, `linux-arm64-musl`, `linux-x64`, `linux-x64-musl`,
  `win32-arm64`, `win32-x64` (`any` is the fallback when a target has no row of its own);
- the facts file (`<root>/pm/facts.json`): what this install actually has — per package the
  version, target, archive hash, store entry, tree digest and exports.

`ensure(name)` reconciles one package (and its dependencies) to the lock: when the facts already
match the lock and the store entry exists it only stats (the lock and facts are cached by their
stat signature); otherwise it fetches the archive, verifies its sha256 whichever origin served it
(the pinned URL, then the content-addressed mirror `<mirror>/sha256/<hex>` when configured, local
bundle folders first when given), unpacks into a fresh staging folder under `<root>/pm/store/`,
checks the executable is there, renames the staging folder into its content-addressed entry
(`<name>-<version>-<sha256[:16]>`) and records the fact. It returns a `Runner`: the executable,
the entry, the exports, and an environment composed by the caller's env builder
(`safety.child_env`, passed in because `indus.pm` may not import `indus.safety`; R02.22).

Downloads are resumable: a partial `<sha256>.part` is continued with an HTTP `Range` request and
restarted when the server answers 200; every request has a timeout. A hash mismatch discards the
bytes and moves to the next origin, so a bad primary falls back to the mirror, which must still
verify. Redirects must stay on https.

`indus.pm` resolves its exports lazily, so importing it (boot, `--help`) never loads the fetcher
or httpx. Boundary rule: ruff `banned-api` forbids importing `indus.pm.fetch`, `indus.pm.store`
and `indus.pm.lock` outside `indus.pm`; consumers ask the facade (`ensure`, `tool_path`,
`env_for`) for a tool, never run pip or uv themselves.

Inputs → outputs: `ensure(scope, name, *, transport=…, origins=…)` → `Runner` or `PmError`
(not locked, no artifact for this target, every origin failed with each reason, unpack or
verification failure); files under `<root>/pm/` (`lock.json`, `facts.json`, `store/`,
`downloads/`, `.pm.lock`).

Files: `src/indus/pm/__init__.py` (facade), `src/indus/pm/lock.py`, `src/indus/pm/store.py`,
`src/indus/pm/fetch.py`, `src/indus/pm/packages.toml`, `src/indus/pm/core.py` (definitions,
targets, `Pm`, `ensure`, `Runner`), `tests/fakes/pm_origin.py`, `tests/unit/pm/test_lock.py`,
`tests/unit/pm/test_store.py`, `tests/unit/pm/test_fetch.py`; shared: `pyproject.toml` (ruff
`banned-api` entries), `foundation/config/schema.py` + its test (`pm.mirror_url`,
`pm.timeout_s`).

Edge cases / failure modes:
- Unreadable or wrong-schema lock/facts: lock → error naming the file; facts → treated as empty
  and the bytes kept beside it as `facts.json.corrupt` (never silently overwritten).
- Archive with absolute paths, `..`, links pointing out, or device files: refused.
- Interrupted unpack (crash, exception): only a `.staging-*` folder is left; the entry name never
  exists half-filled; `gc` removes stale staging folders.
- Two processes installing: one file lock per root; a racing entry that already exists is kept and
  the staging copy dropped.
- A target the package does not ship: clear error, nothing downloaded.
- Server ignores `Range` (200): restart from zero; `416`: the part is checked as complete.

Our design differences / decisions:
- Our package list and pins (D-16.52), no reference lock content; no Python environment,
  node_modules or plugin packages (the product ships as a wheel; pm manages helper binaries only).
- The lock is per root and written by `indus pm lock`; a packaged `indus/pm/lock.json` (release
  build) would be used when no root lock exists — none is shipped yet.
- A hash mismatch at the primary falls back to the mirror (the reference stops on a mismatch).
- One connection per download (no parallel ranges).
- The runner's environment is built by the caller's builder (layer rule).

Tests we will write:
- Lock shape round trip, target fallback to `any`, wrong schema refused, corrupt facts kept.
- Fetch: primary hash mismatch → mirror verifies; mirror also bad → error naming both; resume
  with `Range`; 200 restart; https-only redirects; timeout passed on every request.
- Store: unpack formats and strip, unsafe members refused, interrupted unpack leaves no entry,
  tree digest, `ensure` on satisfied facts does nothing but stat (reads and network forbidden).
- Runner env from the env builder (credential names stay out, pm dirs first on `PATH`).
- Banned-api: ruff reports a seeded `indus.pm.fetch` import under `src/indus/tools/`.

## Decisions for the lead
- Lock location `<root>/pm/lock.json`; packaged fallback path reserved (`indus/pm/lock.json`).
- `pm.mirror_url` (default empty: no mirror) and `pm.timeout_s` (60) config rows.
- ffmpeg is fetched from a third-party static build (GPL binary, run as a separate process only,
  never linked); owner may prefer another origin.

Compare checklist (filled after the rewrite; reopened `pm/lock.py`, `pm/downloader.py`,
`pm/artifact_mirror.py`, `pm/install.py`, `pm/__init__.py`):
- [x] Lock and facts shapes, target fallback to `any`, wrong schema refused, unreadable facts kept
  as `.corrupt` (test: `tests/unit/pm/test_lock.py`)
- [x] Installed identity = (version, target, archive hash) and the entry exists (test:
  `test_store.py::test_a_new_pin_installs_a_new_entry`)
- [x] Mirror only with a full sha256, content-addressed key (test:
  `test_fetch.py::test_mirror_url_is_content_addressed`)
- [x] https only, redirects included (test: `test_fetch.py::test_redirects_stay_on_https`,
  `::test_a_plain_http_url_is_refused`)
- [x] 4xx / network failure at the primary → mirror (test:
  `test_fetch.py::test_a_missing_primary_falls_back_to_the_mirror`); every origin listed when all
  fail (test: `::test_the_mirror_must_verify_too`)
- [x] Resume with ranges; restart when ranges are ignored; incomplete bodies retried (test:
  `test_fetch.py::test_a_dropped_connection_resumes_with_a_range_request`,
  `::test_a_server_that_ignores_ranges_restarts_from_zero`, `::test_a_complete_part_answered_416_is_checked_and_kept`)
- [x] Cached blob re-hashed before reuse (test: `::test_a_cached_blob_is_reused_without_a_request`)
- [x] Staged entry checked before publication; publication by rename; nothing half-made (test:
  `test_store.py::test_an_interrupted_unpack_leaves_no_half_entry`,
  `::test_a_killed_unpack_leaves_only_a_staging_folder`, `::test_publish_keeps_an_entry_that_won_the_race`)
- [x] `env_for` never installs (test: `test_store.py::test_the_facade_ensures_through_one_manager_per_root`)
- [x] Dependents first on `PATH` (test: `test_store.py::test_dependencies_install_first_and_join_the_path`)
- [x] Lazy facade (test: `test_store.py::test_importing_the_facade_loads_no_internals`)
- [x] Satisfied `ensure` only stats (test: `test_store.py::test_ensure_on_a_satisfied_facts_file_only_stats`)
- [x] Boundary rule (test: `test_store.py::test_banned_api_refuses_pm_internals_outside_pm`)
- [x] R02.22 (test: `test_store.py::test_the_runner_environment_comes_from_the_env_builder`)
- [x] Gap found while testing: the facts cache held the pre-install read, so the next `ensure`
  re-read the file — fixed by `Pm.save_facts` priming the stat cache, pinned by
  `::test_ensure_on_a_satisfied_facts_file_only_stats`
- Deliberate differences: a hash mismatch at the primary falls back to the mirror (the reference
  stops); one connection per download; the partial is bound to the pinned hash, not an ETag (a
  changed upstream shows as a mismatch and restarts from zero); entries are named by the archive
  hash, not by version and target; exports are stored relative to the entry instead of `{{store}}`
  templates.
- Not taken: several archives per target (llama.cpp's CUDA runtime split), the Python
  environment, node_modules and plugin packages, the "lazy installs allowed" policy for sealed
  installs, a writable second store root, pause/progress callbacks, `.previous-*` restore points
  (a failed re-install simply re-installs on the next `ensure`). Follow-ups for the lead.

## Status

Done (2026-10-21). Shared: `pyproject.toml` (ruff `banned-api` for `indus.pm.fetch` /
`indus.pm.store` / `indus.pm.lock`, per-file ignores for `src/indus/pm/**` and the pm tests),
`src/indus/foundation/config/schema.py` + `tests/unit/foundation/config/test_schema.py`
(`pm.mirror_url`, `pm.timeout_s`), `docs/internals/traps-ledger.md` (R02.22). No ACC-S9 rows exist
in the acceptance map (P2). No new library (httpx is already a core dependency).

## Integration (P24 integrator, 2026-10-21)

The facade moved from `indus/pm/__init__.py` to `indus/pm/api.py`: layer packages hold only a
one-line docstring (`tests/unit/test_layers.py`). Callers use `from indus.pm import api`
(`ensure`, `tool_path`, `env_for`, `manager`); the ruff banned-api messages point there. Importing
`indus.pm.api` still loads no pm internals (`test_importing_the_facade_loads_no_internals`).
