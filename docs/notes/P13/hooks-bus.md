# Task: Hooks bus (P13-17) Phase: 13 Owner: wave-10 builder M2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/plugins_dispatch.py`
(bounded set, fail-closed set, caller-thread set, suppression, abandoned cap, failure reporting,
event queue caps), `hermes_cli/plugins.py` (the hook-name set and the pre-tool directive
merging only), `website/docs/user-guide/features/hooks.md` (skim; its hook count is stale,
R07.30). Research §3.13.3, trap R07.18. Our `docs/notes/P04/{dispatch,blocking-helper,
d-04-1-decision}.md`. Closed before writing.
Evidence: none.

What it does (own words): plugins observe and steer the agent through named hooks. The bus keeps
the callbacks per hook name and calls them with keyword arguments. Some hooks decide something
(block or rewrite a tool call, add context to the user message, classify an API error), some
transform a value, most only observe. A plugin must never be able to freeze the agent: callbacks
on the hot path have a deadline, a callback that missed it is skipped for a while, and it can strand
only a few worker threads. The one exception to "a failing hook is ignored" is `pre_tool_call`: a
guard that hangs or crashes made no decision, so the tool does not run (R07.18).

Inputs → outputs: `HookBus(*, clock, timeout_s=30, suppress_s=60, max_abandoned=3, pool="hooks")`,
`HookBus.from_scope(scope)`; `register(name, callback, owner) -> Registration`; `has(name)`;
`await invoke(name, **payload) -> list`; typed helpers `pre_tool_call(...) -> ToolDirective`,
`pre_llm_call(...) -> str | None` (joined context), `transform(name, **payload)` (first non-None),
`pre_transcription(...) -> dict` (last writer wins per key), `pre_verify(...)`,
`pre_gateway_dispatch(...)`, `classify_api_error(...)` (first valid), `observe(name, ...)`.
`PluginToolHooks(bus, ask_approval=None)` implements P04's `ToolHooks` seam.
`EventBus`: `emit(name, payload) -> int`, `listen(name, callback, owner)`, `await drain()`.
`with_llm_context(message, context)` puts the context into the user message's `api_content`.

Files: `src/indus/plugins/hooks.py`, `src/indus/tools/hooks.py` (shared),
`src/indus/foundation/blocking.py` (shared: `hooks` pool size and queue),
`tests/unit/plugins/test_hooks.py`, `tests/unit/tools/test_hooks_seam.py`.

Hook names (D-16.39, generic, 37 accepted; R07.30: the reference user doc says 27, the code
accepts 46 including 8 board hooks and a room hook we do not have): directive `pre_tool_call`,
`pre_llm_call`, `pre_verify`, `pre_gateway_dispatch`, `transform_api_error_classification`;
transform `transform_tool_result`, `transform_terminal_output`, `transform_llm_output`,
`pre_transcription`; observers `post_tool_call`, `post_llm_call`, `pre_api_request`,
`post_api_request`, `api_request_error`, `pre_auxiliary_call`, `post_auxiliary_call`,
`on_stream_start|delta|end`, `on_interim_message`, `on_session_start|end|finalize|reset|delete`,
`post_compaction`, `agent_loop_stopped`, `on_skill_lifecycle`, `subagent_start`,
`subagent_stop`, `pre_approval_request`, `post_approval_response`,
`on_human_input_request|resolved`, `pre_command`, `post_gateway_admission`,
`gateway_platform_event`. An unknown name warns once and is still stored.

Rules:
- callbacks get keyword arguments, narrowed to what a narrow signature declares (`**kwargs`
  gets all); sync callbacks run on the `hooks` blocking pool (D-04.1), async ones on the loop.
- bounded hooks (`pre_tool_call` and the hot-path set: tool, LLM, API, auxiliary, stream,
  session start/end, verify, command, admission, compaction) use `plugins.hook_callback_timeout`
  (30 s; 0 = off; max 600). A miss abandons the worker (never joined), suppresses that callback
  for 60 s and counts it as abandoned until its thread returns; at 3 live abandoned workers the
  callback is skipped. Pool refusal counts as a failure.
