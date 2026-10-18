# Task: Agent object + event emitter (P04-20) Phase: 04 Owner: wave-6b builder S

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `run_agent.py` (the constructor
surface and its callback parameter list, `close`/`release_clients` and their guarded phases;
`main` skipped), `agent/agent_init.py` (`init_agent`: the phase list at the end, and `_load_tools`
for what the tool phase owns), `agent/turn_facade.py` (`chat` only: one turn, final text back).
Spec: research `01-agent-core.md` §3.1 (agent object), trap R01.16; ours:
`docs/notes/P04/{turn-loop,interrupts,route-and-d04-1,persist-order,prompt-lifecycle}.md`, the
wave 5 / 6a landing notes. Closed before writing.
Evidence: none.

What it does (own words): the `Agent` is the one object a host holds per session. It wires the
pieces the loop needs once, in a fixed order, keeps them across turns, and turns the loop's
side channels into typed protocol events. A host (the `SessionService` of P04-21, or a test)
asks it to run a turn and gets a `TurnResult`; `chat(text)` is the "just give me the answer"
form. While a turn runs, the host reaches it through the agent (interrupt / steer / redirect),
all on the loop thread. `close()` gives back what the agent holds: it stops a running turn,
releases clients it owns and runs the cleanups that tool backends registered for its task id,
each step guarded, exactly once.

Construction order (documented in the module and fixed; the reference loads clients before tools
and tools before the session state, and reads config sections after both):
1. **route** — `RouteBinding.bind(scope, route)` (or a ready binding): window, reasoning, client.
2. **event sink** — one `EventSink` per session (a plain callable is wrapped).
3. **client** — `BoundCaller`, the `ModelCaller` over the binding (see R01.5 below), with
   `agent.api_max_retries` passed through (`clamp_max_retries`).
4. **tools/definitions** — registry (given, or a fresh one with the built-in modules loaded),
   `toolsets.select(selection, disabled, platform=…)`, then `definitions(…)`.
5. **session** — session id (given or new), transcript store (given, else in-memory), prompt
   lifecycle (+ `SessionPrompts` when the store is the P02 one), `SessionRuntime`.
6. **config sections** — `LoopOptions.from_scope` (`agent.max_turns`), context window and
   reasoning effort taken from the bound route state.
7. **context engine slot** — `context_engine = None` (P08 fills it).

Construction is async (`await Agent.start(...)`, or `Agent(...)` then `await agent.open()`):
the route phase resolves the context window through P03, which may ask the endpoint.

R01.5 (route moves during a call): `BoundCaller.call` reads `binding.state` once per request; a
request built for another model is answered `RouteChanged` without being sent; every retry
attempt first checks `binding.generation` and a moved generation ends the call `RouteChanged`
(the loop refunds the iteration and rebuilds the request on the new route). A route dependent
registered by the agent keeps the runtime in step after a switch (options' window and effort,
the prompt context's model/provider); `prompt_identity(lifecycle)` rewrites the prompt lines.

Events (`core/events.py`): `EventSink(session_id)` owns the session's `seq` counter, a bounded
replay buffer (`since(seq)` for reconnects), event listeners and server-request listeners.
Every listener failure is logged and never reaches the turn. Helpers build typed events from
callback-shaped calls: `text`, `reasoning`, `tool_started`, `tool_progress`, `tool_completed`,
`status`, `notice`; server requests (approval, clarify, sudo, secret) are delivered by the broker
through `sink.request`.

Scope (R01.16): every task the agent starts goes through `agent.spawn`, which runs the coroutine
in a copied context with the agent's own `Scope` set (`spawn_with_scope`), so a background task
keeps its profile even when the caller's current scope is another one.

Inputs → outputs: `Agent(scope, route, toolset_selection, options, sink)`;
`await run_turn(message, *, system=None, history=None, persist_text=None) -> TurnResult`;
`await chat(text) -> str`; `start_turn(...) -> RunningTurn` (task + control, for the service);
`interrupt(generation=None, *, hard=False)`, `steer(text, ticket=None)`, `redirect(text)`;
`on_close(fn)`; `await close()`. Cleanup: `CleanupRegistry.register(name, fn(task_id))`,
`await registry.run(task_id) -> errors`; `TASK_CLEANUPS` is the process registry backends use.

Files: `src/indus/core/agent.py`, `src/indus/core/events.py`, `src/indus/core/cleanup.py`,
`tests/unit/core/test_agent.py`.

Edge cases / failure modes:
- a raising sink or listener → logged once per failure, the turn completes.
- `run_turn` while a turn runs → `AgentBusy` (the service maps this to busy before it gets here).
- `close()` twice → the second is a no-op; a turn still running is interrupted (by system) and
  awaited; a raising cleanup is logged and the others still run.
- a background task started from inside another profile's scope → sees the agent's scope.
- route switched while a call retries → the retry is dropped, the loop rebuilds (R01.5).
- `chat` of a failed turn → the failure text the funnel produced (never an exception).

Our design differences / decisions:
- No callback keyword list on the constructor: one sink with typed events (the reference takes
  some twenty callbacks). Tool progress has a sink helper but no tool reaches it yet (P05 adds a
  progress hook to `ToolContext`).
- `run_turn` and `chat` are coroutines (asyncio core, D-04.1); synchronous callers use
  `InProcessHost` (P04-21).
- `system=` is the per-session extra system message; it only affects a prompt that is (re)built
  (the stored prompt is reused byte for byte, R01.1).
- Clients from the shared pool are never closed by the agent; clients from a caller's factory are
  (they belong to this agent).
- Not taken: memory provider, child agents, trajectory saving, usage/cost counters, checkpoints,
  credential pools, fallback chain, quiet-mode printing (later phases).

Tests we will write (`tests/unit/core/test_agent.py`): construction order recorded; deltas arrive
in order with strictly growing seq; a raising sink does not fail the turn; background task sees
the agent's scope under another current scope; `close()` runs registered cleanups once (also on
a second close) and survives a raising one; tool round end to end with a registered fake tool;
route switch between turns uses the new model; a switch during a retry wait drops the retry;
`api_max_retries` reaches the caller; `chat` returns the final text; busy agent refuses a
second turn; interrupt through the agent; replay buffer `since`.

