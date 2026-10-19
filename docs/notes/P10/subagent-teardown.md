# Task: child teardown and resource hand-off (P10-06) Phase: 10 Owner: P10 slice G builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/process_registry.py`
(`transfer_ownership` only), `tools/code_kernel.py` (child keying, disposal). Closed before writing.
Evidence: none.

What it does (own words): when a child ends, background processes it started are killed unless it
handed them to the parent. `process_manage action=handoff` works only inside a child (a grant is
registered for the child's session), at most 3 per child, needs a `purpose` sentence, and moves
the job's owner to the parent session (completion then goes to the parent, notify on when the
parent can receive it). Jobs left behind are killed and listed in the child's result as
`orphaned_processes`. Child code kernels (keys `<parent>::child::<child>`) are disposed through a
disposer hook (the kernel module of P10-10 registers it); browser sessions and vision counters
likewise through registered releasers. `subagent_start` / `subagent_stop` hook events go to a
hook bus (a no-op until P13); `subagent_stop` is emitted on the caller's task.

Inputs → outputs: child end → killed/handed-off jobs, disposed resources, hook events.

Files: `src/indus/core/subagents/teardown.py`, `tests/unit/core/subagents/test_teardown.py`;
shared: `src/indus/backends/processes.py` (`transfer_ownership`), `src/indus/tools/builtin/process_manage.py`
(handoff action through grants).

Edge cases / failure modes:
- 4th handoff → refused, the job stays the child's.
- Parent (no grant) calls handoff → refused with the old "subagents only" text.
- Handoff of an ended or foreign job → refused.

Our design differences / decisions:
- Kernel/browser/vision release are seams (registered callables) until P10-10/12/15 land.

Tests we will write:
- two jobs, one handed off (parent owns and gets completion), other killed and orphaned; 4th
  handoff refused; parent handoff refused; kernel disposer called and absent from LRU stand-in;
  hook events recorded by a fake bus.

Compare checklist (reopened `tools/process_registry.py` `transfer_ownership`/`_handoff_process`):
- [x] hand-off only in children, ≤ 3, purpose required (tests: `tests/unit/core/subagents/test_teardown.py::test_fourth_handoff_is_refused`, `::test_handoff_rules`)
- [x] parent owns and gets the completion; leftovers killed and orphaned (test: `::test_handed_off_job_survives_and_reports_to_the_parent`)
- [x] child kernel disposed, outside the LRU (test: `::test_child_kernel_is_disposed_and_never_in_the_lru`, real `kernel.POOL` since integration)
- [x] start/stop hook events, stop on the caller's task (test: `::test_handed_off_job_survives_and_reports_to_the_parent`)
- Deliberate differences: the purpose travels in a new `purpose` argument (reference: `data`).
- Follow-up: the reference adds the hand-off note to the completion notice; ours keeps it on the
  record (`handoff_note`) only — `process_notify.completion_text` could show it.

## Decisions for the lead (added)
- At integration, slice H's kernel pool registers its releaser with
  `core.subagents.teardown.register_releaser` and `tests/unit/core/subagents/_stubs_g.py` goes.

## Integration (lead, P10 slices G+H)
- One naming: the kernel owner is `kernel.child_owner(parent, child)` (`<parent>::child::<child>`);
  `teardown.child_kernel_key` was removed. `teardown` registers the built-in `code-kernels`
  releaser (`kernel.dispose_child`, local and remote pools); `build_child` sets
  `SessionRuntime.code_kernel_owner`, which the tool round offers as the `code_kernel_owner` extra.
- `_stubs_g.py` deleted; `test_teardown` drives the process pool with stub kernels;
  `tests/integration/core/test_subagent_host.py::test_a_subagents_kernel_is_its_own_and_ends_with_it`
  runs a child's `execute_code` end to end through `indus -z`.

## Status

Done (2026-10-19). Shared: `backends/processes.py`, `tools/builtin/process_manage.py` (+ its test's
expected handoff text).
