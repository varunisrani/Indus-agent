# Task: Guardrails (P04-18) Phase: 04 Owner: wave-6a builder X

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/tool_guardrails.py` (defaults,
config, signature, controller before/after/observe, caps, stub, guidance text shape), `run_agent.py`
(the guardrail observation hook and the halt message). Spec: research `01-agent-core.md` §3.7.
Closed before writing.
Evidence: none.

What it does (own words): a per-turn controller watches tool calls by a stable hash of
`(tool, canonical args)` and steers the model out of loops:

- exact failure (same tool, same args, failed again): warn from the 2nd, block from the 5th;
- same-tool failure (any args): warn from the 3rd, halt from the 8th (tools whose failures are
  normal work output — terminal-like tools — never halt on this counter);
- idempotent no-progress (a read-only tool returning the same result for the same args): warn from
  the 2nd, block from the 5th;
- per-turn caps: 50 `web_search` calls, 50 subagent spawns (`delegate_task`, counting tasks in a
  batch call); `0` disables a cap. A cap blocks the call that would exceed it.

Blocks and halts are hard stops and apply only on unattended surfaces (cron, gateway, webhook, any
surface not known to be attended); attended surfaces (cli, tui, desktop, acp, api_server,
subagent) only get warnings. Caps apply everywhere. A successful mutating tool (registry trait)
resets the failure streaks. A repeat of the same call that returns byte-identical text (≥ 512
chars, not an error) enters the context as a short pointer stub naming the earlier call instead of
the full text. Guidance is appended to the tool result when the result is built (so stored rows
never change later). A block returns a synthetic error result without running the tool; a block or
halt ends the turn after the batch with `guardrail_halt` and a reply inviting the user to say
"continue".

Inputs → outputs: `before(tool, args)` → decision; `after(tool, args, result_text, failed, call_id)`
→ decision + optional stub; `guidance(decision)` → suffix text; `halt` property.

Files: `src/indus/core/guardrails.py`, `tests/unit/core/test_guardrails.py`.

Edge cases / failure modes:
- args that are not a mapping hash as `{}`.
- a mutating success between two identical failures restarts the exact streak.
- multimodal results never stub.
- the first block/halt decision of the turn is the one reported.

Our design differences / decisions:
- Idempotent = registry `read_only` trait; mutating = `mutating` trait (with a name fallback table).
- Not taken: the A-B-A-B cycle detector and the 3rd-identical-call notice (not in the phase row),
  terminal exit-code and memory-full failure parsing (P05 tools report `is_error` themselves),
  legacy flat config keys.

Tests we will write: threshold table per counter; attended vs unattended; mutating success resets;
identical-bytes stub; caps incl. 0 = off and batch spawn counting; halt → `guardrail_halt` exit via
the funnel (loop test).

## Decisions for the lead
- Thresholds are `GuardrailConfig` fields; no config section yet (`tool_guardrails.*` can come with
  P05 when the tools that loop exist).

Compare checklist (filled after the rewrite; reopened `agent/tool_guardrails.py`, `run_agent.py`
(observation hook, halt reply)):
- [x] thresholds 2/5, 3/8, 2/5 (tests: `tests/unit/core/test_guardrails.py::test_exact_failure_warns_at_two_and_blocks_at_five`,
  `::test_same_tool_failure_warns_at_three_and_halts_at_eight`, `::test_idle_no_progress_for_read_only_tools`)
- [x] caps 50/50, 0 disables, control actions spawn nothing (tests: `::test_web_search_cap[*]`,
  `::test_subagent_cap_counts_tasks_in_a_batch_call`)
- [x] attended/unattended split; caps regardless (test: `::test_attended_surfaces_only_warn`)
- [x] noisy tools never halt on different arguments (test: `::test_noisy_tools_never_halt_on_different_arguments`)
- [x] mutating success resets streaks (test: `::test_mutating_success_resets_failure_streaks`)
- [x] identical-bytes stub (test: `::test_identical_bytes_become_a_pointer_stub`)
- [x] guidance appended at construction; halt ends the turn (tests: `::test_guidance_and_block_texts`,
  `tests/unit/core/test_loop.py::test_every_exit_passes_the_funnel[guardrail_halt]`)
- Deliberate differences: the stub compares with the previous call only (same signature and same
  bytes), not a per-signature history; unknown surface names count as attended.
- Not taken: cycle detector, 3rd-identical notice, terminal exit-code / memory-full parsing,
  spill-path hint inside the stub, legacy flat config keys.

## Status

Done (2026-10-18). Files: `src/indus/core/guardrails.py`, `tests/unit/core/test_guardrails.py`.
