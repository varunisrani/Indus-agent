# Task: Platform registry, scoped token locks, proxy (P15-06) Phase: 15 Owner: wave-18 builder G2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/platform_registry.py`
(entry shape, register / lookup / create path), `gateway/status_scoped_locks.py` (release),
`gateway/status.py` (lock dir, scope hash, start-time reading, scoped acquire and the stale test
only). Research 06 §3.2, traps R06.16, R04.26. Closed before writing.
Evidence: none.

What it does (own words): the gateway finds every platform it can run in one table instead of an
if-chain. Each entry says how to build the adapter, how to tell (without importing anything heavy)
whether its library is installed, which env names hold its credential and its access lists, its
message cap, whether its "DM" is really private, and an optional parser for cron target strings.
The three chat platforms plus the API server and the webhook receiver are built in; plugins add
more through the P13 provider registry. A platform's SDK is imported only when the gateway builds
that platform's adapter, never at import or for a disabled platform.
Two gateways must never poll one bot token (two profiles, or two installs on one machine): on
connect each adapter that owns a unique credential takes a machine-local lock file named from the
platform and a hash of the credential, holding pid + process start time + profile. A live foreign
holder at startup is a fatal config conflict for that platform; the same conflict while running is
retryable (the other holder may be on its way out). A holder whose pid is gone, or whose pid now
belongs to a different process (start time differs), is stale and is reclaimed.
Proxy: adapters use the standard proxy env vars unless `gateway.trust_env: false`; a per-platform
proxy setting always wins.

Inputs → outputs:
- `PlatformEntry{name, label, factory, check_fn, required_env, install_hint, allowlist_env,
  allow_all_env, allow_bots_env, home_env, max_len, dm_is_private, platform_hint,
  cron_target_parser, group_users_env, group_chats_env, trusted_inbound, source, plugin_name}`.
- `PlatformRegistry(builtins)`: `register(entry, replace=False) -> bool`, `unregister`, `get`,
  `names`, `entries`, `load_plugin_entries(providers)`, `create_adapter(name, config)` (the
  only place a factory runs), `missing_deps(name)`.
- `builtin_entries()`; `policy_env_name(platform, suffix)` → `INDUS_<PLATFORM>_<SUFFIX>`.
- `proxy_for(platform_proxy, env, trust_env, target_host) -> str | None`.
- `TokenLocks(directory, profile, home)`: `acquire(platform, credential, startup) ->
  LockOutcome{acquired, fatal, retryable, holder, message}`, `release`, `release_all`, `held`.
  `default_lock_dir(env, family, user_home)`; `lock_file_name(platform, credential)`.

Files: `src/indus/gateway/registry.py`, `src/indus/gateway/token_locks.py`,
`tests/unit/gateway/test_registry.py`, `tests/unit/gateway/test_token_locks.py`.

Edge cases / failure modes:
- relative `$XDG_STATE_HOME` is invalid per the XDG spec and ignored (else the lock dir follows
  the cwd and two starts share nothing).
- a lock file that is empty or not JSON (a writer died mid-write): treated as stale.
- our own pid in the record: re-acquire (reconnect) and refresh the record.
- pid alive but start time unreadable on either side: treated as live (fail closed; conflict).
- races: every read-decide-write runs under one cross-process `FileLock` on `<dir>/.guard`, so two
  racers can never both reclaim a stale record; the record lands by atomic replace (0600).
- the credential itself is never written anywhere: only `sha256(credential)[:16]` in the name.
- plugin entry that tries to mark a built-in platform name `trusted_inbound`: the flag is dropped
  with a warning (people send on that platform; trusting it would skip allowlists).
- `check_fn` raising counts as "not installed"; factory raising is logged and returns None.

Our design differences / decisions:
- env names: vendor credential names (`TELEGRAM_BOT_TOKEN`, …) kept; policy names prefixed
  `INDUS_<PLATFORM>_*` (P15-02).
