# Task: Protocol models (P04-01) Phase: 04 Owner: wave-5 builder A

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`):
`website/docs/developer-guide/architecture.md` (skimmed: system overview, data flow for the CLI and
gateway surfaces, design principles; nothing about wire names is taken). Research: `05-tui.md`
§3.2–3.5, §3.16–3.17 and trap R05.15 / R05.3 / R05.21 (which method families exist, names only);
`09` §3 headings (no method names used). Envelope rules checked against the JSON-RPC 2.0
specification text (jsonrpc.org/specification): `jsonrpc` is exactly `"2.0"`; a request has `id`
(string or number; null discouraged), `method`, optional structured `params`; a notification is a
request without `id`; a response has `result` xor `error`, with `id` equal to the request's (null
when it could not be read); `error` = `{code: integer, message: string, data?: any}`; reserved
codes -32700 parse, -32600 invalid request, -32601 method not found, -32602 invalid params,
-32603 internal; -32000..-32099 are reserved for implementation-defined server errors.
Evidence: none.

What it does (own words): the one typed contract every surface talks through. Each method,
event and server request is a pydantic v2 model that refuses unknown keys, so a client typo is a
precise "invalid params" error instead of a silently ignored option. Error codes live in one
table where each number has exactly one meaning, a symbolic name and an HTTP-like status; the
reference reuses numbers with different meanings per handler (R05.15) and clients cannot branch on
them. Without this package the frontend track has nothing real to build against.

Inputs → outputs: JSON frames ↔ envelope models (`Request`, `Notification`, `SuccessResponse`,
`ErrorResponse`, `ErrorObject`); method params/results; event models (wire: a notification whose
`method` is the event type); server-request models (wire: a `server_request` notification) and
their answers (sent back with the `server_request.answer` method); `ErrorCode` rows.

Files: `src/indus/protocol/{base,envelope,errors,methods,events,server_requests}.py`,
`tests/unit/protocol/{__init__,_samples,test_models,test_error_table}.py`.

Method catalogue (our names, D-16.1):
- `session.create {title?, cwd?, model?, toolsets?, idempotency_key?}` → `{session_id, created}`
  (a repeated idempotency key returns the first session with `created=false`, R05.21).
- `session.resume {session_id}` → `{session_id, message_count, last_seq, pending_requests[]}`
  (open server requests are re-delivered after reconnect, R05.3).
- `prompt.submit {session_id, text}` → `{turn_id, generation}` (the turn ticket).
- `session.interrupt {session_id, generation?, hard}` → `{interrupted}` (stale generation = no-op).
- `session.steer {session_id, text}` → `{queued, ticket?}`.
- `session.redirect {session_id, text}` → `{outcome: redirected|steered|rejected}`.
- `server_request.answer {session_id, request_id, answer}` → `{accepted}`.
- `client.capabilities {features[]}` → `{features[]}`; the known set is empty in P04, unknown
  names are dropped (a negotiation, not a typo) — the only place unknown strings pass.

Events (all carry `type`, `session_id`, `seq`): `turn.started {turn_id, generation}`,
`message.delta {turn_id, text}`, `reasoning.delta {turn_id, text}`,
`tool.started {turn_id, call_id, name, arguments}`, `tool.progress {turn_id, call_id, message,
fraction?}`, `tool.completed {turn_id, call_id, name, ok, duration_ms, result}`,
`status {kind, text}`, `notice {level, text, key?}`, `server_request.cancelled {request_id,
reason}`, `turn.done {turn_id, reason: ExitReason, failure?: {reason, retryable, resets_at?}}`.

Server requests (`kind` discriminator, base `{request_id, session_id, turn_id?, deadline_s?}`):
`approval {command, description, choices[]}` → `{choice: once|session|always|deny}`;
`clarify {questions[{id, text, choices?, multi_select}]}` → `{answers: {question_id: [values]}}`
(empty list = skipped); `sudo {reason}` → `{password?}` (null = cancelled); `secret {name, prompt}`
→ `{value?}` (null = skipped).

Error table (groups and HTTP-ish status): invalid request 400 (`PARSE_ERROR` -32700,
`INVALID_REQUEST` -32600, `INVALID_PARAMS` -32602, `TEXT_EMPTY` 1001); not found 404
(`METHOD_NOT_FOUND` -32601, `SESSION_NOT_FOUND` 2001, `SERVER_REQUEST_NOT_FOUND` 2002); conflict
409 (`SESSION_BUSY` 3001, `IDEMPOTENCY_CONFLICT` 3002, `ANSWER_KIND_MISMATCH` 3003); refused 403
(`SESSION_REFUSED` 4001, `SERVER_REQUEST_UNSUPPORTED` 4002); provider 502
(`PROVIDER_UNAVAILABLE` 5001); internal 500 (`INTERNAL_ERROR` -32603, `STORE_FAILED` 6001).
Application codes use our own positive bands (1xxx … 6xxx) so the group is readable from the
number; the -32000 band is not used. The five spec codes are declared by every method (any call
can be malformed or crash), so every code is referenced by at least one method.

Edge cases / failure modes:
- unknown key at any depth → `ValidationError`; `validate_params` maps it to `INVALID_PARAMS`
  with the dotted key path in `data.path`; unknown method → `METHOD_NOT_FOUND`.
- `id` must be a string or an integer (bool and float refused; null only on an error response).
- a success response with `result: null` keeps the key on the wire (spec: `result` is required).
- empty or whitespace-only text on submit/steer/redirect → `TEXT_EMPTY` (model validator), not a
  generic invalid-params error, so clients can show "type something".
- frames that are neither request, notification nor response → `INVALID_REQUEST`.
- foundation `IndusError` codes map onto the table at the boundary (`error_for_indus_code`):
  session-not-found → 2001, tombstoned/compression-closed → 4001, guarded/lease-lost → 3001,
  other `store.*` → 6001, `provider.*` → 5001, anything else → -32603.

Our design differences / decisions:
- events are notifications named by their type (no single catch-all event method).
- server requests are notifications answered through `server_request.answer`, not response
  frames: one answer path for live clients, replayed cards and the in-process host.
- `ExitReason` lives here (protocol is below core in the layer contract) and P04-15's
  `core/exit_reasons.py` re-exports it.
- secrets (sudo password, secret value) are plain `str` on the wire so models round-trip; the
  transport and logs must never print them (P17); no `SecretStr` (it would dump as asterisks).
- every model and enum has a docstring (schema `description`); fields use attribute docstrings.

Tests we will write:
- round trip (model → JSON → model) for every model in the catalogue, events, server requests,
  answers and envelopes, from one sample per model.
- unknown field rejected for every model (top level) and nested (clarify question).
- envelope: id types, response requires exactly one of result/error, `result: null` kept, frame
  classification, docstrings on every model.
- error table: unique codes and names, name shape, group ↔ status, every code referenced by a
  method, spec codes have the spec numbers, IndusError mapping table.
- `turn.done.reason` accepts every `ExitReason` value and nothing else.

## Decisions for the lead
- Server requests travel as `server_request` notifications and are answered with the
  `server_request.answer` method (not JSON-RPC response frames). Change: make them request frames
  and drop the method.
- `ExitReason` is defined in `indus.protocol.events`; P04-15 should re-export it from
  `core/exit_reasons.py` rather than define a second enum.
- Error numbering: spec codes plus our own positive bands per group; -32000..-32099 unused.
- `client.capabilities` silently drops unknown feature names (forward compatibility).
- `src/indus/protocol/__init__.py` stays the one-line docstring: `tests/unit/test_layers.py`
  requires every layer package's `__init__` to be docstring-only (the same ruling as
  `state/__init__.py`). Public names are imported from the submodules; `PROTOCOL_VERSION` lives in
  `indus.protocol.methods`. Change: relax the layers test, then re-export from `__init__`.
- pyproject import-linter row (P04-01 "Files to update"): the existing layer contract already
  puts `indus.protocol` below `indus.core`/`indus.service` and `indus.tools` below
  `indus.core`, so no change was needed.

Compare checklist (filled after the rewrite; reopened `website/docs/developer-guide/architecture.md`):
- [x] Surfaces named there (CLI, gateway, ACP, API server, library) all reach the agent through
  one contract — ours is `indus.protocol` + `SessionService` (no test; structural).
- [x] Envelope matches the JSON-RPC 2.0 text, not the reference (test:
  `tests/unit/protocol/test_models.py::test_success_response_keeps_null_result`,
  `::test_response_needs_exactly_one_of_result_or_error`, `::test_request_id_types`).
- [x] R05.15 one table, one meaning per code (test:
  `tests/unit/protocol/test_error_table.py::test_codes_and_names_are_unique`).
- [x] R05.3 unanswerable kinds have their own code (`SERVER_REQUEST_UNSUPPORTED`) (test:
  `test_error_table.py::test_every_code_is_declared_by_a_method`).
- Deliberate differences: the reference overloads numeric codes per handler and uses a catch-all
  event method; we give each code one meaning and name each notification by its event type.
- Not taken: the reference's ~245-method surface; only P04's eight methods exist now.

## Status

Done (2026-10-18). Shared change: none outside the protocol package. Handed on: P04-15 re-exports
`ExitReason`; P04-21 uses `error_for_indus_code` at the service boundary.
