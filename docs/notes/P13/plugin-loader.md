# Task: Plugin loader (P13-15) Phase: 13 Owner: wave-10 builder M2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/plugins_loader.py`
(load deadline, abandoned loaders, module import, failure rollback, activation summary hook),
`hermes_cli/plugins_activation.py` (summary shape, names only), research §3.13.1. Closed before
writing.
Evidence: none.

What it does (own words): turns the plugins discovery allowed into live registrations. For each
one, in dependency order, it imports the module and calls its `register(ctx)` on a worker thread
with a deadline, so a plugin that hangs or crashes is skipped with a reason and every other plugin
still loads. Whatever a failed plugin registered before failing is taken back out. Afterwards each
loaded plugin gets a short summary of what works right away and what waits for the next session.

Inputs → outputs: `PluginLoader(host, *, clock=None, timeout_s=None, running_version=None,
pool=None)`; `await loader.load(discovery_or_records) -> LoadReport` with one `LoadOutcome` per
plugin (`key`, `loaded`, `reason`, `summary`). `load_timeout(scope) -> float | None`.

Files: `src/indus/plugins/loader.py`, `tests/unit/plugins/test_loader.py`,
`tests/fixtures/plugins/` (good, hanging, raising, shadowing, capabilities).

Edge cases / failure modes:
- `plugins.load_timeout_seconds` default 10; 0 = no deadline; above 600 clamped with a warning;
  negative or not a number → default with a warning.
- deadline passed → "load timed out after N s", the worker is abandoned (it keeps its thread
  until it returns), the context is closed so late registrations are refused, earlier ones undone.
- `register` raises (any `Exception` or `SystemExit`) → skipped, traceback logged, undone.
- no `register` callable → skipped with a reason.
- `requires_indus` newer than the running version → skipped before import.
- `requires_plugins`: dependencies load first (topological, ties by key); a missing requirement,
  a version mismatch, a failed requirement or a cycle skips the dependents with a reason.
- the load pool is full of stuck loaders → refused with a reason, never run inline.
- a directory plugin is imported as `indus_plugins.<slug>` from its `__init__.py`; a failed import
  leaves no half module in `sys.modules`.
- tools go into the profile's overlay of the registry; shadowing a built-in needs the plugin to
  pass `override=True` **and** the operator's `plugins.entries.<id>.allow_tool_override: true`
  (bundled plugins are trusted).

Our design differences / decisions:
- Loading is async (`await loader.load`) and uses `foundation.blocking` with its own pool
  `plugin-load` (8 workers, no queue): the cap on stuck loaders is the pool size.
- A missing or failing requirement **skips** the dependent (row); the reference loads it anyway.
- Activation summary keys: `live_now` (commands, hooks, providers, skills — skills go live through
  the P09 delta note) and `next_session` (tools, prompt sections).
- Not taken: plugin host process (D-16.46), deferred platform loaders (P15), portable MCP servers.

Tests we will write:
- hanging `register` skipped at 10 s on the fake clock while the next plugin loads; raising
  plugin skipped with its registrations undone; dependency order; failing and missing
  dependencies skip dependents; cycle; version gate before import; tool shadowing refused
  without opt-in and allowed with it; summary lists live vs next-session items; timeout config
  clamp; late registration after a timeout refused.

## Decisions for the lead
- Own pool `plugin-load` (8 workers, refuses when full) rather than sharing `hooks`/`io`: a stuck
  loader must not eat hook or file-read workers.

Compare checklist (filled after the rewrite; reopened `hermes_cli/plugins_loader.py`):
- [x] deadline 10 s / 0 off / 600 max (test: `test_loader.py::test_load_timeout_setting`)
- [x] overrun skipped, abandoned, others continue (test: `::test_hanging_register_is_skipped_and_the_next_plugin_loads`)
- [x] late registrations from an abandoned load refused (test: `::test_late_registration_after_timeout_is_refused`)
- [x] exception → skipped, registrations rolled back (test: `::test_raising_register_is_skipped_and_undone`)
- [x] SystemExit in a plugin does not end the process (test: `::test_system_exit_in_register_is_contained`)
- [x] stuck-loader cap refuses instead of loading inline (test: `::test_full_load_pool_refuses`)
- [x] dependency order (test: `::test_dependency_order`, `::test_missing_or_failing_requirement_skips_dependents`)
- [x] override needs opt-in (test: `::test_tool_shadowing_needs_operator_opt_in`)
- [x] activation summary (test: `::test_activation_summary_live_vs_next_session`)
- Deliberate differences: dependents of a missing/failed requirement are skipped (row) where the
  reference loads them with a warning; async loader on the shared blocking helper.
- Not taken: plugin host isolation, deferred platforms, language packs, portable plugins.

## Status

Done (2026-10-19). Shared: `src/indus/foundation/errors.py` (`plugins.*` rows).