- `group_users_env`, `group_chats_env` and `trusted_inbound` added to the entry (the authz chain
  P15-08 needs them; the phase row's list did not name them). Built-ins leave the group env names
  empty: group allowlists come from config (`group_allow_from`, `group_allowed_chats`).
- API server and webhook are `trusted_inbound`: their own bearer-key / signature check is the
  authorization (D-16.26); nothing reaches the chat chain unauthenticated from them.
- lock dir override `INDUS_GATEWAY_LOCK_DIR` (absolute only) for tests and containers.
- macOS system proxy lookup is left out (env and per-platform settings only); add if asked.
- start time: `/proc/<pid>/stat` field 22 on Linux, `ps -o lstart=` elsewhere; P15-24's liveness
  module should replace the private helper here.

Tests we will write:
- registry: built-ins present with their env names; plugin entry added, replaced only with
  `replace=True`; plugin cannot trust a built-in name; importing the registry module imports no
  SDK (subprocess `sys.modules` check); `create_adapter` imports only the enabled platform's
  module (import spy) and never runs for a missing dep; check_fn/factory errors contained.
- proxy table (trust_env on/off, per-platform wins, NO_PROXY match).
- locks: acquire/release round trip, file mode 0600, credential not in file or name; startup
  conflict fatal, mid-run retryable; same pid re-acquire; dead pid reclaimed; recycled pid with a
  different start time reclaimed; garbage record reclaimed; two processes (multiprocessing) racing
  one token → exactly one wins; default dir per OS family and the relative XDG rule.

## Decisions for the lead
- Entry fields added beyond the row: `group_users_env`, `group_chats_env`, `trusted_inbound`,
  `source`, `plugin_name` (smallest set the authz chain needs).
- Plugin platforms: P13's `PROVIDER_KINDS` has no `platform` kind yet (plugins/ is off-limits in
  this wave). `PlatformRegistry.load_plugin_entries(host.providers)` reads a `platform` slot when it
  exists; wiring = add `"platform"` to `PROVIDER_KINDS` and a `register_platform` alias.
- `INDUS_GATEWAY_LOCK_DIR` override (absolute path only).
- Config row needed (schema owned by P15-02): `gateway.trust_env: bool = True`.

Compare checklist (filled after the rewrite; reopened `gateway/platform_registry.py`,
`gateway/status.py` scoped-lock part, `gateway/status_scoped_locks.py`):
- [x] lock key = platform + 16 hex of sha256(identity) (test: `test_token_locks.py::test_lock_name_hides_credential`)
- [x] startup conflict fatal, mid-run retryable (test: `test_startup_conflict_is_fatal_and_midrun_retryable`)
- [x] stale when the pid is gone or its start time changed (tests: `test_dead_holder_reclaimed`, `test_recycled_pid_reclaimed`)
- [x] our own pid re-acquires and refreshes the record (test: `test_same_process_reacquires`)
- [x] empty / invalid record treated as stale (test: `test_garbage_record_reclaimed`)
- [x] relative `XDG_STATE_HOME` ignored (test: `test_default_dir_ignores_relative_xdg`)
- [x] release only removes a record we own (test: `test_dead_holder_reclaimed`)
- [x] passive dependency probe separate from the factory; factory failure contained (tests: `test_registry.py::test_create_adapter_contains_failures`, `test_importing_the_registry_imports_no_sdk`)
- [x] a plugin cannot mark a shipped platform trusted (test: `test_plugin_cannot_trust_builtin`)
- Deliberate differences: the reference creates the record with an exclusive create and reclaims a
  stale one through a rename to a tombstone; we serialise read-decide-write under one
  cross-process guard lock and write by atomic replace (also closes "two racers judge the same
  record stale"). The reference also calls a live process stale when its command line is not a
  gateway, and a stopped (SIGTSTP) process stale; we rely on pid + start time only and fail closed
  when the start time is unknown.
- Not taken: deferred per-profile loader scopes, dependency auto-install (`ensure_deps_fn`),
  dashboard card fields, env-override steps, standalone senders, target-ref syntax hooks (P16-P18,
  P24 bring what they need); macOS system proxy lookup.

## Status

Done (2026-10-19). Shared: none for this task (the registry is new). Hand-on: P15-24 should replace
the private start-time helper with its liveness module; P15-33..35 provide
`indus.gateway.adapters.<name>:create_adapter`, P15-30/32 `indus.gateway.http.*:create_adapter`.
