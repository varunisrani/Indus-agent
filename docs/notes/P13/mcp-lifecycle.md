# Task: Reconnect, cooldown and recycling (P13-04) Phase: 13 Owner: wave-10 builder M1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/mcp_tool_lifecycle.py` (orphan
sweep, shutdown), `tools/mcp_tool_server_run.py` (run loop: initial ladder, reconnect budget,
parking with timed self-probe, recycle wait, permanent errors), `tools/mcp_tool_handlers.py`
(respawn wait on a dead stdio child), `tools/mcp_tool.py` (retry constants, cooldown 30 → 600),
`tools/mcp_tool_discovery.py` (cooldown ledger), `tools/mcp_tool_common.py` (±20 % jitter),
`tools/mcp_tool_health.py` (recycle deadlines; names only). Research `07` §3.16.4. Evidence: none.

What it does (own words): `ManagedServer` keeps one server usable for the life of the process. One
runner task per server owns every transport it opens (P13-03 rule). States, exposed to status:
`connecting` (first connects) → `ready` → `reconnecting` (after a drop) → `cooling_down` (first
connects keep failing) → `parked` (reconnects exhausted or a permanent error) → `closed`, plus
`recycled` (a stdio server stopped for idleness or age; it restarts on the next call).

Timings (all on the injected clock):
- first connect: 1 attempt + up to 3 retries, backoff 1 s doubling, capped at 60 s, ±20 % jitter.
- reconnect after a drop: up to 5 retries with the same backoff; a session that served a call or
  lived through a keepalive interval resets the budget, a session that drops right after the
  handshake does not (no hot respawn loop).
- first-connect ladder exhausted → `cooling_down`: wait 30 s, then 60, 120, … capped at 600 s,
  one attempt per wait; a success clears the ledger. The ledger is also readable by discovery
  (P13-05) to skip servers still cooling down.
- reconnects exhausted → `parked`; a parked server probes itself every 300 s (one attempt), or at
  once on `request_reconnect()`.
- permanent errors (spawn failure, missing cert, unresolved reference, refused URL, HTTP 401/403)
  park at once without retries.
- keepalive: remote servers are pinged every 180 s, each ping bounded by 30 s; a failed ping
  reconnects. Stdio servers are not pinged; their death shows as the transport ending.
- a call that finds the server reconnecting or a stdio child dead waits up to 15 s for a fresh
  session (respawn wait), else gets `McpServerUnavailable`.
- a recycled server restarts transparently on the next call, bounded by 15 s.
- stdio recycling: after `idle_timeout_seconds` without a call or after `max_lifetime_seconds`
  since the spawn (0 = never); never while a call is in flight.

Inputs → outputs: `McpServer` + scope + clock → `ManagedServer` with `start()`, `session()`
(ensure ready), `call(...)` helpers, `status()`, `request_reconnect()`, `close()`.

Files: `src/indus/mcp/lifecycle.py`, `tests/unit/mcp/test_lifecycle.py`.

Edge cases / failure modes:
- `close()` during a backoff or a park wait ends at once; during a call, the call fails with
  "closed".
- a reconnect request while ready rebuilds the transport without charging the budget.
- concurrent callers waiting on a respawn all get the same new session.
- recycle deadline reached while a call runs → recycles right after the call.

Our design differences / decisions:
- `recycled` is a seventh state (the row's six do not describe a deliberately stopped stdio
  server waiting for its next call).
- Cooldown and parking are separate on purpose: cooling down = never connected; parked = was
  connected (or a permanent error).
- Left out: the one-time grace for an auth error on a proven session, OAuth background parking
  (P13-07/09), the suspect/verify-before-reuse latch, cross-profile connection adoption.

Tests we will write:
- retry delays within ±20 % and capped at 60 s (fake clock, seeded random).
- cooldown 30 → 60 → … → 600 → 600; ledger cleared on success.
- parked probe at 300 s; `request_reconnect` wakes a parked server.
- idle recycle then restart on the next call; lifetime recycle.
- respawn waits 15 s after a stdio crash, then fails clearly; a crash with a quick respawn
  serves the call.
- permanent error parks without retries; keepalive failure reconnects; close from every state.

## Decisions for the lead
- `recycled` state added (see above).
- Status strings are internal state names; display copy belongs to P13-12 (`indus mcp list`).
- Status `failures` reports the cooldown ledger (first-connect cycles); the reconnect budget is internal.

Compare checklist (filled after the rewrite; reopened `tools/mcp_tool_server_run.py`,
`tools/mcp_tool_lifecycle.py`, `tools/mcp_tool_handlers.py`):
- [x] retry delays within ±20 % and capped at 60 s (`tests/unit/mcp/test_lifecycle.py::test_backoff_delays_stay_in_the_jitter_band_and_under_the_cap`)
- [x] first connect: 3 retries, then cooldown 30 → 60 → … → 600 → 600 (`::test_first_connect_retries_three_times_then_cools_down`, `::test_cooldown_doubles_from_30_to_600`)
- [x] reconnect: 5 retries, then parked; parked probe every 300 s (`::test_unproven_drops_spend_the_budget_then_park`, `::test_permanent_error_parks_without_retries_and_probes_every_300_s`)
- [x] proven session resets the budget; flapping session does not (`::test_a_proven_session_reconnects_at_once`)
- [x] respawn wait 15 s after a stdio crash (`::test_respawn_wait_is_15_s_after_a_crash`, real process: `tests/contract/mcp/test_transports.py::test_managed_stdio_server_respawns_after_a_crash`)
- [x] idle and lifetime recycling, restart on the next call, never during a call (`::test_idle_stdio_server_recycles_and_restarts_on_the_next_call`, `::test_lifetime_limit_recycles_even_a_busy_looking_server`, `::test_recycling_waits_for_the_call_in_flight`)
- [x] keepalive 180 s, 30 s per ping, failure reconnects; `tools/list` when `ping` is not implemented (`::test_remote_keepalive_failure_reconnects`, `::test_a_hung_keepalive_gives_up_after_30_s`, `::test_a_server_without_ping_is_probed_with_a_tool_listing`)
- [x] close from every state (`::test_close_ends_every_state_at_once`, `::test_close_during_cooldown`)
- Deliberate differences: the reference parks a server whose first connects fail (300 s probes) and keeps a separate 30 → 600 s ledger for discovery passes; we use the ledger as the state itself (`cooling_down`), so one timing governs both. `recycled` is an explicit state.
- Not taken: one-time grace for an auth error on a proven session, OAuth credential stamps on park (P13-07/09), the suspect latch, failing in-flight calls fast on a deliberate teardown (they fail when the transport closes), pausing the park probe for a server disabled on disk (P13-08 reconcile owns it), cross-profile connection adoption.

## Status

Done (2026-10-19). `src/indus/mcp/lifecycle.py`, `tests/unit/mcp/test_lifecycle.py`.
