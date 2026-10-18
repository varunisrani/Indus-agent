# Task: Dispatch contract (P04-09) Phase: 04 Owner: wave-5 builder D

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `model_tools.py` (whole file:
`handle_function_call` flow, `_run_async`, error sanitiser, hook/middleware seams, agent-loop
tool refusal), `tools/registry.py` (`dispatch`, `_normalize_handler_result`, `_bound_error_text`,
`_bound_json_error_result`, `_kwargs_accepted_by`), `agent/inline_tool_executors.py` (table
shape, lines 1–120 and 280–337), `tools/arg_coercion.py` (whole), `tools/schema_sanitizer.py`
(`resolve_local_schema_ref`, `unrename_tool_args`). Test names of
`tests/agent/test_tool_arg_coercion.py`, `tests/tools/test_model_tools.py` and
`tests/tools/test_registry.py` for edge cases. Research `02-tools.md` §3.1, §3.1a.
Closed before writing.
Evidence: none.

What it does (own words): the one door from a parsed tool call to a handler. It looks the tool
up, repairs argument types the model got wrong (only when the schema makes the fix certain), gives
the handler only the context keywords its signature asks for, runs it (awaited if async, on a
worker thread with a deadline if sync), passes the call through the hook seam, and turns whatever
happens into a bounded `ToolResult`: a string, or the one multimodal envelope. The model never sees
a traceback or an unbounded error.

Inputs → outputs: `await dispatch(call: ToolCall, ctx: ToolContext, *, registry, hooks=NO_HOOKS,
inline=INLINE_TOOL_NAMES) -> ToolResult`. `ToolCall{name, args, call_id}` (args already parsed by
the tool round). `ToolContext{scope, task_id, session_id, turn_id, extras}`; injected kwargs are
`task_id, session_id, turn_id, tool_call_id, scope` plus `extras`. `ToolResult{content: str |
envelope, is_error, error_type}`. `coerce_args(args, parameters) -> dict`. `ToolHooks` protocol
(`before`, `after`, `transform`) with `NO_HOOKS` default. `InlineTable` name → executor (filled
by core in wave 6).

Files: `src/indus/tools/dispatch.py`, `src/indus/tools/coerce.py`, `src/indus/tools/hooks.py`,
`tests/unit/tools/test_dispatch.py`, `tests/unit/tools/test_coerce.py`.

Edge cases / failure modes:
- unknown name → `{"error": "Unknown tool: <name>"}`.
- name handled by the agent loop (inline table) → refused with an error naming that.
- handler returns dict/list/None/int → contract error (`error_type: tool_result_contract`); the
  envelope `{multimodal: true, content: [...], text_summary: str}` is the only non-string accepted.
- handler raises → `Tool failed: <Type>: <msg>` with framing tags, CDATA and fences removed,
  capped at 2,048 chars plus a marker; the log line keeps up to 8,192.
- handler returns a JSON string whose `error` field is huge → that field trimmed the same way.
- coercion: `"42"`→42 (integer), `"4.5"` stays for integer, `"4.5"`→4.5 for number,
  `"inf"`/`"nan"` stay strings, `"true"/"false"` (any case) → bool, `"1"` for boolean stays,
  JSON text → list/dict when the schema wants one (also nested by schema), bare scalar → one-item
  list for array, `"null"` → None only when the schema allows null, unknown keys untouched,
  local `$ref` (JSON pointer, `~0`/`~1`, percent escapes, through arrays) resolved, remote /
  cyclic / dangling refs left alone; renamed (sanitised) property keys mapped back.
- sync handler past its deadline → error result; the worker thread is abandoned, not killed.
- empty hook seam raising → ignored (fail-open only for the empty seam); a non-empty seam whose
  `before` blocks or raises → the tool does not run (fail closed). No test asserts fail-open for a
  non-empty seam.

Our design differences / decisions:
- Our envelope key is `multimodal` (row text), not the reference's underscored key.
- Error wording ours: `Tool failed: …`; the reference adds a second outer wrapper in the loop; we
  have one.
