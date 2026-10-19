# Task: Daemon process: run, locks, liveness, runtime status (P15-24) Phase: 15 Owner: wave-20 builder W3

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/run_startup.py` (startup
order only: `start`, `_start_impl`, `_start_recover_previous_run`, the connect/aggregate/no-connection
steps), `gateway/status.py` (pid + start-time liveness, `terminate_pid`, the runtime lock helpers,
the runtime-status writer/reader and staleness), `gateway/lifecycle_ledger.py` (phases, unclean-death
detection). Research 06 §3.1, §3.21, §3.22; R06.16–R06.18, R03.20. The API section below was
written before the Read step (it is our own design; W1, W2 and W4 build against it in parallel).
Evidence: none.

## Daemon API (written first; W1, W2, W4 plug into this)

### Modules
| Module | Holds |
|---|---|
| `indus.state.gateway_runtime` | shared by the daemon, the CLI (`indus gateway status/stop`, W4), the cron tick guard (W2) and the updater: process identity (pid + start time + pid namespace), the per-profile file paths, the pid record, the runtime-status file model + reader + staleness, the planned-stop record, exit-code constants. No gateway import (layer rule D-16.246). |
| `indus.gateway.daemon.liveness` | the profile's single-instance lock (`InstanceLock`: an `flock` on `<home>/gateway.lock` held for the process lifetime + the pid record), takeover (`--replace`: SIGTERM, 10 s, SIGKILL). |
| `indus.gateway.daemon.status` | `StatusWriter`: the runtime-status file writer (atomic replace, 0600), state transitions, per-platform rows, the 60 s heartbeat. |
| `indus.gateway.daemon.parts` | `DaemonPart` (base class), `Stage`, `PART_FACTORIES` (the one list other slices append to). |
| `indus.gateway.daemon.process` | `GatewayDaemon` (assembly + the whole lifecycle), `DaemonOptions`, `AdapterConfig`, `run_daemon(scope, ...) -> int`. |
| `indus.gateway.daemon.command` | the click command `run` (`indus gateway run [--replace]`) that W4's `indus gateway` group registers by `"indus.gateway.daemon.command:run_command"`. |
| `indus.gateway.daemon.lifecycle` / `drain` / `watchdogs` | P15-25 (signals, drain, restart, external drain marker, loop/startup/shutdown watchdogs, restart-loop guard, systemd notify). |
| `indus.gateway.daemon.recovery` | P15-26 (boot recovery). |

### What `GatewayDaemon` builds (assembly, before anything connects)
For one profile (`scope`):
- `store` (`state.registry.acquire(scope)`), `settings = load_gateway_settings(scope)`, `section` (the
  `gateway` section).
- `registry`: `PlatformRegistry()` (built-ins), then `plugins = PluginUpkeep(scope, tools,
  surface="gateway", interactive=False)`, `await plugins.start()`, then
  `registry.load_plugin_entries(plugins.host.providers)` before the enablement pass.
- `mcp = McpUpkeep(scope, tools, aux=aux, watch=False)`; `service = SessionService(...,
  mcp=mcp, plugins=plugins, ...)` (agents run with `run_context="interactive"`, platform
  `gateway`; one tool registry shared by every agent). Tests pass `service_factory=` instead.
- `hooks = plugins.bus` (may be None), `automation = SessionAutomation(service)` (started on the loop).
- `bot_guard = BotLoopGuard(settings_reader(view.get))`, `cap = ConcurrencyCap(lambda:
  section.max_concurrent_sessions)` (re-read every call), `sent_index = SentIndex()`,
  `dead_targets = DeadTargets.for_scope(scope)`, `pairing = PairingStore(scope)`,
  `authorizer = Authorizer.from_registry(registry, env=…, extra_for=…, adapter_for=…,
  is_paired=pairing.is_approved, global_unknown_dm=lambda: section.unauthorized_dm_behavior,
  bot_guard=bot_guard, profile=…)`, `unauthorized = UnauthorizedHandler(authorizer, pairing)`.
- `senders: dict[str, OutboundSender]` — one per connected platform, built by
  `daemon.make_sender(name, adapter)` with `reply_to_mode=platform.reply_to_mode`,
  `ephemeral_ttl=display.ephemeral_system_ttl`, `sent_index`, `dead_targets`,
  `media=MediaPolicy.for_scope(scope, section)` **plus `**daemon.sender_options`** (a dict parts may
  fill in `prepare`, e.g. W1's ledger).
- `replies = PlatformReplies(daemon.senders)` (the live dict: a platform that connects later is seen).
- `pipeline = AdmissionPipeline(scope=, service=, sender=replies, store=, authorizer=, unauthorized=,
  bot_guard=, cap=, hooks=, paused=<drain/maintenance probe>, sent_index=, adapters=daemon.adapters,
  extra_for=, channel_model=, home_for=, automation=, …)`. Session keys use `key_options(section)`
  through `IntakeSettings.from_scope` (profile namespace).

### Parts: how other slices plug in
```python
from indus.gateway.daemon.parts import DaemonPart, Stage


