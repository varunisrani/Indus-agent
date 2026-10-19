# Task: stdio transport and fake server (P12-05) Phase: 12 Owner: wave-7 builder K

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `ui-tui/README.md` (stdio framing
paragraph: stdout carries frames, stderr is free text; noise is never painted),
`tui_gateway/transport.py` (framing only: one JSON object per line, unserialisable payloads become
an error frame, peer-gone write errors). Research `05-tui.md` §3.2 (mode A), §3.30, trap R05.1,
R05.20. Evidence: none.

What it does (own words): the TUI spawns `indus tui-bridge --stdio` and talks NDJSON over its
pipes. Our transport splits stdout into lines whatever the chunking (a frame cut across reads,
CRLF endings, a 1 MiB line), parses each line as one frame and hands it to the channel. A line that
is not a JSON object becomes a `transport.protocol_error { preview }` event (preview at most 120
characters) and never reaches the screen. stderr lines go into the log ring and out as
`transport.stderr { line }`. If no `gateway.ready` arrives within 15,000 ms (env
`INDUS_TUI_STARTUP_TIMEOUT_MS`, floor 5,000) it emits `transport.start_timeout { python, cwd,
stderr_tail }` with the last 20 log lines. When the child exits it emits `transport.exit { code,
signal, stderr_last }`. `close()` ends stdin, gives the child 2,000 ms, then kills its whole
process group. A scriptable in-process fake server lets every renderer test run without Python.

Inputs → outputs: `spawnStdioTransport({command, args, cwd, env, ...})` → a `Transport` plus
`onLocalEvent`; `LineSplitter.push(chunk)` → lines; `parseFrameLine(line)` → frame or preview;
`FakeServer` → `transport`, `handle`, `emit`, `ready`, `ask`, `request`, `die`, `stall`.

Files: `packages/client/src/ndjson.ts`, `stdioTransport.ts`, `localEvents.ts`, `client.ts`,
`index.ts`, `testing/fakeServer.ts`, `testing/fakeChild.mjs`, `ndjson.test.ts`,
`stdioTransport.test.ts`, `client.test.ts`.

Edge cases / failure modes:
- a chunk boundary inside a multi-byte character → decoded with a streaming UTF-8 decoder.
- blank lines skipped; a final line without newline delivered at end of stream.
- a line over the hard cap (64 MiB) is dropped as a protocol error instead of growing memory.
- JSON that is valid but not an object (`42`, `[]`) → protocol error.
- spawn failure (ENOENT) → `transport.exit { code: null }` plus a log line, close listeners run.
- write after the child died → the send throws, the channel fails that call.
- close twice → one shutdown; close after exit → resolves at once.
- the child is its own process group (POSIX) so the kill reaches grandchildren; Windows uses
  `taskkill /T /F`.

Our design differences / decisions:
- synthetic events are named `transport.*` (ours); they carry `type` like server events.
- `client.ts` composes channel, event buffer, log ring, heartbeat and reconnect over a transport
  factory; crash-recovery budgets stay with the app (P12-27).
- the env floors apply to the env values only; explicit options are taken as given (tests).

Tests we will write: split chunks, CRLF, 1 MiB line, garbage → protocol_error, stderr routed,
start-timeout payload, exit event, child tree gone after close (no orphan pid); fake server
respond/emit/ask/die/stall exercised through the client.

## Protocol gaps (slice G owns protocol changes; stubbed here, not added on the Python side)
- `gateway.ready` (first frame: `{protocol_version?, heartbeat?}`) is not declared; stubbed in
  `localEvents.ts` (`READY_METHOD`, `ReadyEvent`). The start timeout and heartbeat depend on it.
- `gateway.ping` (keep-alive; any answer counts) is not declared; stubbed as `PING_METHOD`.
- `client.capabilities` has no feature names for answerable server-request kinds, so a client cannot
  declare them over the wire (the broker's default set is empty, R05.3); `answerableKinds()` is ready.
- `server_request.answer` has no refusal/error form (see the channel note).

## Decisions for the lead
- `close()` kills the process group only when the child is still running after the grace; a child
  that exits on stdin EOF is not chased further (its own children are its business, P05 backends).
- Exit is reported on `close` (stdout drained) or 1 s after `exit` if a grandchild keeps the pipe
  open.
- `@indus/client` has three entries: `.` (browser-safe), `./stdio` (Node only), `./testing` (fake
  server). `fakeChild.mjs` is plain JS so Node runs it without a TS loader.
- Floors apply to env values only; explicit options (tests) are taken as given.
- `start_timeout.stderr_tail` is a list of lines (the last 20), not one joined string.

Compare checklist (filled after the rewrite; reopened the two files above):
- [x] split chunks incl. a split multi-byte character (tests: `ndjson.test.ts::joins a frame cut
  across reads…`, `stdioTransport.test.ts::reassembles frames split across reads…`)
- [x] CRLF (tests: `strips CRLF…`, `accepts CRLF line ends`)
- [x] 1 MiB line (tests: `passes a 1 MiB line through whole`, `takes a 1 MiB line as one frame`)
- [x] garbage → `transport.protocol_error`, preview ≤ 120, never on the frame path (tests: `turns
  non-frame lines into protocol_error events…`, `keeps previews to 120 characters on one line`)
- [x] stderr → log ring + `transport.stderr` (test: `routes stderr to the log ring and as events…`)
- [x] start-timeout payload with the last 20 log lines; no timeout after ready (tests: `emits
  start_timeout with the command, cwd and the last 20 log lines`, `does not time out once the
  ready frame came`)
- [x] exit event `{code, signal, stderr_last}`; spawn failure reported as exit (tests: `routes
  stderr…`, `reports a command that cannot start as an exit`)
- [x] close: stdin end, 2 s grace (300 ms in the test), whole tree killed, no orphan pid (test:
  `close() ends stdin; a child that ignores it is killed with its whole tree after the grace`)
- [x] fake server: respond, emit, ask, request, cancel, die, stall exercised (tests in
  `client.test.ts`, `eventBuffer.test.ts`, `heartbeat.test.ts`, `backoff.test.ts`)
- Deliberate differences: synthetic events are `transport.*`; the reference's protocol-error log
  preview is longer than the event's, ours uses one 120-char preview.
- Not taken: the WebSocket/attach transports and bootstrap helper (P17); the sidecar event mirror.

## Status

Done (2026-10-19). `packages/client/src/{ndjson,stdioTransport,localEvents,client,index}.ts`,
`testing/{fakeServer.ts,fakeChild.mjs}`, `package.json`, `tsconfig.json`, tests `ndjson.test.ts`
(7), `stdioTransport.test.ts` (11), `client.test.ts` (5). Shared: `pnpm-lock.yaml` regenerated
locally (lead re-locks).