- `pre_tool_call` failure (timeout, exception, suppressed, at the abandoned cap, pool refusal)
  → block. Every other hook fails open (skipped, logged).
- exceptions logged once per (hook, callback, error); repeats at debug.
- `subagent_stop` runs in the caller's task, never on a worker.
- directives for `pre_tool_call`: block (needs a message) > approve (first valid; the human gate
  decides; no gate, denial or gate error → block) > modify (shallow merge, later wins per key).
- `pre_llm_call`: `{context}` or a string; joined with blank lines in registration order; goes
  into the user message (`api_content`), never the system prompt.
- event bus: emit depth cap 8 (a subscriber's emit carries its depth), pending cap 64 (queued +
  running), fire-and-forget on the emitting loop, each subscriber gets its own deep copy.

Our design differences / decisions:
- Async bus on the shared blocking helper; async callbacks bounded with the bus clock and
  cancelled on a miss (the reference cancels them too in its loop path).
- `hooks` pool: **8 workers and a wait queue of 32** within the hook deadline (D-04.1 proposal,
  LEAD-NOTES): eight lets several stuck callbacks (≤ 3 each) coexist with healthy ones; the queue
  stops a burst of parallel tool calls from being refused (and so blocked) just because 8 hooks
  are running. A queued call that cannot start before its deadline counts as a timeout.
- `tools/hooks.py`: a non-empty seam may declare `before_deadline_s`; `run_before` then also
  blocks when `before` does not answer in time (backstop); `TimeoutError` named in the message.
- Event names: host `indus.<event>`, plugins publish `<key>.<event>` (see plugin-context note).
- Not taken: middleware, safe-worker mode switch, telemetry schema version field, per-call-id
  running gate (the abandoned cap bounds threads).

Tests we will write:
- `pre_tool_call` hanging → blocked at 30 s on the fake clock; the next call within 60 s skips
  the suppressed hook (blocked at once, callback not called); after 60 s it runs again; 4th
  abandoned worker refused; raising `pre_tool_call` blocks, raising `post_tool_call` ignored;
  modify merged across two hooks; block beats approve beats modify; approve with no gate or a
  denying gate blocks; `pre_llm_call` joined text in the user message, system prompt bytes
  unchanged; transform first-non-None; pre_transcription last writer wins; classification first
  valid; emit depth 9 refused; pending cap; narrow signature; unknown hook warns; subagent_stop
  inline; seam: empty fail-open, backstop deadline blocks.

## Decisions for the lead
- Follow-up (P04 dispatch, not this slice): `post_tool_call` does not fire for a call the
  `before` seam blocked, because `dispatch` returns early; the reference reports blocked calls to
  observers too (`status: blocked`).
- Hooks pool: 8 workers + queue 32 (both proposals in LEAD-NOTES taken). The D-04.1 (d) perf
  test's last step changed: with two hooks stuck, a third session's hooked call now runs instead
  of being refused; the pool bound is pinned in `test_blocking.py`.
- `approve` needs a human gate port (`ask_approval`); P05 has no per-tool approval prompt yet, so
  until the host passes one an `approve` directive blocks (fail closed). Wire with P13-07.
- `core/prologue.py` is not changed in this slice: `pre_llm_call` context is ready
  (`HookBus.pre_llm_call` + `with_llm_context`) for the prologue to call once per user turn.