class MyPart(DaemonPart):
    name = "my-part"
    stage = Stage.BACKGROUND  # CONTROL | RECOVERY | BACKGROUND
    required = False  # True: a start() failure fails startup (exit 75)

    async def prepare(self, daemon) -> None: ...  # assembly done, nothing connected yet
    async def start(self, daemon) -> None: ...  # at its stage (see the order below)
    async def stop(self, daemon) -> None: ...  # reverse order at shutdown; must not raise


def create(daemon) -> DaemonPart | None: ...  # None = not wanted for this profile
```
Production parts are listed once in `indus.gateway.daemon.parts.PART_FACTORIES` as
`"module:attr"` strings (each slice adds its line; the lead unions them). Tests pass
`GatewayDaemon(parts=[...])` (an explicit list replaces the factories).

### Startup order (what runs when)
1. single-instance lock + pid record (`liveness`); status `starting`; previous-death check
   (lifecycle marker left in `starting`/`running`/`draining` with a dead owner = unclean);
   restart-loop guard records this boot; startup watchdog armed (300 s).
2. assembly (above).
3. every part's `prepare(daemon)` in list order. **Seams set here:**
   - W2 approvals/clarify: `daemon.pipeline.on_request = …`, `daemon.pipeline.interceptors.append(…)`;
   - W2 intake: `daemon.pipeline.enrich = …`;
   - W1 ledger: `daemon.sender_options["ledger"] = …` (or whatever kwarg W1's sender takes);
   - anyone: `daemon.on_platform_up(cb)` / `daemon.on_platform_down(cb)` listeners.
4. `Stage.CONTROL` parts start — **W4 control socket** (`required = True`: cannot start → status
   `startup_failed`, exit 75).
5. platforms: `pipeline.begin_restore()`, `pipeline.start()`, then for each enabled platform:
   token lock (live foreign holder at startup → `fatal`), `registry.create_adapter(name,
   AdapterConfig(...))`, `connect()` with a timeout (30 s default, `gateway.connect_timeout`;
   Telegram 45 s), then `make_sender`, then `on_platform_up(name, adapter)` listeners
   (**W1 registers its P14 deliverer here**). A retryable failure leaves the platform `retrying`
   and fires `on_platform_down(name, fatal=False, reason)` (**W4's reconnect watcher queues it**);
   a non-retryable one `fatal`. Nothing connected + a fatal config problem (token collision, or no
   platform enabled and a config error) → exit 78.
6. `Stage.RECOVERY` parts start, inside the restore window — **W1 ledger sweep** — then P15-26
   recovery (unknown turns paused, restart-interrupted routes resumed once, restart notices),
   then `pipeline.finish_restore()` replays held events.
7. `Stage.BACKGROUND` parts start — **W2 cron ticker, W4 reconnect watcher, W1 channel-directory
   refresh** — then housekeeping (status heartbeat every 60 s), loop watchdog, status `running`,
   systemd `READY=1`.

### Shutdown order
`draining` → refuse new turns ("maintenance"/"restarting" notice) → chat turns interrupted after
`gateway.restart_drain_timeout` (default 0), their routes flagged restart-interrupted, users told →
`Stage.BACKGROUND` parts stop (reverse; W2's cron drain runs in its own `stop`) → adapters disconnect
(5 s each; `on_platform_down(name, fatal=False, reason="stopping")`) → `Stage.RECOVERY` parts stop →
pipeline, replies, automation, service close → `Stage.CONTROL` parts stop (the socket answers during
the drain) → token locks released → status `stopped` (+ `exit_reason`) → pid record and lock removed.

### What the daemon offers parts (attributes and methods)
- attributes: `scope`, `clock`, `options: DaemonOptions`, `settings: GatewaySettings`, `section`,
  `store`, `registry`, `service`, `plugins`, `mcp`, `hooks`, `automation`, `pipeline`, `replies`,
  `senders`, `adapters` (connected, by name), `platforms` (`PlatformRuntime` rows by name: state
  `connected/retrying/fatal/paused`, `error_code`, `error`, `retrying_since`, `needs_attention`),
  `status` (`StatusWriter`), `token_locks`, `boot` (`BootInfo`: `epoch`, `unclean_previous`,
  `restart_loop_tripped`), `lifecycle` (P15-25: `draining`, `stopping`, `stop_reason`).
- `await daemon.connect_platform(name, *, is_reconnect=True) -> PlatformRuntime` (W4 reconnect
  watcher; a token-lock conflict now is retryable, R06.16).
- `await daemon.disconnect_platform(name, *, fatal: bool, reason: str)`.
- `daemon.set_platform_state(name, state, *, error_code=None, error=None, needs_attention=None)`
  (W4: `needs_attention`, `/platform pause|resume` → `paused`); rewrites the status file.
- `daemon.on_platform_up(cb(name, adapter))`, `daemon.on_platform_down(cb(name, fatal, reason))`
  → undo callables; callbacks may be sync or async; failures are logged.
- `daemon.identity() -> dict` (W4 `identify`: pid, start_time, pidns, home, profile, supervised,
  code_version, epoch); `daemon.status_snapshot() -> dict` (W4 `status`).
- `daemon.request_stop(reason, *, exit_code=0)`, `daemon.request_restart(reason)` (graceful, exit
  75 after in-flight turns), `daemon.lifecycle.set_draining(reason)` / `clear_draining()` (W4
  `drain` / `pause-for-update`).
- `daemon.spawn(coro, name=)` — a tracked background task cancelled at shutdown.
- `daemon.recovery.crash_reply_sink = async (route, source, text) -> None` — W1 may route boot
  recovery's owed replies through the ledger (default: one labelled send).
- `daemon.make_sender(name, adapter)`, `daemon.serving_state()` (`running` / `degraded`),
  `daemon.closers` (async callables run after the service closes).

### Shared records (`indus.state.gateway_runtime`, for W2 / W4 / the updater)
- paths: `pid_path(home)` (`gateway.pid`), `lock_path(home)` (`gateway.lock`), `status_path(home)`
  (`gateway_state.json`), `planned_stop_path(home)`, `drain_marker_path(home)`
  (`gateway_drain.json`), `boots_path(home)`.
- `ProcessIdentity.current()`, `.alive()` (pid + start time + pid namespace; never bare pid),
  `pid_alive`, `process_start_time`, `pid_namespace`.
- `read_pid_record(home) -> PidRecord | None`, `live_gateway(home) -> PidRecord | None`.
- `RuntimeStatus` model, `read_status(home)`, `is_stale(status, now, after=120.0)`.
- `record_planned_stop(home, pid, reason)` (W4 `stop` writes it **before** SIGTERM),
  `read_planned_stop(home)`, `clear_planned_stop(home)`.
- `EXIT_OK = 0`, `EXIT_RESTART = 75`, `EXIT_CONFIG = 78`; `SUPERVISED_ENV =
  "INDUS_GATEWAY_SUPERVISED"`; `GATEWAY_GUARD_ENV = "_INDUS_GATEWAY"` (set in the daemon's
  process; W4's `stop`/`restart` refuse when they see it — the agent cannot stop its own gateway).

## Task

What it does (own words): `indus gateway run` keeps one gateway per profile in the foreground. It
first makes sure no other live gateway serves the profile: the pid record names a pid, its start
time and its pid namespace, and a process counts as that gateway only when all three still match
(a recycled pid or a pid from another container is not "alive"). An exclusive `flock` on
`<home>/gateway.lock`, held for the life of the process, closes the race between two starts (the
kernel drops it when the process dies, so a crash never leaves a lock that needs cleaning; the lock
file itself is never deleted, only the pid record). A second `run` while one lives exits 0 for a
person at a shell ("already running, pid N") and 75 under a supervisor
(`INDUS_GATEWAY_SUPERVISED=1`) so the unit is retried rather than parked; `--replace` sends
SIGTERM to the live one, waits up to 10 s, then SIGKILL (only while the start time still matches).

Then it assembles the daemon (see the API), starts the control parts, connects every enabled
platform with a timeout, runs recovery, starts the background parts and reports `running`.
Exit codes: 0 clean stop; 75 "restart me" (graceful restart, watchdog, another live gateway under
a supervisor, a required part that could not start, an unexpected error while starting); 78 fatal
configuration (nothing connected and every enabled platform failed with a connect-time conflict —
a token collision or a non-retryable connect error — and none is retrying; or no platform enabled
while the config has errors) — never for a transient condition. A missing library or an adapter
that cannot be built parks that platform as `fatal` but never forces 78 (a node may lack one
platform and still run cron and API work). While any platform is parked `fatal` the daemon reports
`degraded` instead of `running`.

Runtime status (`<home>/gateway_state.json`, atomic replace, 0600): `{state, updated_at, pid,
start_time, pidns, profile, code_version, epoch, exit_reason?, previous_exit?, platforms: {name:
{state, needs_attention?, error_code?, error?, retrying_since?}}}`; `state` is one of `starting`,
`running`, `draining`, `stopping`, `stopped`, `startup_failed`, `degraded`. Housekeeping rewrites it
every 60 s as a heartbeat; readers call it stale after 120 s. The same file is the lifecycle
marker: when a new process finds it in `starting`/`running`/`draining`/`stopping` with an owner that
is no longer alive, the previous process died uncleanly; that is logged, appended to
`logs/gateway_exits.log` and recorded as `previous_exit: "unclean"`.

Inputs → outputs: profile home, config (`gateway.*`, `platforms.*`), env credentials,
`--replace`, `INDUS_GATEWAY_SUPERVISED` → pid record, lock, status file, exit code; adapters
connected; parts started.

Files: `src/indus/state/gateway_runtime.py` (new, shared records), `src/indus/gateway/daemon/
{__init__,liveness,status,parts,process,command}.py`, `src/indus/gateway/__init__.py` (export),
`src/indus/gateway/token_locks.py` (start-time/pid helpers now come from `state.gateway_runtime`),
`src/indus/foundation/config/schema.py` (shared: four `gateway.*` rows) + its test,
`tests/unit/gateway/daemon/test_liveness.py` (identity, records, the lock),
`tests/integration/gateway/test_daemon_start.py`, the kits `tests/integration/gateway/daemon_kit.py`
(in-process daemons) and `tests/integration/gateway/spool.py` (child processes), the plugin fixture
`tests/fixtures/gateway/spool_plugin/` (the stand-in entry `_stubs_w3.py` was removed at integration).

Edge cases / failure modes:
- pid record names a live pid with a different start time (reuse) → not alive; same pid but the
  recorded namespace differs from ours → not provably ours: treated as alive only when the start
  time matches too (fail closed when a value cannot be read on both sides).
- the lock is held but the pid record is missing/garbled (another process is between lock and
  write) → treated as live (75/0), never stolen.
- `--replace` on a pid whose start time changed meanwhile → no signal is sent.
- a platform whose library is missing or whose factory fails → `fatal` with the reason; others go on.
- token lock held by a live foreign process at startup → that platform `fatal` (`token_conflict`);
  if nothing else connected → 78.
- connect timeout or a retryable connect error → `retrying` (W4's reconnect watcher retries).
- no platform enabled and no config problems → keep running (cron, API parts) with a warning.
- status file unwritable → logged once, never fatal.

Our design differences / decisions:
- One status file doubles as the lifecycle marker (no separate sentinel file).
- Liveness = pid + start time + pid namespace (R03.20), shared through `state.gateway_runtime`
  so the CLI and cron can read it without importing the gateway (D-16.246).
- Supervisor detection reads only `INDUS_GATEWAY_SUPERVISED` (our service units set it, W4).
- Platform adapters are built with `AdapterConfig(name, settings, scope, clock, proxy)` passed to
  the registry entry's factory (P15-33..35 implement `create_adapter(config)` to it).
- Multiplexed profiles, desktop "ensure" start, the respawn-storm guard, code-sha fleet checks
  and agent caches are not in P15.

Tests we will write:
- liveness: recycled pid, other namespace, unreadable start time, dead pid; lock exclusion between
  two processes; `--replace` refuses on a changed start time (fake process table).
- status: transitions recorded in order; stale after 120 s with a fake clock; unclean previous
  death detected; file mode 0600.
- in-process daemon over the fake platform and fake model: a DM is answered; token collision with
  nothing connected → 78; a missing library → `fatal`, others connect.
- subprocess daemon in a tmp home (spool plugin platform + HTTP fake model): DM answered; second
  `run` exits 0, and 75 when supervised; SIGTERM drains, exits 0, removes the pid record and leaves
  no stray process or file.

## Decisions for the lead

### Interface with W4 (control socket / `indus gateway`; built in parallel, reconciled by note)
- Run target: `indus.gateway.daemon.process:run_command` works (re-exported from
  `daemon/command.py`), matching W4's `RUN_TARGET`.
- Aligned: `daemon.hook_bus`, `daemon.runtime_status()`, `daemon.set_platform_status(name,
  PlatformStatus | None)`, `daemon.scope`, `daemon.pipeline`; zombies count as gone in
  `pid_alive` (as W4's `alive`); the planned-stop record has the same name
  (`gateway_planned_stop.json`).
- **Mismatch — two runtime-record modules:** mine is `indus.state.gateway_runtime`, W4's is
  `indus.service.gateway_runtime`. Keep one; I recommend `state/` because the cron tick guard
  (W2, `indus.cron`) sits *below* `service` in the layer contract and cannot import it.
- **Mismatch — status file name:** mine is `gateway_state.json` (already excluded as a runtime
  file by `indus import`, `cli/commands/import_cmd.py`); W4 uses `gateway_status.json`. Pick one;
  `gateway_state.json` needs no import-list change.
- **Mismatch — wiring shape:** W4 expects `await side_services.attach(daemon)` right after the pid
  lock, plus `services.platform_failed(...)`, `services.started(platforms)`, `services.stop()`.
  The daemon offers the same moments as a part: a `Stage.CONTROL`, `required = True` part whose
  `prepare` subscribes `daemon.on_platform_down(lambda name, fatal, why:
  services.platform_failed(name, why, retryable=not fatal, lost=...))`, whose `start` calls
  `attach(daemon)` (ControlSocketError → required-part failure → `startup_failed`, exit 75), a
  `daemon.on_platform_up` / BACKGROUND hook calling `services.started(sorted(daemon.adapters))`,
  and whose `stop` calls `services.stop()` (CONTROL parts stop last). Attaching after assembly
  (not literally right after the lock) is what gives W4 a built `pipeline`. Add its factory to
  `PART_FACTORIES`.
- **Mismatch — `connect_platform`:** returns the daemon's `PlatformRuntime` (state, error_code,
  error, retrying_since, needs_attention), not W4's `ConnectOutcome`; the bridge part maps it.
- `control_handlers()`: not provided; W4's part can build handlers from `identity()`,
  `status_snapshot()`, `request_stop`, `request_restart`, `lifecycle.set_draining/clear_draining`
  and `plugins` (reload).
- `src/indus/gateway/daemon/__init__.py` exists in both slices (docstring only): take either.

- Unclean-death marker = the status file's state (see above) instead of a second sentinel file.
- `AdapterConfig` lives in `gateway.daemon.process`; adapters (P15-33..35) take it in
  `create_adapter(config)`.
- A required CONTROL part (W4's control socket) that cannot start exits 75 (could be transient: a
  stale socket path), with `startup_failed` in the status file.
- Config rows added (shared file): `gateway.connect_timeout_seconds` 30,
  `agent.restart_drain_timeout` 0, `agent.restart_after_turn_timeout` 1800 (under `agent` to
  match W4's unit generator), `gateway.systemd_watchdog_seconds` 0. `agent.cron_drain_timeout`
  (30 s) belongs to W2's cron part (W4's unit reads it); not added here.
- The daemon's agents use a lean factory (like the TUI bridge): `with_learning` lives in
  `cli/runtime.py` and the gateway may not import the CLI; moving it to `service/` would give chat
  sessions the learning loop (follow-up).
- Streaming edits / tool progress / typing (`StreamConsumer`, `ToolProgress`, `TypingLoop`,
  `Heartbeat`) are not fed by the daemon yet: replies go out whole through `PlatformReplies`
  (follow-up for the runner wiring).
- `degraded` doubles as "serving with a parked fatal platform" (the reference does the same) as
  well as the loop-watchdog exit state; `exit_reason` tells them apart.
- `_INDUS_GATEWAY` reaches the agent's tool subprocesses through the scope env view plus
  `_INDUS_FORCE__INDUS_GATEWAY` (child_env's force entry); the process env is never written
  (`no_raw_environ`). W4's `stop`/`restart` read it from their own scope env.
- The ConcurrencyCap reads `gateway.max_concurrent_sessions` from the settings loaded at start
  (an edit applies at the next start), not on every claim.
- The process tests run `python -m indus gateway run` (integration; the `_stubs_w3` entry is gone).
- Integration (lead): `PART_FACTORIES` = control (`side_services:create`, CONTROL, required),
  delivery ledger (`delivery.parts:ledger_part`, RECOVERY), deliverers + channel directory
  (`delivery.parts:router_part`, BACKGROUND), cron / prompts / intake (`extras:create`,
  BACKGROUND). `daemon.on_ready(cb)` (new) fires once startup is complete (W4's
  `gateway:startup`). `AdapterConfig` moved to `indus.gateway.config` (re-exported here) so
  `indus send` builds adapters the same way.

Compare checklist (filled after the rewrite; reopened `gateway/run_startup.py` `_start_impl`,
`_start_aggregate_connect_results`, `_start_handle_no_connections`, `_startup_fail_fatal_config`;
`gateway/status.py` liveness, `terminate_pid`, lock and status helpers; `gateway/lifecycle_ledger.py`):
- [x] liveness is pid + start time (+ namespace), never bare pid
  (`tests/unit/gateway/daemon/test_liveness.py::test_a_recycled_pid_with_another_start_time_is_not_alive`,
  `::test_unknowns_fail_closed_and_foreign_records_are_not_probed`)
- [x] takeover: SIGTERM, wait, SIGKILL only while the start time still matches
  (`::test_replace_terminates_the_holder_and_takes_over`, `::test_replace_never_signals_a_recycled_pid`,
  `::test_kill_follows_term_only_while_the_start_time_matches`)
- [x] lock files are never unlinked (only our own pid record)
  (`::test_the_lock_excludes_another_process_and_dies_with_it`)
- [x] second start: 0 at a shell, 75 supervised
  (`tests/integration/gateway/test_daemon_start.py::test_a_second_daemon_for_the_same_profile_refuses`,
  `::test_the_daemon_process_serves_refuses_a_second_and_drains_on_sigterm`)
- [x] token collision with nothing connected → 78 (`::test_a_token_collision_with_nothing_connected_exits_78`)
- [x] gap found: a parked fatal platform must not look healthy — fixed by `serving_state()`
  (`degraded`), pinned by `::test_a_missing_library_is_fatal_for_that_platform_while_the_others_connect`
- [x] gap found: missing libraries / unbuildable adapters must not exit 78 — fixed by
  `_NOT_CONFLICTS`, pinned by `::test_nothing_installed_is_not_a_fatal_config_the_daemon_keeps_running`
- [x] mixed fatal + retrying stays up (`::test_a_connect_timeout_leaves_the_platform_retrying`)
- [x] status transitions in order, 0600, stale after 120 s
  (`::test_a_dm_is_answered_and_the_status_file_follows_the_lifecycle`,
  `tests/unit/gateway/daemon/test_liveness.py::test_status_round_trip_and_staleness`)
- [x] unclean previous death detected and logged (`::test_an_unclean_previous_death_is_detected`)
- Deliberate differences: one status file is also the lifecycle marker (the reference keeps a
  separate sentinel with memory samples and an OOM guess); no multiplexing, rendezvous records,
  respawn-storm guard, startup warm-up or code-sha fleet fields; Windows lock paths not built.
- Not taken: the stuck-loop auto-suspend across three restarts (the restart-loop guard and the
  unknown-turn pause cover the same risk in P15); plugin re-wire subscriptions.

## Status

Done (2026-10-19). Shared files touched: `src/indus/gateway/token_locks.py` (helpers moved to
`state.gateway_runtime`, re-exported), `src/indus/gateway/__init__.py` (lazy exports),
`src/indus/foundation/config/schema.py` + `tests/unit/foundation/config/test_schema.py` (four rows),
`docs/internals/traps-ledger.md` (R03.20, R03.21, R06.16–R06.20), `docs/internals/acceptance-map.md`
(ACC-K1-1, ACC-K3-1, ACC-K31-1). Handed on: W4 registers `run_command` and adds the control-socket
part; W1/W2 add their parts to `PART_FACTORIES`.
