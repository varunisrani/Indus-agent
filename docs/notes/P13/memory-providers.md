# Task: External memory provider API (P13-24) Phase: 13 Owner: wave-10 builder M3

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/memory_provider.py` (method
list, defaults, core sentinels, trivial-prompt gate), `agent/memory_manager.py` (one-provider rule,
tool schema normalising and reserved names, egress scrub walk, prefetch thread with its timeout and
"still running → skip" rule, single serial background worker, drain with abandonment counts,
mirroring of memory-tool writes, pre-compress, delegation, shutdown order),
`website/docs/user-guide/features/memory-providers.md` (skim: quick start, `memory setup` /
`status`, how-it-works list, egress note). Closed before writing.
Research: `docs/plan/research/07-learning-skills-plugins-cron.md` §3.2. Evidence: none.

What it does (own words): besides the two built-in stores (P09), one external memory provider —
named by `memory.provider`; empty or `builtin`/`none` = built-in only — can run in a session. It is
additive: the built-in tool, files and frozen block keep working unchanged. The `ProviderManager`
owns that one provider for a session:
- `start` checks `is_available()`; unavailable → one warning with `unavailable_reason()`, the
  manager stays inert; otherwise `initialize(session_id, scope)` with the profile home.
- prompt: `system_prompt_block()` rendered once, wrapped in our tag, and frozen for the session
  (resume/compaction rebuilds cut it back out of the stored prompt, like P09-04); it lands in the
  builder's volatile tier right after the user profile.
- before each turn: `on_turn_start(n, message)` then `prefetch(query)` run off the loop with a
  bounded wait of 8 s; a late prefetch is abandoned (left to finish) and while it is still running
  later turns skip prefetch. The recall comes back whole up to max(10 × spill threshold,
  100,000 chars); with `memory.provider_recall_spill` on, recall over the plain spill threshold is
  replaced by head + tail and the path of a file holding all of it. `recall_status()` gives a
  one-line indicator.
- after the response: `sync_turn(turn)` and `queue_prefetch(next query)` on one serial background
  worker (FIFO, so turn N lands before N+1).
- mirrors every committed built-in memory write (`on_memory_write(action, target, content, meta)`;
  `patch` arrives as the whole entry after the edit with `previous` in meta; staged writes are not
  mirrored).
- provider tools: offered only when the `memory` tool is selected; names that collide with
  registered tools are refused; calls routed to the provider with their args scrubbed.
- `on_session_end` (extract), `on_session_switch`, `on_pre_compress` → text for the summary,
  `on_delegation(task, result, child_session_id)`.
- `shutdown` drains the worker for at most 5 s (what is left is abandoned and counted), then
  `shutdown()` on the provider.
Everything handed to the provider passes the secret redactor in force mode first (turn text, tool
output in messages, queries, transcripts, mirrored writes, delegation results, its own tool args),
even when `security.redact_secrets` is off. A provider failure never breaks a turn: each call is
guarded and logged.

Inputs → outputs: config `memory.provider`, `memory.provider_recall_spill`; the provider catalog
(plugins register factories, P13-16); turn text, transcripts, memory-tool write events →
provider calls, recall text, a frozen prompt block, tool schemas/results, a drain report;
`indus memory setup [NAME]` writes `memory.provider` and the provider's config, `indus memory
status` shows provider state.

Files: `src/indus/learning/memory/providers.py`, `tests/unit/learning/memory/test_providers.py`.
Shared: `src/indus/learning/memory/tool.py` (write listeners for mirroring),
`src/indus/core/prompt/builder.py` (`BuildContext.memory_provider_block` slot, volatile tier),
`src/indus/foundation/config/schema.py` (+ pinned-defaults test: `memory.provider`,
`memory.provider_recall_spill`), `src/indus/cli/commands/memory.py` (`setup`, provider lines in
`status`).

Edge cases / failure modes:
- two providers configured (a list, or a second registration) → only the first named one is
  active; the rest are logged and ignored;
- configured provider not in the catalog → one warning per process, built-in only;
- provider raises in any hook → logged, turn goes on; `is_available()` raising → unavailable;
- prefetch slower than 8 s → the turn waits 8 s at most, the next turn skips while it still runs;
- empty / slash-command / trivial acknowledgement query → no prefetch;
- tool schema without a name, or in the wrapped `{"type": "function"}` form → normalised or
  dropped with a warning;
- late background work after shutdown starts → refused with a warning;
- shutdown with a stuck sync → returns after 5 s, reports abandoned writes/prefetches.

Our design differences / decisions:
- async manager (the core is asyncio): blocking provider calls run on a manager-owned
  `foundation.blocking.BlockingPool` (prefetch, tools, hooks) and one serial daemon worker
  (sync/queue/mirror); waits are measured on the scope's clock, so tests use the fake clock;
- `initialize(session_id, scope)` takes a typed `ProviderScope` (home, profile, platform, agent
  context, parent session) instead of free keyword arguments; no signature sniffing — the
  protocol is fixed, and `BaseMemoryProvider` gives no-op defaults;
- mirrored actions keep our names (`add`/`patch`/`remove`), the patched whole entry as content;
- no per-profile multiplexing checks (one manager per session, built with the session's scope);
  no checkpoint API versions for pre-compress; no "learned token" cross-masking (the redactor's
  exact-value pass already masks known secrets everywhere).

Tests we will write (fake provider):
- second provider configured → only the first named active; missing provider warns once;
- prefetch slower than 8 s does not delay the turn beyond 8 s (fake clock); skipped next turn;
- a secret in a user turn reaches the provider redacted (sync, prefetch, tool args, mirror);
- built-in add mirrored once (real `MemoryTool`), staged write not mirrored;
- shutdown waits ≤ 5 s (fake clock) and reports the abandoned write;
- provider block frozen across turns and restored from the stored prompt;
- tools hidden without the memory tool; reserved name refused; recall ceiling and opt-in spill;
- `indus memory setup` / `status`.

## Decisions for the lead
- Host wiring (adding the manager to `learning.host.wire_session`, staging recall as a turn note,
  `after_turn` sync) is not done here: the plugin context that registers providers (P13-16) is in
  another slice. The manager exposes everything a host needs (`builder`, `inline`, `before_turn`,
  `after_turn`, `mirror`, `shutdown`).
- Catalog: `register_provider(name, factory)`; first registration of a name wins (bundled before
  user before project), as research §3.2 says.
- Config keys: `memory.provider` (string, "" default) and `memory.provider_recall_spill` (false).

Compare checklist (filled after the rewrite; reopened the three reference files):
- [x] one external provider; first named wins (tests: `test_only_first_named_provider_is_active`, `test_core_names_mean_builtin_only`) — matches `add_provider`/`is_core_memory_provider`.
- [x] prefetch bound 8 s, skip while still running (test: `test_slow_prefetch_bounded_by_eight_seconds`).
- [x] drain ≤ 5 s with abandonment counts (test: `test_shutdown_waits_at_most_five_seconds`); clean drain (`test_clean_shutdown_drains_and_closes_provider`).
- [x] egress scrub of every hand-off (tests: `test_tool_args_reach_provider_redacted`, `test_prefetch_secret_reaches_provider_redacted`, `test_after_turn_syncs_and_queues_next`, `test_delegation_result_redacted`).
- [x] recall ceiling max(10×, 100k) and opt-in spill (tests: `test_recall_ceiling_caps_oversize_recall`, `test_opt_in_spill_replaces_oversize_recall`).
- [x] mirroring only committed writes; patch as changed entry (tests: `test_builtin_add_mirrored_once`, `test_patch_mirrored_as_changed_entry`, `test_staged_write_is_not_mirrored`).
- [x] missing provider warns once (test: `test_missing_provider_warns_once_and_runs_builtin_only`); unavailable parks inert (`test_unavailable_provider_warns_and_stays_inert`); block frozen (`test_provider_block_frozen_across_turns`, `test_frozen_block_restored_from_stored_prompt`); tools gated (`test_tools_hidden_without_memory_toolset`).
- Deliberate differences: async manager over `foundation.blocking` pools + one serial worker (the reference uses threads and a single-worker executor); fixed protocol with `ProviderScope` and `MemoryWrite` instead of duck-typed signature sniffing; `notify_tool_write` lives in the manager (reference `notify_memory_tool_write`); recall spill reuses `tools.spill`; no checkpoint API versions, no multi-profile instance-claim, no learned-token cross-masking (the force redactor's exact-value pass covers known secrets).
- Not taken: the pre-compress checkpoint v2 contract; gateway identity multiplexing; per-call provider metrics; bundled providers (none shipped — registry is empty until a plugin registers one).

## Status

Done (2026-10-19). Shared: `foundation/config/schema.py` (+ pinned-defaults test) `memory.provider` / `memory.provider_recall_spill`; `core/prompt/builder.py` `BuildContext.memory_provider_block` slot in the volatile tier (empty by default, golden prompts unchanged); `cli/commands/memory.py` `setup` / `off` / provider status line. Host wiring (manager into `learning.host.wire_session`, staging recall as a turn note, `after_turn` sync, `notify_tool_write` after the memory tool) is deferred to the plugin-context/prompt-sections slice (P13-16/P13-18); the manager exposes everything a host needs.
