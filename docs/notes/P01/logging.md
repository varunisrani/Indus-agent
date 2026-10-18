# Task: Logging (P01-18) Phase: 01 Owner: P01 builder, slice S-log

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_logging.py` (setup function,
session record factory, component filters, the routing file handler, the queue functions, the
rotating handler's external-rotation check). Research: `docs/plan/research/03-state.md` §3.27,
§3.1 (process home vs routed home). Closed before writing.
Evidence: none.

What it does (own words): one call, `setup(scope, mode, verbose)`, wires the root logger for the
whole process. Loggers only drop records on an in-process queue; one listener thread writes them, so
an event loop never waits on disk or rotation. Each record is stamped, in the emitting thread, with
the session id, turn id and the home of the scope that was current when it was logged. The listener
routes every record to the files under **that** home's `logs/` dir, so a host serving several
profiles never mixes one profile's lines into another's files (R03.23). Every file and console line
passes through the scope's `Redactor` (P01-17) at write time; whether redaction is on is read per
call from the scope, never snapshotted at import (R04.13). In `stdio` mode (protocol servers)
nothing is ever written to stdout (R09.14).

Inputs → outputs: `setup(scope, mode="cli"|"gateway"|"gui"|"stdio", verbose=False)`; config keys
`logging.level` (INFO), `logging.max_size_mb` (5), `logging.backup_count` (3) from `scope.config`
when loaded, else those defaults → files `<home>/logs/agent.log` (configured level, size × count),
`errors.log` (WARNING+, 2 MiB × 2), `gateway.log` (mode gateway only, INFO+, 5 MiB × 3, gateway
loggers only), `gui.log` (mode gui only, INFO+, 10 MiB × 5, web/TUI-bridge loggers only). Files
0600, the `logs` dir 0700. `log_tags(session_id=, turn_id=)` binds the ids for the current
context. `flush()`, `drain(timeout=1.0)`, `release_home(home)`, `shutdown()`.

Files: `src/indus/foundation/log.py`, `tests/unit/foundation/test_log.py`,
`tests/integration/foundation/test_log_routing.py`, test stand-ins in
`tests/unit/foundation/_stubs_log.py` (removed by the lead once P01-06/17/19/20 land).

Edge cases / failure modes:
- `setup` called twice (re-home after `-p`, tests): the previous listener is stopped and our
  handlers removed first; foreign handlers stay, except stdout handlers in `stdio` mode.
- A record logged with no scope bound (library thread) routes to the process home.
- A routed home that was deleted or tombstoned: no dir is created for it (R04.25); the record
  goes to the process home's files instead and the routed handlers are released.
- `logs/agent.log` moved away by logrotate or by hand: the next write sees the inode change (or
  the missing file) and reopens at the path.
- A blocked handler: `drain(timeout)` gives up after the timeout and returns; it never joins the
  listener without a bound. `flush()` waits for the queue to empty (normal exits).
- Records are not pre-formatted on the queue (a shallow copy is queued), so the redaction and the
  format happen once, on the listener, with the record's own scope.
- Unknown `logging.level` text → INFO and one warning; non-numeric sizes → defaults.
- Windows: `os.open` mode bits are ignored; no cross-process rotation lock in P01 (one writer per
  home is the assumption until the gateway phase).

Our design differences / decisions:
- Context, not thread-locals: session/turn ids live in `ContextVar`s, so asyncio tasks keep
  their own tags; the home comes from `current_scope()` (P01-06), falling back to the process
  scope when none is bound.
- Routing is the only file path: there is no separate "single home" handler that later gets
  upgraded to a router; every home (the process one included) gets its handler set lazily.
- The record factory is installed by `setup`, not at import (no import-time side effects);
  records created before `setup` still format because the formatter supplies defaults.
- Group-readable files for managed installs are not built here (follow-up once
  `home.secure_dir` lands; see Decisions).
- Line shape is ours: `<time> <LEVEL> <logger> [<session>:<turn>]: <message>`.

Tests we will write:
- unit: two asyncio tasks under two scopes → each scope's `agent.log` holds only its own lines;
  WARNING lands in `errors.log` too; stored secret masked in the file; `stdio` mode leaves fd 1
  empty (and removes a pre-existing stdout handler); tiny `max_size_mb` → `.1`, `.2`, `.3`
  backups and no more; external `mv agent.log` → next record recreates it; `drain(0.1)` with a
  blocked writer returns in bounded time; session/turn tags appear; gateway/gui files only in their
  mode and only for their loggers; noisy loggers pinned at WARNING; files 0600 and dir 0700;
  deleted/tombstoned routed home falls back to the process home and creates nothing.
- integration: routing across two real temp profile homes with threads carrying scopes, and
  `release_home` closes the files.

## Decisions for the lead
- Logger prefixes for the mode files: `gateway.log` takes `indus.gateway*`; `gui.log` takes
  `indus.web*`, `indus.tui_bridge*` and `uvicorn*`. Change `MODE_FILES` in `log.py`.
- Without `verbose`, `cli`/`gateway`/`gui` modes attach no console handler (files only);
  `stdio` attaches a stderr handler at WARNING (DEBUG when verbose). Change `_console_level`.
- `setup` reads the logging keys from `scope.config` when the scope carries a loaded view and uses
  the defaults otherwise; it does not call the P01-10 loader itself (bootstrap can load first).
- Dir/file modes are fixed 0700/0600 here; honouring `INDUS_HOME_MODE` and the managed marker
  should go through `home.secure_dir` once P01-03 is merged (one-line swap in `_secure_mkdir`).

Compare checklist (filled after the rewrite; reopened `hermes_logging.py` piped-stdout helper and
the file spec table):
- [x] Same four files, sizes, counts and levels; mode files only in their mode and only for their
  component loggers (tests: `test_log.py::test_warning_reaches_errors_log_and_info_does_not`,
  `::test_rotation_keeps_backup_count`, `::test_gateway_file_only_in_gateway_mode_and_for_gateway_loggers`)
- [x] One queue + listener, records queued unformatted as a shallow copy; flush and bounded drain
  (`::test_drain_returns_within_timeout_when_the_writer_is_stuck`)
- [x] Records carry the resolved home and are written with that home's redaction
  (`::test_records_from_two_tasks_land_in_their_own_homes`,
  `test_log_routing.py::test_threads_with_different_scopes_write_to_their_own_profile`,
  `::test_secret_in_message_is_masked_in_the_file`)
- [x] External rotation (inode change / missing file) reopens (`::test_external_rotation_reopens_the_file`)
- [x] Deleted profile's logs dir is not recreated; its records fall back to the process home
  (`::test_deleted_or_tombstoned_routed_home_is_never_recreated`,
  `test_log_routing.py::test_profile_deleted_while_logging_falls_back_without_recreating`)
- [x] Noisy loggers at WARNING; verbose adds DEBUG on stderr (`::test_noisy_loggers_pinned_at_warning`,
  `::test_verbose_adds_debug_on_stderr`)
- [x] Gap found: a piped stdout is line-buffered by the reference — added
  `_line_buffer_piped_stdout` for non-stdio modes (`::test_piped_stdout_becomes_line_buffered`)
- [x] Gap found (by pyright): the router's `release(home)` overrode `logging.Handler.release` (the
  handler lock) — renamed `release_home`.
- Deliberate differences: the reference routes only to homes it was told about and sends any
  other home to the launch home; we route to any live home lazily (a scope is the authority).
  The reference re-checks routed homes for tombstones every 2 s; we check when a home's files are
  first opened and whenever its logs dir disappears, and rely on P07's delete calling
  `release_home` first. Session tags come from context vars, not thread-locals. `stdio` mode is
  ours (R09.14). Fewer noisy loggers pinned (the phase's four; others join with their phases).
- Not taken: Windows cross-process rotation lock and its fallback warning; group-readable files
  for managed installs (follow-up via `home.secure_dir`); component filter for `indus logs`
  (P07); redaction of detached child stdio (P05).

## Status

Done (2026-10-18). `src/indus/foundation/log.py` with unit and integration tests, written against
the phase-file names of `scope`, `errors`, `redact`, `layout`; tests run against the test-only
stand-ins in `tests/unit/foundation/_stubs_log.py` until S-core/S-env land. Pyright strict was run
clean against temporary typed stand-ins (removed). Ledger rows R03.23 (routing part), R09.14 and
ACC-I53-1 point at tests.

## Integration (lead)

- `_secure_mkdir(path, scope, *, parents)` → `home.secure_dir` with the owning scope's env
  (`INDUS_HOME_MODE`, managed installs); without `parents` a plain `mkdir` runs first so a vanished
  home is still never re-created.
- The 3 pyright errors (`Scope` vs `RedactScope`) are fixed in `scope.SecretSource`; they were
  also why `test_layers.py::test_untyped_function_fails_only_under_strict_path` failed (the copied
  tree had errors in `log.py` besides the planted probe).
- Tests use `make_scope` / `use_scope` from `foundation.scope`; `FakeConfig` (a `ConfigSource`
  for values the schema would reject) moved into `test_log.py`.
