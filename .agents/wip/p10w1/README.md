# P10 wave 1 — slice reports

## Slice H — w15-p10h.patch (base 85cd8a9, before P09 w1 landed) — DONE (P10-08..11 execute_code)
- New: tools/builtin/code/{__init__,settings,output,launch,rpc,gate,runner,stub_module,schema,kernel,
  kernel_runner,remote,remote_runner}.py; tests/unit/tools/builtin/code/* (golden cli_description.txt);
  docs/notes/P10/code-{runner-rpc,schema-module,kernel,remote}.md.
- Shared: tools/builtin/__init__.py (BUILTIN_MODULES row); tools/toolsets.py (`code_execution`
  toolset, execute_code in CORE_TOOLS) + test; core/tool_round.py handler_extras (tool_registry,
  tool_hooks, enabled_tools); config `code_execution` section + test; test_wizard.py (core bundle +
  code_execution — P09 w1 also edited this list: union); tests/golden/prompt_surface/*/tools.json
  (regenerate after merge); traps R02.2/21/22/32; ACC-C35-1..3, E9-1.
- Wiring: host exit `await kernel.POOL.shutdown()` and `await remote.POOL.shutdown()`; long-lived
  hosts run `kernel.run_reaper(clock, idle_fn)`; /new and session close `await
  kernel.dispose_session(id)`; G (P10-06) passes `code_kernel_owner=kernel.child_owner(parent, child)`
  and calls `await kernel.dispose_child(child_id)` at teardown.
- Decisions: E9 approval (interactive asks once per session — the reference never asks; unattended
  follows approvals.*_mode so `indus -z` refuses by default); kernel auto/off; ToolCallRefused
  exception; empty tool intersection → helpers only; pinned child kernels exempt from idle sweep;
  remote `python` ≥3.8 fallback; 0.5 s remote poll serves both results and tool requests.
- Follow-ups: public Environment.temp_dir; sweep crashed-host staging dirs >7 d; move RemoteShell to
  tests/fakes/backend/.

## Slice G — w15-p10g.patch (base 85cd8a9) — DONE (P10-01..07 delegation); uv.lock EXCLUDED — re-lock
- New: tools/builtin/delegate.py; core/subagents/{__init__,delegation,child,batch,output_schema,
  background,teardown,worktree}.py; tests/unit/core/subagents/*, tests/unit/tools/builtin/test_delegate.py,
  tests/fakes/subagents.py, tests/integration/core/test_delegate_batch.py (M2 demo);
  tests/unit/core/subagents/_stubs_g.py (STAND-IN for H's kernel pool — delete); docs/notes/P10/ ×7.
- Shared: core/agent.py (AgentOptions.only_tools, delegation_depth; lazy delegate executor);
  core/turn_state.py (max_child_calls from delegation.max_concurrent_children); tools/builtin/__init__.py;
  tools/toolsets.py (`delegation` toolset, delegate_task in CORE_TOOLS — H and P09 w1 also edit);
  config DelegationSection + test; backends/processes.py (handoff grants, transfer_ownership,
  handoff_note); tools/builtin/process_manage.py (`handoff` live, `purpose` arg) + test;
  service/session_service.py (deliver_internal, stop_subagents); cli/slash/port.py (/stop, /new stop
  subagents); goldens tests/golden/prompt_surface/*/tools.json (regenerate after merge);
  test_toolsets.py, test_wizard.py (union with H and P09 w1); pyproject.toml jsonschema==4.26.0
  (MIT, 2026-01-07; pulls attrs, referencing, jsonschema-specifications, rpds-py — all MIT);
  THIRD_PARTY_NOTICES.md; traps R01.2/4/16, R02.17/32; ACC-C36-1.
- Wiring: H's kernel pool registers via core.subagents.teardown.register_releaser(...) and uses
  child_kernel_key (H's report: child passes code_kernel_owner=kernel.child_owner(parent, child),
  dispose_child at teardown) — reconcile the two names into one; then delete _stubs_g.py.
- Decisions: delegate_task in CORE_TOOLS; background only with a delivering host + interactive
  context; usage rolled up on the parent ledger only; hand-off `purpose` arg; steer via P04 steer row;
  async cap queues (reference rejects); images listed as paths/URLs until P10-15; /stop count
  includes subagents.
- Follow-ups: no subagent.* protocol events yet; hand-off note not shown in completion text; Docker
  container aliasing for children.
