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
