# Task: Environment registry + lifecycle (P05-04) Phase: 05 Owner: wave-1 builder D

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/terminal_tool_lifecycle.py`
(whole: reaper, teardown, eviction, lazy bring-up, exit cleanup), `tools/terminal_tool_backends.py`
(builders table, `_create_environment`, the unavailable-reason record), `tools/terminal_scope.py`
(whole: per-profile terminal policy and refusal), `tools/terminal_tool.py` (env key resolution
`_resolve_container_task_id`, creation under a per-key lock, `_degraded_result`, the reaper thread,
the "disabled" creation error). Test names only of `tests/tools/test_terminal_degraded_mode.py`,
`test_terminal_environment_recreated_notice.py`. Spec: research `02-tools.md` trap R02.8, D-16.75.
Closed before writing.
Evidence: none.

What it does (own words): finds or makes the environment a command runs in, keeps it while it is
used, and gives it back.

- Key (`EnvKey`): always starts with the profile home, so two profiles never share an environment
  (R02.8). Calls with per-task overrides (image, backend) get their own task key; otherwise the
  session id (or `default`); host-local control calls (Indus's own commands) a separate `host` key.
- `await registry.acquire(scope, …)`: under one asyncio lock per key, re-check, then create from
  the backend entry. Every use stamps the activity time on the registry clock.
- Backend selection: `terminal.backend` from the call's `Scope` config (an override may name
  another); never from process env (D-16.75: no `TERMINAL_*` bridge). Unknown name or a missing
  optional dependency → `BackendStatus(status="disabled", error=…)`.
- `await registry.run(scope, command, …)`: acquire + execute. A connection-class failure
  (`EnvironmentConnectionError`) evicts the environment (cleaned up) and returns `{output: "",
  exit_code: -1, status: "degraded", reason, retry_hint, error}`; the next call creates a fresh one.
  `terminal.degraded_mode: fail` returns `status: "error"` with the error instead (still evicted).
- Reaper: a task that wakes every 60 s and tears down environments idle longer than
  `terminal.lifetime_s` (300) — unless a background process of that key is alive, then the
  activity time is refreshed. Teardown runs outside the table lock.
- `shutdown()`: stop the reaper, close the foreground exit fence, clean every environment with a
  15 s bound each.
- `BackendEntry(name, factory, flags, probe)` rows: P11/P21 add entries, nothing else changes.

Inputs → outputs: scope + task/session ids + command → `ExecResult` or `BackendStatus`.

Files: `src/indus/backends/registry.py`, `tests/unit/backends/test_registry.py`,
`tests/contract/backends/__init__.py`, `tests/contract/backends/conftest.py`,
`tests/contract/backends/test_execution_contract.py`, `tests/fakes/backend/environment.py`
(fake `Environment`); shared: `src/indus/foundation/config/schema.py` (+ its pinned-defaults
test), `docs/plan/16-what-we-change.md` (D-16.75 "Decided in").

Edge cases / failure modes:
- two concurrent first calls on one key → one creation.
- creation raising a connection error → degraded, nothing cached.
- unresolved scope → `ScopeUnresolved` (refuse, never the launch profile).
- cleanup that hangs → abandoned after 15 s; one that raises → logged.
- reaper sees a key whose background process is alive → refreshed, kept.

Our design differences / decisions:
- asyncio locks and a reaper task on the injected clock instead of threads and a 1 s sleep loop.
- Every key carries the profile home (the reference qualified keys only for routed profiles).
- Fail mode also evicts (a dead connection should not be reused); the reference returned before
  evicting.
- Not taken: docker shared/persistent container keys and session isolation (P11), disk-usage
  warning for singularity scratch, file-ops cache invalidation (slice B calls `release`).

Tests we will write: two profiles never share; per-task override key; host-local key separate;
one creation under a race; activity stamp; idle env reaped at 301 s (fake clock) and kept while a
background process runs; reaper task wakes every 60 s; degraded result + eviction + recreation;
fail mode; disabled for a missing dependency and an unknown backend; shutdown cleans all with a
bounded wait; scope config chooses the backend, env vars do not. Contract suite parametrised over
the registered backends (local + fake): marker/cwd, odd output, backstop, interrupt, stdin,
bounded vs unbounded.

## Decisions for the lead
- Fake `Environment` lives at `tests/fakes/backend/environment.py`: `tests/fakes/backend/` is
  already a package (P00 skeleton), so `tests/fakes/backend.py` cannot exist beside it.
- Config keys added (shared `schema.py`): `terminal.lifetime_s` 300, `terminal.degraded_mode`
  `warn`, `terminal.persistent_shell` true, `terminal.temp_dir` "", `terminal.daemon_term_grace_s`
  2.0, `tool_output.max_chars` 50,000.

Compare checklist (filled after the rewrite; reopened `tools/terminal_tool_lifecycle.py`,
`tools/terminal_tool_backends.py` (`_create_environment`, builders, reject reasons),
`tools/terminal_scope.py`, the key resolution / creation / degraded paths of `tools/terminal_tool.py`):
- [x] R02.8 keys carry the profile home; task overrides; host-local key (tests:
  `tests/unit/backends/test_registry.py::test_two_profiles_never_share`, `::test_key_kinds`)
- [x] per-key lock + re-check, one creation (test: `::test_one_creation_under_a_race`)
- [x] activity stamp, reap at lifetime, refresh while a background process lives, wake every
  60 s, teardown outside the table (tests: `::test_idle_env_reaped_after_its_lifetime`,
  `::test_env_with_a_live_background_process_is_kept`, `::test_reaper_task_wakes_every_minute`)
- [x] degraded result shape, eviction, recreation; fail mode; creation failure caches nothing
  (tests: `::test_degraded_result_evicts_and_the_next_call_recreates`, `::test_fail_mode_returns_the_error`,
  `::test_connection_failure_while_creating_caches_nothing`)
- [x] disabled: missing package (ImportError), unknown backend with the known names, probe
  (test: `::test_disabled_backends`)
- [x] D-16.75 backend from the scope config only; unresolved scope refuses (tests:
  `::test_backend_comes_from_scope_config_never_env_vars`, `::test_unresolved_scope_refuses`)
- [x] shutdown: reaper stopped, every env cleaned with a 15 s bound (test: `::test_release_and_shutdown_clean_with_a_bound`)
- [x] gap found: the reference refuses when the profile's terminal policy cannot be read; with
  defaults after a failed config read we would have fallen back to `local` — now refused
  (`disabled`), pinned by `::test_failed_config_read_refuses_instead_of_guessing`
- [x] contract suite over every registered backend (local + fake): `tests/contract/backends/test_execution_contract.py`
- Deliberate differences: asyncio locks and a reaper task on the injected clock; every key
  carries the profile home; fail mode also evicts; status results are values, not JSON text
  (`BackendStatus.to_dict()` gives the wire shape).
- Not taken: docker shared/persistent container keys, session isolation and `force_remove`
  (P11); scratch-dir disk warning and orphan scratch removal (P11 singularity); file-ops cache
  invalidation (slice B calls `release`/watches eviction); the remembered "last unavailable
  reason" for the setup UI (P07 doctor can call the probes).

## Status

Done (2026-10-18). Files: `src/indus/backends/registry.py`, `tests/unit/backends/test_registry.py`,
`tests/contract/backends/{__init__,conftest,test_execution_contract}.py`,
`tests/fakes/backend/environment.py`. Shared: `src/indus/foundation/config/schema.py` (five
`terminal.*` keys + `tool_output.max_chars`) and `tests/unit/foundation/config/test_schema.py`
(pinned defaults); `docs/plan/16-what-we-change.md` (D-16.75 "Decided in"). Follow-up for the
host (P06/service): call `EnvironmentRegistry.shutdown()` on process exit.
