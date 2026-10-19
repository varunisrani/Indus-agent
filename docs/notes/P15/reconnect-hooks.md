# Task: Reconnect watcher, `/platform`, gateway hooks (P15-29) Phase: 15 Owner: wave-20 builder W4

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/run_adapters.py`
(reconnect watcher, supervised respawn, needs-attention flag, backoff bump only),
`gateway/run.py` (`_reconnect_backoff`, `_reconnect_needs_attention` and the supervision
constants only), `gateway/hooks.py` (all). Research 06 §3.18, §3.23. Closed before writing.
Evidence: none.

What it does (own words):
Reconnect watcher. A platform that failed to connect (at startup or later) with a retryable error
goes into a queue. The watcher retries it with backoff 30, 60, 120, 240 s, then every 300 s,
forever, calling the daemon's connect function with `is_reconnect` semantics. A non-retryable
failure drops the platform out as `fatal`. A platform that has been failing continuously for
`agent.reconnect_attention_after` seconds (default 1,800; 0 = never) is flagged
`needs_attention` in the runtime status — a signal only, retries go on. Nothing pauses on its
own: `/platform pause <name>` stops retries of a queued platform (or drops the inbound of a
connected one, P15-13), `/platform resume` restarts them. The watcher task itself is supervised:
if it crashes it is respawned after 2, 4, 8 … 60 s; five fast crashes in a row stop the
supervision with an error log (a healthy run of 300 s resets the count).
Gateway hooks (G12). Folders `<home>/hooks/<name>/` with `HOOK.yaml` (`name`, `description`,
`events`) and `handler.py` exposing `handle(event_type, context)`, sync or async. Event names
are `<kind>:<what>`: the agent kind fires when a chat message starts a turn, after each of its tool
calls and when the reply is done; the session kind on start, end and reset; the command kind per
command name (wildcard `command:*` for all); the gateway kind once at startup. Context: platform, user_id,
chat_id, thread_id ("" outside threads), chat_type, session_id, message (first 500 chars); agent
end adds response, model, provider. The hooks folder is found from the profile in scope when the
event fires (never at import). A sync handler runs on the `hooks` blocking pool with a 10 s
deadline, an async one is awaited for 10 s; a handler that raises or runs late is logged and
skipped; the message is still answered.

Inputs → outputs:
- `ReconnectWatcher(connect, *, clock, attention_after, on_change)`: `enqueue(name, error,
  retryable)`, `pause(name)`, `resume(name)`, `forget(name)`, `snapshot() -> {name:
  PlatformHealth}`, `due(now)`, `tick()` (one pass), `run()` (the loop), `start()` / `stop()`
  (supervised task). `connect(name) -> ConnectOutcome{state: connected|retryable|fatal, error}`.
- `GatewayHooks(home_of)`: `load(home)`, `emit(event, context) -> list[results]`,
  `command_hooks` (the P15-13 `CommandHooks` seam), `loaded(home)`.
- `attach_bus(bus, hooks, pipeline, service)` bridges the P13 bus: `post_gateway_admission` →
  `agent:start`, `post_llm_call` → `agent:step`, `on_session_start/end/reset` → `session:*`, and
  the service's turn-end listener → `agent:end`.
- one wiring function for the daemon: `indus.gateway.daemon.side_services.attach(daemon)`.

Files: `src/indus/gateway/daemon/reconnect.py`, `src/indus/gateway/hooks.py`,
`src/indus/gateway/daemon/side_services.py`, `src/indus/gateway/core/commands.py` (shared:
`/platform` shows and pauses queued platforms), `src/indus/gateway/core/admission.py` (shared:
one attribute), `src/indus/foundation/config/schema.py` (shared: `agent.reconnect_attention_after`),
`tests/unit/gateway/daemon/test_reconnect.py`, `tests/unit/gateway/test_hooks.py`.

Edge cases / failure modes:
- connect raising → counted as a retryable failure (transient), next backoff applied.
- platform enqueued twice → one entry, `queued_at` kept (continuous failure).
- a platform that reconnects and fails again later starts a fresh `queued_at`.
- paused entries keep their attempt count; resume makes the entry due at once.
- threshold read per call (a config edit applies without a restart); non-positive disables.
- hooks: missing `handler.py`, no events, YAML not a mapping, no `handle` → skipped with one log
  line; a handler module that fails to import is skipped; two hook folders may share a name
  (module names are made unique per path).
- `command:<name>` results flow to P15-13's decision handling (deny / handled / rewrite).

Our design differences / decisions:
- First retry 30 s after the failure (the reference waits for its 10 s watcher poll as well);
  the watcher sleeps until the next due entry (bounded by a 30 s poll) instead of 1 s steps.
- Hooks: context is a plain dict copy per handler (a handler cannot change what the next one
  sees); async handlers get the same 10 s budget as sync ones.
- Hooks run in the gateway only (the CLI has the P13 shell hooks).
- `/platform list` also shows queued platforms with their state and next retry.

Tests we will write:
- fake clock backoff sequence 30/60/120/240/300/300; fatal drops out; needs_attention after the
  threshold while still retrying; 0 disables; pause stops retries, resume makes it due.
- supervised respawn after a crash; gives up after five fast crashes.
- hooks: discovery and skips; exact + wildcard dispatch; sync and async handlers; crashing hook
  logged and the pipeline continues; slow sync hook abandoned after 10 s (fake deadline);
  home resolved per call (two scopes, two folders); bus bridge fires `agent:start` with the
  context fields and `agent:end` with response/model/provider.

## Decisions for the lead
- `agent.reconnect_attention_after` added to `AgentSection` (float, default 1800, ≥ 0).
- The daemon (W3) exposes what `side_services.attach` needs through the `DaemonPorts` protocol
  defined there (`scope`, `pipeline`, `hook_bus`, `runtime_status()`, `control_handlers()`,
  `connect_platform(name)`, `set_platform_status(name, status | None)`); the test stand-in
  implements it. The daemon calls `services.platform_failed(name, error, retryable=, lost=)` for
  a failed connect (`lost=True` when a running platform dropped: retried at once),
  `services.started(platforms)` after connecting, `services.stop()` on shutdown.
- `post_gateway_admission` now fires after the session is found or created and carries
  `session_id` (shared change in `gateway/core/admission.py`): the hook folders' `agent:start`
  needs the session id, which a first message only has once its session exists. Still once per
  admitted message, still before enrichment and the turn.
- `AdmissionPipeline.reconnect` (shared, one attribute) lets `/platform` list, pause and resume
  queued platforms; `/platform pause` on a queued platform holds its retries.

Compare checklist (filled after the rewrite; reopened the files above):
- [x] backoff 30·2^(n−1) capped at 300, forever for retryable (test: `test_reconnect.py::test_backoff_sequence`)
- [x] non-retryable drops out as fatal (tests: `::test_fatal_failure_drops_out`, `::test_non_retryable_at_enqueue_is_never_tried`)
- [x] needs_attention once past the threshold, read per pass, ≤0 disables, retries go on (tests: `::test_needs_attention_after_threshold`, `::test_attention_after_reads_the_config_each_time`)
- [x] continuous-failure clock kept on re-queue (test: `::test_requeue_keeps_the_first_failure_time`)
- [x] exceptions from connect count as transient (test: `::test_exceptions_count_as_retryable`)
- [x] supervised respawn 2,4,…,60 s, gives up after 5 fast crashes, healthy 300 s resets (tests: `::test_watcher_is_respawned_after_a_crash`, `::test_supervision_gives_up_after_five_fast_crashes`)
- [x] gap found: the reference retries a platform lost at runtime at once (only startup failures
  wait 30 s) — `enqueue(immediate=True)` / `platform_failed(lost=True)` (test: `::test_a_platform_lost_at_runtime_is_tried_at_once`)
- [x] gap found: a queued platform must revive a watcher whose supervision gave up — `enqueue`
  restarts it (test: `::test_enqueue_revives_a_watcher_that_gave_up`)
- [x] pause manual only (test: `::test_pause_holds_and_resume_retries_now`, `::test_platform_command_shows_and_holds_queued_platforms`)
- [x] hooks: discovery skips, exact + `<kind>:*` wildcard, bare kind never matches, sync/async, errors isolated, answers collected, per-home loading at call time (tests in `test_hooks.py`)
- [x] agent:start / agent:end context through the real pipeline and bus; command hook decisions (test: `test_hooks.py::test_agent_start_end_and_command_hooks_through_the_daemon`)
- Deliberate differences: handlers get a 10 s budget (the reference has none); each handler gets
  its own copy of the context; `agent:step` fires per tool call (from `post_tool_call`); hook
  failures are logged, not printed; first startup retry after 30 s with attempts counted from 0.
- Not taken: per-served-profile registries (multiplexing, P24), plugin load re-arming and
  credential claims inside the watcher (the daemon's `connect_platform` owns adapter building).

## Status

Done (2026-10-19). Shared: `gateway/core/commands.py` (`/platform` over the watcher),
`gateway/core/admission.py` (`reconnect` attribute; `post_gateway_admission` after the session
lookup, with `session_id`), `foundation/config/schema.py` + its test (one row).
