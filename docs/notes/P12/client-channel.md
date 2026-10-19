# Task: `@indus/client` channel core (P12-02) Phase: 12 Owner: wave-7 builder K

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/shared/src/json-rpc-channel.ts`
(request, timeout, abort, server-request delivery and its error answers),
`ui-tui/src/gatewayClient.ts` (transport swap, close gating, the channel options it passes).
Research `05-tui.md` §3.2 "shared channel behaviour", §3.30; LEAD-NOTES wave 5 (wire shapes).
Evidence: none.

What it does (own words): the one JSON-RPC 2.0 peer every surface (TUI, web, desktop) talks
through. It does not know how frames travel: it is handed a `Transport` (send a frame object, get
frame objects and a close signal back) and can be handed a new one at any time. It numbers our
calls, keeps a pending table with a timeout per call, turns error frames into one typed error,
hands notifications (events) to its owner, and routes questions from the server to a handler
table so the agent never waits on a client that cannot answer.

Facts kept from the read (rules, not code):
- client ids are integers counting from 1; server-minted request ids are strings (`srq-` + 12
  hex), so a response can never be mistaken for the answer to a server question.
- default call timeout 120,000 ms; the env override has a floor of 30,000 ms; a call may pass its
  own timeout and an abort signal; abort and timeout both remove the pending entry at once.
- a server request with no handler is answered "method not found" (-32601) immediately; a handler
  that throws is answered -32603; only the first answer for an id goes out.
- swapping the transport fails every call of the old one (they would otherwise hang), and a close
  event that comes from a transport that is no longer current is ignored.
- a response for an id that is not pending (late after a timeout, a duplicate) is dropped.

Inputs → outputs: `channel.attach(transport)`, `channel.call(method, params, {timeoutMs, signal})`
→ result or `GatewayRpcError` / `RpcTimeout` / `RequestAborted` / `TransportReplaced` /
`TransportClosed` / `NotConnected`; `channel.notify(method, params)`; inbound notifications →
`onNotification`; inbound server requests → handler table → response frame (or, for our P04 wire,
a `server_request.answer` call).

Files: `packages/client/src/channel.ts`, `errors.ts`, `clock.ts`, `channel.test.ts`,
`packages/client/package.json`, `packages/client/tsconfig.json`.

Edge cases / failure modes:
- call before any transport → `NotConnected`; signal already aborted → rejected without sending.
- `send` throws (pipe gone) → that call fails at once with the thrown error, nothing pending.
- server request before the handler table is installed → held (P12-03), answered on install.
- handler answers twice, or answers after throwing → second answer dropped.
- error frame with a code outside the generated table → still a `GatewayRpcError`, `known=false`,
  and reported through `onUnknownErrorCode` so a test or log can flag it (R05.15, D-16.27).
- old transport delivering frames after a swap → ignored (identity gate on frames too).

Our design differences / decisions:
- Two server-request paths. (1) A JSON-RPC request frame with an id: answered with a response
  frame (`result`, or `error` -32601 / -32603). (2) Our P04 wire: a `server_request` notification
  whose params are a typed `ServerRequest`; the handler table is keyed by `kind` and the answer goes
  back with the `server_request.answer` method. Our wire has no error form for path 2, so a kind
  with no handler (or a crashing handler) is answered with that kind's *decline* answer (approval
  `deny`, clarify all skipped, sudo/secret empty) — the agent still never waits out its deadline.
- Error type named `GatewayRpcError { code, message, data, name, known }`; `name` comes from
  `data.name` or the generated table.
- No Node-only API in `channel.ts` (env handling is a pure function taking the raw string); timers
  go through an injected `Clock`.

Tests we will write (vitest, fake timers, in-memory transport):
- env timeout below the floor resolves to 30,000 and the call fails exactly then; per-call
  timeout and abort; late response after timeout dropped.
- client ids are integers from 1 and never equal a server `srq-` id; responses route by id type.
- unknown server request (path 1) → -32601 on the next microtask; throwing handler → -32603;
  double answer sent once; path 2 decline answers per kind.
- swap rejects every pending call with `TransportReplaced`; old close and old frames ignored;
  close of current transport rejects with `TransportClosed`.
- error frame → `GatewayRpcError` with name from the table; unknown code flagged.

## Decisions for the lead
- Our P04 wire carries server questions as `server_request` notifications answered with
  `server_request.answer`, which has no error form. A kind with no handler, or a crashing handler,
  is answered with that kind's decline answer (approval `deny`, clarify every question skipped,
  sudo/secret empty) instead of `-32601`/`-32603`. Change (slice G / protocol): give
  `server_request.answer` an error form (e.g. `{refused: SERVER_REQUEST_UNSUPPORTED}`) or move server
  questions to id-bearing request frames; the channel already answers those with -32601/-32603.
- A second delivery of an open or recently answered request id (live + resume replay) is dropped at
  the channel, so a card never opens twice; the memory holds the last 512 ids.
- `answerableKinds()` lists the kinds with handlers; nothing sends it yet because
  `client.capabilities` has no feature names for server-request kinds (protocol gap, P12-30).
- Answers to id-bearing requests go only to the connection that asked; after a swap they are
  dropped (that connection's peer is gone). `server_request.answer` goes over the current one.

Compare checklist (filled after the rewrite; reopened the two files above):
- [x] ids: integers from 1, server ids are strings and never settle a call (test:
  `packages/client/src/channel.test.ts::never confuses a server string id with a client integer id`)
- [x] default 120 s, env floor 30 s, per-call timeout, abort before/after send (tests: `times out at
  the env floor…`, `uses the default timeout…`, `aborts through the caller's signal…`)
- [x] unknown server request → -32601 on the next microtask; throwing/rejecting handler → -32603
  (tests: `answers -32601 at once…`, `answers -32603 when the handler throws or rejects`)
- [x] first answer wins / repeat delivery dropped (tests: `sends the handler's result once…`,
  `first answer wins when a handler resolves after…`)
- [x] swap rejects all pending with `TransportReplaced`; stale close and stale frames ignored (test:
  `rejects every call of a replaced transport…`)
- [x] error frame → `GatewayRpcError` named from the generated table; unknown code flagged (tests:
  `turns error frames into GatewayRpcError…`, `flags an error code the generated table does not know`)
- [x] gap found: the reference re-delivers open requests carried in a resume result inside the
  channel — ours does it in `IndusClient.resume` via `channel.redeliver`, marked `replayed`
  (test: `client.test.ts::re-delivers open requests from session.resume as replayed, once`)
- [x] gap found: withdrawn requests — ours aborts the request's `signal` on
  `server_request.cancelled` so a card can close (test: `aborts the request's signal when the
  server withdraws it`)
- Deliberate differences: the reference tries a list of handlers in turn (a handler may decline);
  ours is one table keyed by method/kind. The reference advertises capabilities itself on ready;
  ours exposes `answerableKinds()` and leaves the call to the app until the feature names exist.
  The reference's error-class default message names its product; ours is neutral.
- Not taken: the "not shown in any window" decline code (desktop multi-window, P20).

## Status

Done (2026-10-19). `packages/client/src/{channel,errors,clock,limits}.ts` + `channel.test.ts`
(23 tests). Shared: none. Handed on: P12-21/22 use `IncomingRequest.signal` and `replayed`;
P12-30 (slice G) decides the refusal form and the capability feature names.
