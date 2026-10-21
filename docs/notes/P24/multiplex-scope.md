# Task: Scope binding and fail-closed secret reads (P24-02) Phase: 24 Owner: wave-37 builder M1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/authz_mixin.py` (the
scoped gate reader it imports, the per-profile adapter map, authorization home), and the scoped
reader it calls, `gateway/platforms/_shared.py` (`platform_gate_env`, `extra_or_secret`,
`profile_scoped`). Research 06 §3.26, traps R06.26, R05.14, R02.8, R02.29, R03.23, R01.16.
Closed before writing.
Evidence: none.

What it does (own words): every piece of work a multiplexed gateway does for a served profile
runs with that profile's `Scope` bound — its home, its env view, its config, its secret store and
its terminal policy travel together, so a turn, an agent eviction, the shutdown flush, a media
delivery, a deferred callback, a cron tick, a child process, a background thread and an
availability probe all read and write that profile and nothing else. The process environment
belongs to the launch profile only: a secondary's scope is built from a minimal set of OS names
(PATH, locale, terminal, user, temp dirs, XDG dirs, CA bundle paths) plus its own `.env` and config
bridges, never from the launch process's env, so a launch-profile credential or allowlist is not
even present in a secondary's view.

One scoped reader answers every credential and allowlist read: the profile's secret store first,
then its own env view; a miss returns the caller's default. For a routed (secondary) scope the env
rung reads only what that profile's `.env` and config put there (the overlay), so even a scope
built the wrong way cannot see inherited values. That is what makes R06.26 fail closed: an
allowlist set only for the launch profile is absent for a secondary, which then denies.

Probes that run before any scope exists (boot) log at debug and are remembered; the first scoped
turn of a profile re-runs them under that scope (R02.29). Inbound media a shared bot downloaded
into the launch profile's cache is moved into the routed profile's cache and the event's paths are
repointed, so the routed profile's turn reads its own files. Adapter settings of secondaries are
never written to `os.environ` (we never bridge YAML into the process env at all; the guard below
keeps it that way).

A static guard test parses every module under `src/indus/gateway/` and fails on any process-env
read (`os.environ`, `os.environb`, `os.getenv`, `os.putenv`, `from os import environ`, and
`EnvView.from_process()`), except the one launch-only default listed with its reason.

Inputs → outputs:
- `secondary_base_env(launch_env)` → dict of OS names only.
- `build_served_scope(launch, home)` → routed `Scope` (the launch scope for the launch home);
  `ProfileScopeCache(launch)` → one scope per served home, `drop(home)` on unserve.
- `ScopedReader(scope).get(name, default)`, `.allowlist(name)`, `.flag(name)`;
  `scoped_reader_for(scope)` → `Callable[[str], str | None]` for `Authorizer(env=)`.
- `ScopeBinder(scope)`: `run_turn(coro)`, `evict(fn)`, `finalise(fn)`, `deliver_media(coro)`,
  `defer(delay, fn)`, `cron_tick(fn)`, `child_env(**kw)`, `thread(fn)`, `probe(name, fn)`.
- `ProbeGate`: `run(name, check)` → result or None when unscoped; `on_scoped_turn(scope)` re-runs
  owed probes once.
- `adopt_inbound_media(event, launch_home, scope)` → event with refs inside the routed cache.
- `authz.py`: `Authorizer.for_scope(scope, registry, **kw)` reads policy through the scoped
  reader.

Files: `src/indus/gateway/multiplex/scope.py`, `tests/unit/gateway/multiplex/test_scope.py`,
`tests/unit/gateway/multiplex/test_no_env_reads.py`; shared: `src/indus/gateway/core/authz.py`.

Edge cases / failure modes:
- the secondary's `.env` missing or unreadable → empty overlay (every secret read misses → deny).
- the secret store raises → logged by name only, then the env rung; for a routed scope only its
  overlay.
- a value present in both launch env and the secondary's `.env` → the secondary's (and only
  because its own file says so).