## Decisions for the lead
- `Agent` construction is async (`Agent.start`, or `Agent(...)` + `await agent.open()`); the
  constructor alone does no I/O. `run_turn`/`chat` are coroutines; sync callers use
  `InProcessHost`.
- The session id is settled in the **sink** phase (events carry it), not in the session phase;
  the session phase builds the store, prompt lifecycle and runtime.
- `EventSink` keeps the last 2,000 events per session for `events(session, since)`; older ones
  are dropped (a reconnect after that reloads the transcript).
- Shared one-field change in `src/indus/core/model_call.py`: `RouteCaller.still_current`
  (default always true). Each attempt, retries included, first asks it and returns
  `RouteChanged` without sending when the route moved — the only way to drop a retry inside
  P03's `retry_call` without copying it.
- `AgentBusy`/`AgentClosed` carry code `session.busy` (no new `ERROR_CODES` row).
- Tool progress: `EventSink.tool_progress` exists, but `ToolContext` has no progress hook yet;
  P05 adds one when the first long-running tool lands.
- Task cleanups run at `close()` only; the reference also clears per-task resources at turn end
  for one-shot tasks — P05's terminal/browser backends decide whether they need a turn-end hook.

Compare checklist (filled after the rewrite; reopened `run_agent.py` constructor, `close`,
`release_clients` and their phase helpers; `agent/agent_init.py` `init_agent` tail and
`_load_tools`; research `01` §3.1; no gap found beyond the decisions above):
- [x] construction phases in a fixed, documented order; later phases read only what earlier ones
  set (test: `tests/unit/core/test_agent.py::test_phases_run_in_the_documented_order`)
- [x] callbacks become typed events through one sink; a failing listener is logged, never fatal
  (tests: `::test_deltas_arrive_in_order_with_growing_seq`, `::test_a_raising_sink_never_fails_the_turn`,
  `::test_sink_helpers_build_typed_events_and_replay`, `::test_sink_listeners_and_request_delivery`)
- [x] clarify/approval go through the broker (tests: `tests/unit/service/test_session_service.py::test_an_answered_approval_lets_the_turn_finish`,
  `::test_interrupt_withdraws_a_pending_approval`)
- [x] `run_turn(message, system?, history?, persist_text?)` and `chat(text)` (tests:
  `::test_deltas_arrive_in_order_with_growing_seq`, `::test_tool_round_end_to_end`)
- [x] close: every phase guarded, idempotent, task resources released per task id (tests:
  `::test_close_runs_every_cleanup_once`, `::test_cleanup_registry_replaces_and_removes`,
  `::test_close_interrupts_a_running_turn`)
- [x] R01.16: background work keeps the agent's profile (tests:
  `::test_background_task_keeps_the_agents_scope`, `::test_the_turn_task_runs_under_the_agents_scope`)
- [x] R01.5: retry dropped when the route moved; new route used after a switch (tests:
  `::test_a_retry_is_dropped_when_the_route_moves` — fails without `still_current`,
  `::test_route_switch_between_turns_uses_the_new_model`)
- [x] `agent.api_max_retries` reaches the caller (test: `::test_api_max_retries_reaches_the_model_caller`)
- Deliberate differences: one typed sink instead of ~20 callback keywords; the route (window
  resolution and the 64K floor) is bound first, where the reference resolves the window late in
  the config phase; async construction; clients from the shared pool are never closed by an
  agent, clients from a caller's factory are.
- Not taken: interim-assistant and tool-generation callbacks (deltas and tool events cover the
  screen; P06 may add a "tool call being written" status), step callback, reaction callback,
  desktop preview/theme/tour callbacks (other areas), memory provider shutdown (P09), child
  agents (delegation), ending the session row on close (sessions stay resumable; P02 end
  reasons are set by the paths that really end a session), trajectory saving, heap trimming.

## Status

Done (2026-10-18). Files: `src/indus/core/agent.py`, `src/indus/core/events.py`,
`src/indus/core/cleanup.py`, `tests/unit/core/test_agent.py`, `tests/fakes/agent.py` (helpers
shared with the service tests). Shared: `src/indus/core/model_call.py` (`still_current`).
