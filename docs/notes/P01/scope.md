# Task: Scope (P01-06) Phase: 01 Owner: P01 S-core builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_constants.py` (lines 210–270:
process home vs routed home, pinning). Closed before writing.
Evidence: R03.23, R01.16.

What it does (own words): `Scope` is the one value that says which profile a piece of work belongs
to: profile id, home, root, an immutable env view, the config view and secret store once loaded, the
clock and (later) terminal policy. It lives in a context variable. `current_scope()` raises
`ScopeUnresolved` when nothing set it, so code can never silently fall back to the default profile.
Helpers copy the context into asyncio tasks, threads and callables so background work keeps its
profile, and `child_env` pins `INDUS_HOME` for subprocesses.

Inputs → outputs: `make_scope(home, env=…, clock=…)` → `Scope`; `use_scope(scope)` context manager;
`spawn_with_scope(coro)` → `Task`; `thread_with_scope(fn, *args)` → unstarted `Thread`;
`bind_scope(fn)` → callable; `child_env(scope)` → `{"INDUS_HOME": …}`.

Files: `src/indus/foundation/scope.py`, `tests/unit/foundation/test_scope.py`, `tests/conftest.py`
(`scope` fixture).

Edge cases / failure modes:
- nothing set → `ScopeUnresolved` (code `scope.unresolved`).
- two concurrent tasks with different scopes see their own.
- bare `threading.Thread` does not inherit context → sees no scope.
- one captured context reused by several threads at once: each call runs in a fresh copy.

Our design differences / decisions:
- `EnvView` (immutable mapping: base snapshot + overlay where `None` deletes) is defined here; P01-15
  builds it.
- `config` and `secrets` are typed by small protocols (`ConfigSource.get(path)`,
  `SecretSource.get(name)`) so this module does not import P01-10/P01-16; the real classes satisfy
  them structurally.
- Extra field `routed: bool` (default False): True when the scope serves a profile other than the
  process's own (P01-19 needs it to ignore process-level env such as `INDUS_TIMEZONE`).

Tests we will write: unresolved raises; concurrent tasks; `thread_with_scope` vs bare thread;
`bind_scope` in an executor; `child_env` pins home; `EnvView` overlay/delete semantics; conftest
fixture shape.

## Decisions for the lead
- `routed` field added to `Scope` (not in the row) for the clock's env rule.
- `conftest.scope` fixture leaves `secrets=None`; S-env adds the file secret store after merge.

Compare checklist (filled after the rewrite; reopened `hermes_constants.py` 210–270):
- [x] routed vs process home distinction kept as `Scope.routed` (test:
  `tests/unit/foundation/test_clock.py::test_routed_scope_ignores_env`)
- [x] no fallback to the default profile (test: `tests/unit/foundation/test_scope.py::test_unresolved_raises`)
- [x] child processes get the home explicitly (test: `test_child_env_pins_home`)
- Deliberate differences: theirs keeps a process-global pin plus env mirroring; ours carries the
  whole answer in one context-local value and never mirrors into `os.environ`.

## Status

Done (2026-10-18).

## Integration (lead)

- `SecretSource` gained `known_values()` and `generation` (exact-value redaction reads them);
  `ConfigSource.get` and `SecretSource.get` take their argument positionally (`ConfigView.get(key,
  default)` did not match the parameter name).
- The conftest `scope` fixture now carries a file-backed `SecretStore`
  (`open_secret_store(home, "work", mode="file")`); an autouse `no_os_keychain` fixture keeps every
  test away from the real keychain. Config still starts unset.
- The log, layout, routing and config tests use `make_scope` / the shared fixture instead of the
  slice stand-ins.