- `dispatch` is async; sync handlers run through a private bounded-pool helper with deadline
  (`asyncio.wait_for` over `run_in_executor`, context copied so the scope travels). When the lead
  lands the shared D-04.1 helper, `_run_sync` delegates to it.
- Coercion is a pure function of (args, parameters schema); dispatch passes the entry's schema.
- Hooks: one `ToolHooks` object (`empty` flag) instead of plugin-module lookups; `after` and
  `transform` failures are logged and the original result kept.
- Middleware chains, tool-search bridge, connector routing, edit approval and legacy name aliases
  are not part of P04 (P13 / later phases).

Cross-slice names needed: none.

Tests we will write:
- coercion table (scalars, booleans, JSON containers, nested, wrap, null, refs, renamed keys,
  ambiguous left alone, seeded-random round trip: values already of the right type never change).
- dispatch: narrow signature gets only declared kwargs; `**kwargs` gets all; raising handler →
  bounded sanitised error; 10 KB error → 2,048 + marker; JSON error field trimmed; dict result →
  contract error; envelope accepted; async and sync handlers; sync deadline; unknown tool; inline
  refused; hooks: empty seam raising ignored, before-block, transform replaces, after observes.

## Decisions for the lead
- Blocking helper lives privately in `dispatch.py` until the shared D-04.1 helper exists.
- Non-empty `before` that raises blocks the tool already in P04 (matches R07.18; P13-17 owns the
  plugin wiring in `hooks.py`).

Compare checklist (filled after the rewrite; reopened `model_tools.py`, `tools/registry.py`,
`tools/arg_coercion.py`, `tools/schema_sanitizer.py`, `agent/inline_tool_executors.py`):
- [x] result contract: text or the envelope, anything else a contract error (test: `test_dispatch.py::test_non_text_result_is_a_contract_error`, `::test_multimodal_envelope_accepted`, `::test_near_envelopes_rejected`)
- [x] raise → sanitised `Tool failed: <Type>: <msg>`, 2,048 + marker, log keeps up to 8,192 (test: `::test_raising_handler_gives_bounded_sanitised_error`, `::test_ten_kilobyte_error_trimmed_to_limit_plus_marker`, `::test_log_keeps_more_than_the_model_sees`)
- [x] oversized `error` field inside handler JSON trimmed, other large results untouched (test: `::test_oversized_error_field_in_handler_json_trimmed`, `::test_large_results_without_long_error_text_unchanged`)
- [x] kwargs narrowed to the signature, `**kwargs` gets all (R02.28) (test: `::test_narrow_signature_gets_only_declared_kwargs`, `::test_var_kwargs_handler_gets_everything`, `::test_narrow_kwargs_ignores_positional_only_and_first_param`)
- [x] unknown tool, agent-loop tools refused through a table (test: `::test_unknown_tool`, `::test_inline_tool_name_refused`, `::test_inline_table_is_a_table`)
- [x] async awaited, sync on a worker with scope and deadline (test: `::test_async_handler_awaited`, `::test_sync_handler_runs_off_the_loop_with_scope`, `::test_sync_handler_past_deadline_is_an_error`)
- [x] coercion branches incl. refs, renamed keys, nested JSON, null, wrap (test: `test_coerce.py`)
- [x] gap found: a union that also admits text must keep JSON-looking text — fixed in `coerce._nested`, pinned by `test_coerce.py::test_union_that_admits_text_keeps_json_looking_text`
- Deliberate differences: stricter number parsing (`"1_000"`, `"inf"`, `" +5"` stay text; big
  integers parsed exactly); a type list that includes `string` keeps the text (the reference tries
  the next type); an array-typed string that looks like broken JSON stays text (the reference wraps
  it); envelope key `multimodal` and exact key set; one error wrapper; hooks via one seam object.
- Not taken: middleware chains, tool-search bridge, connector routing, edit approval, legacy tool
  name aliases, read-loop counter reset, persistent per-thread event loops (async core instead).

## Status

Done (2026-10-18). Files: `src/indus/tools/{dispatch,calls,coerce,hooks}.py`,
`tests/unit/tools/{test_dispatch,test_coerce}.py`. Property checks are seeded-random table tests
(hypothesis is not installed).
