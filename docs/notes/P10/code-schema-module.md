# Task: `execute_code` in-script module and schema (P10-09) Phase: 10 Owner: P10 wave-1 builder H

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/code_execution_tool.py`
(stub templates, module generator, helper block, failure hints, schema builder). Spec: research
`02-tools.md` §3.19, §2 (R02.21), naming `17-naming-and-glossary.md` §a8. Closed before writing.
Evidence: none.

What it does (own words): the Python module a script imports to reach our tools, and the tool
description the model reads.

- `indus_tools` has one function per allowed tool: `read_file`, `write_file`, `patch`,
  `search_files`, `web_search`, `web_extract`, `terminal` — the ones in this session's enabled
  tools only. Each sends `{tool, args, token}` and returns the tool's JSON result parsed (a dict),
  or the raw text when it is not JSON. `terminal` is foreground only: `background`, `pty`,
  `notify`, `notify_on_complete`, `watch_patterns`, `heartbeat`, `persist_on_release` are refused
  (by the host; the stub does not offer them).
- Helpers: `json_parse(text)` (lenient: control characters, a leading BOM), `shell_quote(s)`,
  `retry(fn, max_attempts=3, delay=2)` (doubling waits).
- Never offered: `execute_code`, `delegate_task`, MCP tools, anything not in the allow-list.
- The description is rebuilt per session from (enabled ∩ allowed) through the registry's
  `rewrite` hook (it sees which tools survived), so it never names a tool the session lacks
  (R02.21). It names the limits and the cwd rule of the configured mode.
- Failure hints appended to an error result: importing a name the module lacks (lists what is
  there), calling a helper without importing it, treating a result dict as a string, importing a
  third-party module that is not installed.

Inputs → outputs: tool names + transport (`socket` | `file`) → module source; tool names + mode
→ description text; stderr/traceback text → one hint or None.

Files: `src/indus/tools/builtin/code/stub_module.py`, `src/indus/tools/builtin/code/schema.py`,
`tests/unit/tools/builtin/code/test_schema.py`, golden `tests/unit/tools/builtin/code/golden/cli_description.txt`.

Edge cases / failure modes:
- empty intersection: the module has only helpers, the description says no tools are offered.
- concurrent calls from script threads: one lock per module around each round trip.
- a long-lived kernel whose socket connection dropped: the stub reconnects once.
- file transport: request written to a temp name and renamed; owner-only; seq numbers under a
  lock; per-call wait ≤ 300 s with growing poll gaps.

Our design differences / decisions:
- Module name `indus_tools`; wire answers carry `result` or `refused` (refusals raise
  `ToolCallRefused` in the script).
- An empty intersection does not fall back to every tool (the reference does).

Tests we will write:
- session without web tools → neither description nor module mention `web_search`;
  `terminal(background=True)` in a script → refused error; each hint on its synthetic failure;
  golden description for the CLI toolset; module compiles for every subset.

## Decisions for the lead
- none beyond P10-08's.

Compare checklist (filled after the rewrite; reopened `tools/code_execution_tool.py`):
- [x] helper names and defaults (`tests/unit/tools/builtin/code/test_schema.py::test_helpers`)
- [x] hint classes: missing name (helper and tool), helper not imported, result is a dict, not installed
  (`::test_failure_hints`, `::test_hint_reaches_the_result`)
- [x] description lists only the intersection, helpers text names no tool (`::test_session_without_web_tools_never_mentions_them`,
  `::test_empty_intersection_has_only_helpers`); golden for the CLI toolset (`::test_golden_description_for_the_cli_toolset`)
- [x] terminal foreground only (`::test_terminal_background_in_a_script_is_an_error`)
- Deliberate differences: an empty intersection offers no tools (the reference falls back to all of them);
  the description follows `code_execution.mode`/`kernel` through `schema_overrides` + `rewrite`; an extra hint
  for a failed import of the module itself.
- Not taken: the reference's `cross_profile` argument on write_file/patch stubs (no such argument in our tools).

## Status

Done (2026-10-19). Files: `src/indus/tools/builtin/code/{stub_module,schema}.py`,
`tests/unit/tools/builtin/code/test_schema.py`, `tests/unit/tools/builtin/code/golden/cli_description.txt`
(regenerate when the description changes and say why in this note).
