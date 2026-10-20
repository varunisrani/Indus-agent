# Task: Shared RPC dispatcher (P17-12) Phase: 17 Owner: wave-25 builder K4

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tui_gateway/rpc_dispatch.py`
(all: admission, unknown-method answer, param check, pool dispatch of long handlers, crash
answer), `tui_gateway/contracts/registry.py` (all: declaration tables, import-time refusal of an
undeclared handler, strict/lenient checks of params, results and payloads, completeness check).
Research 05 §3.3 (wire contract discipline). Our P12 `tui_bridge/dispatch.py` and
`tui_bridge/stdio_server.py` (the code being moved). Closed before writing.
Evidence: none.

What it does (own words): every JSON-RPC transport the backend speaks (stdio for the TUI child and
`--local`, the daemon's WebSocket from P17-13, later ACP and the dashboard) answers requests
through one dispatcher in `service/rpc/`, so all of them refuse the same things the same way:

- the handler table only accepts method names declared in `indus.protocol`; a handler module
  that declares an undeclared name fails when it is imported (`MethodSet.method`), and a table
  refuses it at registration (`MethodTable.add`);
- a request for an undeclared or unserved method is `-32601`;
- params are validated against the method's params model before the handler runs: an unknown
  key is `UNKNOWN_PARAM_KEY` with the dotted key path, blank message text `TEXT_EMPTY`, anything
  else `INVALID_PARAMS` with the path;
- the handler runs under the context's scope (R05.14) with a per-call deadline (120 s default,
  longer for submit / background submit / compress); past it the call is cancelled and answered
  `INTERNAL_ERROR` "deadline";
- a protocol error is answered as such, a product error is mapped through the protocol table, and
  any other crash is logged (method name only, never params) and answered `INTERNAL_ERROR`; the
  loop goes on and the next call is served;
- the result, and any event payload a transport emits, is checked against its model: a mismatch
  raises `ContractViolation` in strict mode (`INDUS_PROTOCOL_STRICT`, set by tests) and is logged
  otherwise.

`decode_request(line)` turns one inbound text frame into a request to dispatch, an error frame to
send back (`-32700` for bad JSON with id null, `-32600` for a non-frame with the id when usable),
or nothing (blank line, responses and notifications from the client).

`tui_bridge/dispatch.py` stays as a thin layer: the bridge's handler module list, `build_table`,
and `Dispatcher` bound to `Bridge`; `stdio_server.py` imports the dispatcher pieces from
`service/rpc`. The gateway may not import `tui_bridge` (same layer), which is why the shared part
moves down to `service`.

Inputs → outputs: `Dispatcher(context, table, deadline=, deadlines=)`; `await handle(Request) ->
dict` (response frame); `MethodTable[C]`, `MethodSet[C]` (`method(name)` decorator,
`register(table)`), `declared(name)`, `error_frame`, `decode_request`, `check_payload`,
`check_event`, `strict_from(scope)`, `ContractViolation`, `RpcContext` (protocol: `scope`,
`strict`).

Files: `src/indus/service/rpc/__init__.py`, `src/indus/service/rpc/dispatch.py`,
`tests/unit/service/test_rpc_dispatch.py`; shared (P12 files): `src/indus/tui_bridge/dispatch.py`
(thin re-export), `src/indus/tui_bridge/stdio_server.py` (imports and `decode_request`),
`src/indus/tui_bridge/bridge.py` (`ContractViolation` re-exported from `service/rpc`).

Edge cases / failure modes:
- a handler that never returns → cancelled at its deadline, answered, loop free.
- a handler that raises `CancelledError` because the transport is closing → propagates (the
  transport is shutting down; nothing to answer).
- a handler that returns something other than a protocol model → strict raises, lenient answers
  `INTERNAL_ERROR` (no model to send).
- params may hold a password or a secret answer → never logged, not even on a crash.
- a duplicate registration → refused.

Our design differences / decisions:
- One stable code table (D-16.27) instead of per-handler numbers: unknown key is
  `UNKNOWN_PARAM_KEY` (1002), not the reference's `4000`.
- Full params validation before the handler (the reference only refuses unknown keys there and
  leaves types to handlers); then no "params accepted" after-check is needed.
- No thread pool for long handlers: handlers are async on the loop (D-04.1 blocking helper for
  blocking work); the deadline replaces the pool's isolation.
- No "backend retiring" admission gate here (the daemon's drain refuses work at admission,
  P15-24/P17-02).
- Completeness (every declared method has a handler) stays a per-transport test, as P12 had it.

Tests we will write:
- unknown key → `UNKNOWN_PARAM_KEY` with the path; nested path; blank text; wrong type.
- undeclared method fails at import (a test module written to a temp dir and imported).
- table refuses undeclared and duplicate names; `-32601` for undeclared and unserved.
- handler crash answered, next call served; params absent from logs.
- deadline: a hanging handler answered at its (short) deadline, next call served; per-method
  override.
- strict result check raises, lenient logs; event payload check both ways.
- `decode_request` cases; the P12 re-export keeps the old names working.

## Decisions for the lead
- Deadline answer uses `INTERNAL_ERROR` with message "handler error: <method> passed its N s
  deadline" and `retryable: true` in the error data; P17-25 may add a dedicated code.
- Deadlines: 120 s default; `prompt.submit` and `prompt.background` 1,800 s (a submit may wait
  for the turn lease), `session.compress` 600 s (under the client's 660 s).

Compare checklist (filled after the rewrite; reopened `tui_gateway/rpc_dispatch.py`,
`tui_gateway/contracts/registry.py`):
- [x] undeclared handler fails at import (test: `test_rpc_dispatch.py::test_undeclared_method_fails_at_import`)
- [x] duplicate declaration refused (test: `test_rpc_dispatch.py::test_table_refuses_undeclared_and_duplicate_names`)
- [x] unknown method answered `-32601` (test: `test_rpc_dispatch.py::test_unknown_and_unserved_methods`)
- [x] unknown key refused with its path (test: `test_rpc_dispatch.py::test_unknown_key_names_its_path`)
- [x] result checked: strict raises, production logs (tests: `test_rpc_dispatch.py::test_bad_result_raises_in_strict_mode`, `::test_bad_result_is_logged_when_lenient`)
- [x] event payloads checked the same way (test: `test_rpc_dispatch.py::test_event_payloads_are_checked`)
- [x] a crashing handler is answered and the loop goes on (test: `test_rpc_dispatch.py::test_handler_crash_is_answered_and_the_next_call_served`)
- [x] long work bounded (test: `test_rpc_dispatch.py::test_a_hanging_handler_is_answered_at_its_deadline`)
- Deliberate differences: stable codes, full params validation, no pool, no retirement gate,
  deadline (above).
- Not taken: separate canonical method table (one catalogue in `indus.protocol`; P17-25 adds the
  attach methods to it); OpenRPC rendering (P04 codegen owns generated output).

## Status

Done (2026-10-20). Dispatcher moved to `service/rpc`, P12 files reduced to a re-export and
imports (shared). P17-13 builds its WS loop on `decode_request` + `Dispatcher` + `check_event`.
