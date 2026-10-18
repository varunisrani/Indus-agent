# Task: Tool registry (P04-08) Phase: 04 Owner: wave-5 builder D

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/registry.py` (whole file:
entry record, register/deregister, overlays, probe TTL cache and flake grace, core-drop warning,
definitions, dispatch, error bounding, kwargs narrowing, AST discovery), `tools/AGENTS.md`,
`website/docs/developer-guide/adding-tools.md` (first 120 lines). Test names of
`tests/tools/test_registry.py` (names only, for edge cases). Research `02-tools.md` §1, §3.1.
Closed before writing.
Evidence: none.

What it does (own words): one table of every model-callable tool in the process. A tool is a
name, the toolset it belongs to, a JSON schema, a handler, and optional extras (availability
probe, secret names it needs, async flag, emoji, result cap, a per-call schema patch, a timeout).
Plugins register into a per-profile overlay so one profile's plugin tool never shows up for
another. Every change bumps a generation counter that the toolset resolver and the definitions
memo key on. Probes say whether a tool is usable right now for this profile; their answers are
cached for a short while, a probe that flips to "no" right after a "yes" is treated as a flake for
a minute, and a probe that cannot see a profile scope fails closed.

Inputs → outputs:
- `ToolRegistry.register(name, toolset, schema, handler, *, probe=None, requires_secrets=(),
  is_async=None (auto-detected), emoji="", max_result_chars=None, schema_overrides=None,
  timeout_s=None, cross_refs=(), rewrite=None, override=False, profile=None)` → `ToolEntry`, or
  raises `ToolSchemaInvalid` / `ToolNameTaken`.
- `deregister(name, profile=None)`, `get(name, scope=None)`, `entries(scope=None)`,
  `names_in_toolset(toolset, scope=None)`, `generation`.
- `available(entry, scope=None, memo=None)` → bool (probe cache aware).
- `tools/builtin/__init__.py`: `BUILTIN_MODULES` (explicit list) and `load_builtins(registry)`.

Files: `src/indus/tools/registry.py`, `src/indus/tools/builtin/__init__.py`,
`src/indus/tools/__init__.py` (docstring + exports), `tests/unit/tools/test_registry.py`,
`src/indus/foundation/errors.py` (shared: ERROR_CODES rows only).

Edge cases / failure modes:
- schema not a dict, or `parameters` present but not a dict → named error at registration.
- same name + same toolset → silent replace (an MCP reconnect re-registers).
- same name, other toolset → rejected unless `override=True`; a plugin overlay entry that would
  shadow a global tool also needs `override=True`.
- probe returns false within 60 s of its last true → served as true, not cached; after 60 s the
  false is honoured and cached for 30 s.
- probe raises → unavailable, one warning per probe per process (later raises log at debug).
- no scope (boot-time probe, or a thread that lost its context) → unavailable, nothing cached,
  debug log only (R02.29).
- cache holds at most 512 entries; expired entries are pruned first, then the oldest.
- optional built-in module fails to import → warning, skipped; a required one raises.

Our design differences / decisions:
- Probes take the `Scope` as their one argument and read secrets via `scope.secrets` / env view
  (R02.8); there is no ambient secret reader.
- Registration errors raise (`ToolNameTaken`, `ToolSchemaInvalid`, codes `tools.name_taken`,
  `tools.bad_schema`); the reference logs and silently drops a cross-toolset collision. A raise
  names the culprit at the call site; discovery callers catch it.
- Built-in modules expose `register_tools(registry)` and are listed explicitly (D-16.73); no AST
  scan, no on-disk discovery cache, no import-time side effects, so several registries can coexist
  (tests, profiles).
- Profile key = the profile home path text (`scope_key(scope)`), used for the probe cache and the
  plugin overlay.
- Plugin override policy (operator opt-in per plugin, ownership by module namespace) is left to
  P13; here `override=True` is the only gate.
- "core tool dropped" warning: we warn once per probe per process for a raising probe; a probe
  that turns false is info-level. No separate core-list lookup (avoids a registry → toolsets cycle).
- The registry holds its own clock (`clock=` with `monotonic()`); tests inject `FakeClock`.

Cross-slice names needed: none.

Tests we will write:
- bad schema / bad parameters rejected with the named error.
- same-toolset overwrite; cross-toolset rejected; override accepted; overlay shadow rules.
- generation bumps on register, overwrite and deregister.
- probe cache: hit within 30 s, re-run after; flake inside 60 s → True uncached; after 60 s →
  False; raising probe → False with one warning; two profiles cached separately; unscoped →
  False, not cached, probe never called; cache bound at 512.
- builtin loader: optional import error skipped and logged, required raises.

## Decisions for the lead
- Registration conflicts raise instead of log-and-return (easy to flip in `register`).
- `is_async` defaults to auto-detection (`inspect.iscoroutinefunction`) when not given.

Compare checklist (filled after the rewrite; reopened `tools/registry.py`, `tools/AGENTS.md`):
- [x] schema / parameters rejection with a named error (test: `test_registry.py::test_rejects_schema_that_is_not_an_object`, `::test_rejects_parameters_that_are_not_an_object`)
- [x] same-toolset overwrite, cross-toolset reject, override accepted (test: `::test_same_name_same_toolset_overwrites`, `::test_collision_across_toolsets_rejected`, `::test_collision_across_toolsets_with_override_accepted`)
- [x] per-profile overlay; overlay may not shadow a global tool without override (test: `::test_overlay_is_seen_only_by_its_profile`, `::test_overlay_cannot_shadow_global_without_override`)
- [x] generation bumps on register / overwrite / deregister, not on rejected or missing (test: `::test_generation_bumps_on_every_change`, `::test_generation_unchanged_by_rejected_or_missing`)
- [x] probe TTL 30 s, flake grace 60 s served uncached, failure honoured after grace (test: `::test_probe_cached_for_thirty_seconds`, `::test_probe_flake_inside_grace_returns_true_uncached`, `::test_probe_failure_after_grace_is_honoured`)
- [x] raising probe → unavailable, one warning per probe (test: `::test_raising_probe_unavailable_and_warns_once`)
- [x] cache per (probe, profile), bounded at 512 (test: `::test_two_profiles_cache_separately`, `::test_probe_cache_is_bounded`)
- [x] unscoped probe fails closed, uncached, quiet (R02.29) (test: `::test_unscoped_probe_unavailable_and_not_cached`, `::test_probe_that_loses_its_scope_is_not_cached`)
- [x] one probe run per pass (test: `::test_one_probe_run_per_pass_with_memo`)
- [x] optional built-in import error logged and skipped (test: `::test_builtins_load_and_optional_failure_skipped`, `::test_required_builtin_failure_raises`)
- Deliberate differences: conflicts raise (`ToolNameTaken`) instead of log-and-drop; probes get the
  `Scope` argument instead of reading an ambient secret scope; a probe turning false logs at info
  and the reference's "previously available core tool dropped" warning is not kept (it needs a
  registry → toolsets import); built-ins register through `register_tools(registry)` from an
  explicit list (D-16.73).
- Not taken: AST discovery and its on-disk cache, safe-worker module list, browser-identity cache
  bypass, uncached-probe marking, plugin override policy and ownership checks (P13), toolset alias
  registry for MCP servers named like built-in toolsets (P13), backward-compat query helpers.

## Status

Done (2026-10-18). Files: `src/indus/tools/registry.py`, `src/indus/tools/builtin/__init__.py`
(empty list until P05), `src/indus/tools/__init__.py` (kept a one-line docstring: the P00 layer
test pins package `__init__` files to one line, so no re-exports), `tests/unit/tools/test_registry.py`.
Shared: three `ERROR_CODES` rows in `src/indus/foundation/errors.py`; "Decided in" for D-16.73 in
`docs/plan/16-what-we-change.md`.