Compare checklist (filled after the rewrite; reopened `hermes_cli/plugins_dispatch.py`,
`hermes_cli/plugins.py` hook set and directive merging):
- [x] 30 s default, 0 off, 600 max (test: `test_hooks.py::test_timeout_setting`)
- [x] fail-closed pre-tool on timeout and on raise (test: `::test_hanging_pre_tool_call_blocks_then_is_suppressed`, `::test_raising_pre_tool_call_blocks`)
- [x] 60 s suppression; suppressed pre-tool blocks without calling (test: `::test_hanging_pre_tool_call_blocks_then_is_suppressed`)
- [x] ≤ 3 abandoned workers per callback (test: `::test_fourth_abandoned_worker_refused`)
- [x] observers fail open (test: `::test_raising_post_tool_call_is_ignored`)
- [x] directive precedence and modify merge (test: `::test_modify_merges_across_hooks`, `::test_block_beats_approve_beats_modify`)
- [x] pre_llm_call context joined, user message only (test: `::test_pre_llm_call_goes_into_the_user_message`)
- [x] transform first non-None; last writer for transcription; first valid classification (test: `::test_transform_first_non_none_wins`, `::test_pre_transcription_last_writer_wins`, `::test_error_classification_first_valid`)
- [x] warn once per failing signature (test: `::test_signature_mismatch_warned_once`)
- [x] caller-task hook not moved (test: `::test_subagent_stop_runs_in_the_caller`)
- [x] event caps 8 / 64 (test: `::test_emit_depth_nine_refused`, `::test_pending_cap`)
- [x] 37 generic names, unknown names kept with a warning (test: `::test_hook_names_are_generic_and_complete`, `::test_unknown_hook_warns_once_and_is_kept`)
- [x] a worker that returns frees its abandoned slot (test: `::test_abandoned_count_drops_when_a_worker_returns`)
- Deliberate differences: async bus; sync callbacks on the shared `hooks` pool; unbounded hooks
  still run off the loop when sync; `on_stream_*` are bounded here (the reference feeds stream
  observers through a separate queue); event names dotted; block message wording ours; no
  per-call-id "still running" gate (the abandoned cap and suppression bound threads instead).
- Not taken: board and room hooks (no such features yet), middleware, shell-unsupported list
  (P13-19).

## Status

Done (2026-10-19). Shared: `src/indus/tools/hooks.py`, `src/indus/foundation/blocking.py`,
`tests/unit/foundation/test_blocking.py`, `tests/integration/core/test_d04_1_criteria.py`.

## Wave 46 (builder g): `post_tool_call` for blocked calls (release checklist RS19)

Read: none from the reference (a follow-up on our own seam; no reference file reopened).

- Gap: `dispatch` returned at once when `before` blocked a call, so `after` (the bus's
  `post_tool_call`) never saw it and audit plugins missed every refused call.
- Now: a blocked call goes to `after` with the block result (error type `blocked`, the new shared
  constant `tools.hooks.BLOCKED`) and zero duration; it never goes to `transform` (a block text is
  not rewritten). `PluginToolHooks.after` adds `blocked` (bool) to the `post_tool_call` payload, so
  the same keyword is `False` for calls that ran; shell hooks get it under `extra`.
  `indus hooks test` sample payload carries `blocked: false`. An `after` that raises on a blocked
  call is logged and the block stands.
- Wrapping seams (`ChildHooks`, `LspToolHooks`, the review fork's `WhitelistHooks`) pass `after` through, so they
  see blocked calls too; none acts on them. The gateway hook bridge's `tool_done` now also emits
  `agent:step` for a blocked call (decision W46-g-1).
- Tests: `tests/unit/tools/test_dispatch.py::test_blocked_call_still_reaches_after_but_not_transform`,
  `::test_blocked_call_survives_a_raising_after`,
  `tests/unit/plugins/test_hooks.py::test_post_tool_call_sees_blocked_calls` (both failed before).

Decisions for the lead (wave 46):
- W46-g-1 (D-16.1011): the gateway hook bridge (`gateway/hooks.py` `tool_done`) is left as is, so a blocked
  call now also emits `agent:step` (a step the agent took, refused). Skipping blocked calls there is
  a one-line `if blocked: return` if the lead prefers steps to mean "a tool ran".
- W46-g-3 (D-16.1013): a blocked call skips `transform_tool_result` (the block text is the refusal the model
  must see; a transform could launder it into a success).