- a probe that raises while unscoped → quiet, retried scoped; a probe re-run that raises scoped →
  logged as a warning once, not remembered.
- media refs outside the launch cache (URLs, files elsewhere) → left untouched; a name clash in
  the routed cache → a fresh name, never an overwrite; symlinks are not followed out.
- a thread or task started inside a binder keeps the profile after the binder returns (context
  copied at start).

Our design differences / decisions:
- Our `Scope` already bundles home + env + config + secrets + terminal policy; the binder just
  makes every activity kind go through `use_scope` / `spawn_with_scope` / `bind_scope` /
  `thread_with_scope` / `safety.child_env`.
- OS names kept for a secondary: a fixed list (no proxies: a proxy URL may carry credentials and
  is a profile choice — a secondary that needs one sets it in its own `.env`).
- The guard is a test, not a new tooling check, so it runs with the gateway suite; its allow-list
  is in the test with the reason (below).
- Guard allow-list (written reason, as the phase file requires):
  `src/indus/gateway/control_socket.py` — `ControlServer(env=None)` falls back to
  `EnvView.from_process()`; the control socket serves only the launch profile (supervisor
  detection reads the launch process's env) and the daemon passes `scope.env` anyway.

Tests we will write:
- a secondary with no allowlist of its own denies a sender the launch profile allows
  (Authorizer through the reader); with its own allowlist in its secret store it allows.
- the secondary scope has none of the launch env's names (credential and marker) and its child
  env lacks them; the launch child env keeps them.
- each activity kind sees the bound profile (turn, eviction writes memory into the secondary only,
  finalisation, media delivery, deferred callback, cron tick, background thread, probe).
- probes: unscoped → None + quiet, then re-run on the first scoped turn exactly once.
- inbound media moved and repointed; outside refs untouched.
- guard: the gateway tree is clean; a seeded `os.environ.get` in a temp adapter file fails it;
  `from os import getenv` and `EnvView.from_process()` are caught too.

## Decisions for the lead
- Wiring owed: the daemon builds each served profile's scope with `ProfileScopeCache` and runs
  every per-profile activity through `ScopeBinder`; `Authorizer.for_scope` per served profile;
  intake calls `adopt_inbound_media` after routing (P24-03) when the receiving bot's profile is
  not the routed one; the cron host ticks each served profile under its binder.
- Proxies are not inherited by secondaries (see above); revisit if operators want a host-wide
  proxy (then add the names to `SECONDARY_OS_NAMES`).

Compare checklist (filled after the rewrite; reopened the two files):
- [x] scoped miss returns the default, never the process env (test:
  `tests/unit/gateway/multiplex/test_scope.py::test_a_secondary_without_its_own_allowlist_denies`)
- [x] the default profile keeps env-after-secrets reads (test:
  `test_scope.py::test_the_launch_profile_reads_its_env_after_its_secrets`)
- [x] a secondary with no adapter of its own never borrows the launch profile's (P24-03 table;
  test: `tests/unit/gateway/multiplex/test_routes.py::test_a_secondary_without_a_bot_never_borrows_the_default`)
- [x] eviction writes into the owning profile only (test:
  `test_scope.py::test_eviction_writes_memory_into_the_secondary_only`)
- [x] YAML → env bridging never reaches the process (test:
  `test_scope.py::test_building_a_secondary_never_touches_the_process_env`)
- Deliberate differences: the reference keeps a process-wide secret-scope override and a
  "multiplex active" switch and falls back to the process env when unscoped; we never consult the
  process env for any profile scope (the launch scope's view is its own snapshot), and a routed
  scope reads only its own overlay. No first-writer-wins bridges exist to guard against.
- Not taken: per-adapter YAML `extra` precedence (`extra_or_secret`) stays in each adapter; the
  reader only covers env/secret names.

## Status

Done (2026-10-21). Daemon wiring listed above is owed by the lead.
