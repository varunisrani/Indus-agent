# Task: tui_bridge stdio server and dispatcher (P12-28) Phase: 12 Owner: wave-7 builder G

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tui_gateway/entry.py` (whole file:
startup order, `_serve`, `main`, the read loop), `tui_gateway/AGENTS.md` (transport, wire
declaration, profile scope sections), `tui_gateway/server.py` (`write_json` / `_emit` near line 700
only), `tui_gateway/_stdin_recovery.py` (the spurious-EOF check that `entry.py` calls; skimmed).
Research `05-tui.md` §3.2 mode A, §3.3, traps R05.1, R05.14, R05.15, R05.20. Ours:
`docs/notes/P04/protocol-models.md`, `protocol-codegen.md`, `session-service.md`, LEAD-NOTES
waves 5/6 (wire shapes, `requests_port`). Closed before writing.
Evidence: none.

What it does (own words): `indus tui-bridge --stdio` is the Python child the terminal UI spawns.
It speaks newline-delimited JSON-RPC 2.0 over its own stdin/stdout. stdout is a protocol channel and
nothing else: the first thing the process does is take private copies of the real stdin and stdout
descriptors, point descriptor 1 at stderr (so a stray `print`, a library banner or a child process
that inherits stdout lands on stderr) and descriptor 0 at the null device (so a child started
without an explicit stdin cannot swallow request bytes, R05.20). Every frame is written through one
locked writer. The read loop takes one JSON object per line, ignores blank lines, answers a line
that is not JSON with `-32700` and id `null`, and goes on. Each request runs as its own asyncio task
so a slow handler never blocks reading; a crashing handler is logged and answered with the generic
internal error, never ending the loop. A genuine end of input closes every session cleanly and ends
the process. Without this the TUI has no backend, and a single noisy library would corrupt the
stream and kill the UI.

Inputs → outputs: bytes on the RPC stdin → `Request` frames → handler results as `SuccessResponse`
/ `ErrorResponse`; first frame `gateway.ready {theme, heartbeat: false, protocol_version}`; events
and server requests as notifications (method = event type / `server_request`). CLI: hidden
`tui-bridge` subcommand (lazy import), `--stdio` required; exit 0 on EOF.

Files: `src/indus/tui_bridge/stdio_server.py` (descriptor claim, line reader with spurious-EOF
retry, frame writer, serve loop, `run`), `src/indus/tui_bridge/dispatch.py` (method table, handler
module list, validation, error mapping, result checks), `src/indus/tui_bridge/bridge.py` (the
per-connection state the handlers share), `src/indus/tui_bridge/host.py` (the real service
builder), `src/indus/tui_bridge/command.py` (click command); shared: `src/indus/cli/lazy.py`
(hidden lazy entries), `src/indus/cli/app.py` (`HIDDEN` row), `src/indus/cli/entry.py` (stdio log
mode for the bridge); protocol additions in `src/indus/protocol/{base,bridge,errors,methods,events,
codegen}.py`. Tests `tests/unit/tui_bridge/test_stdio_server.py`, `test_dispatch.py`,
`tests/contract/tui_bridge/test_bridge_process.py` (real subprocess).

Edge cases / failure modes:
- garbage line, a JSON array, a JSON scalar → `-32700` (not JSON) or `-32600` (not a frame), id
  null when unreadable; the next request is served.
- blank or whitespace-only line → ignored, no reply.
- a response or notification from the client → logged at debug and dropped (our server requests are
  answered by method, not by response frames).
- unknown method → `-32601`; a method declared in the protocol but not served by the bridge → also
  `-32601` with that wording; unknown param key → `UNKNOWN_PARAM_KEY` (new, 1002) with the dotted
  path in `data.path`; other param errors → `-32602`.
- read returns EOF while the descriptor has `O_NONBLOCK` set, or `EAGAIN` at all (a child flipped
  the shared description, or set a receive timeout on the socket pair) → clear the flag and the
  timeout and read again; more than 10 such recoveries in 60 s is treated as a real EOF so a
  hostile child cannot spin us. Genuine EOF → shut down. SIGTERM → the same clean shutdown;
  SIGINT is ignored (the UI owns Ctrl+C).
- stdout broken (the UI died) → the writer reports failure; the loop stops and sessions close.
- handler raises `IndusError` → mapped through `protocol_error_for` (store/provider/busy codes);
  anything else → `-32603` "handler error: <type>" with the traceback in the log only (params are
  never logged: they may hold a sudo password or a secret answer).
- result or event that does not validate against its model: strict mode (tests,
  `INDUS_PROTOCOL_STRICT=1`) raises `ContractViolation`; production logs a warning and still sends.
- Windows: no descriptor tricks beyond what `os.dup` gives; close-on-exec is POSIX only.

Our design differences / decisions:
- one asyncio loop in the main thread runs the service and every handler; blocking work uses the
  D-04.1 pools (`foundation.blocking.run_blocking`); the stdin reader is a daemon thread feeding
  the loop.
- every handler runs under `use_scope(process scope)` (R05.14); one profile per bridge process.
- stdout redirection at descriptor level, not only `sys.stdout` replacement: covers C extensions
  and children too.
- MCP discovery: the bridge starts `indus.mcp` discovery in the background when that package offers
  a starter, never awaiting it before `gateway.ready` (P13 owns discovery; no starter → nothing).
- `gateway.ready` keeps the reference's event name (the plan names it); `theme` is the configured
  `tui.theme` (default `auto`).
- the service is built lazily on the first call that needs it, so a profile with no usable provider
  still answers `session.list`, completion and config calls; session calls then fail with
  `PROVIDER_UNAVAILABLE`.

Tests we will write:
- pipe pair: garbage then a valid request → `-32700` with null id, then the answer; blank lines;
  unknown method; unknown key with path; handler crash answered and loop alive; non-object frame.
- a subprocess script that claims stdio, prints, runs a child that tries to read stdin: stdout holds
  only frames, the child read nothing, the private RPC descriptor still has the bytes.
- spurious EOF: a pipe set non-blocking before data arrives is recovered and the line is read.
- strict result check raises; lenient logs.
- real `python -m indus tui-bridge --stdio` child: ready frame first, garbage answered, catalogue
  call, EOF exits 0, every stdout line parses as a frame.

## Decisions for the lead
- Hidden subcommand: `LazyGroup` gained a `hidden` list (lazy, never listed in help or the
  registry); `app.HIDDEN` holds `tui-bridge`. Change: register it in `REGISTRY` if it should show.
- Log mode `stdio` for `tui-bridge` in `cli/entry.py` (one line).
- `src/indus/tui_bridge/__init__.py` stays a one-line docstring: `tests/unit/test_layers.py` requires
  it (same ruling as `protocol`/`state`), so there are no package exports.
- The real service builder (`tui_bridge/host.py`) is a lean copy of `cli/runtime.build_service`
  (route, store, terminal environments, run context, aux client, kernels) without the learning
  wiring: the bridge may not import `indus.cli`. Proposal: move `build_service` into `indus.service`
  so both surfaces share it.

Compare checklist (filled after the rewrite; reopened `tui_gateway/entry.py`, `AGENTS.md`,
`server.py` write path):
- [x] blank lines ignored, parse error `-32700` id null, loop continues (test:
  `tests/unit/tui_bridge/test_stdio_server.py::test_garbage_line_gets_parse_error_then_next_request_is_served`)
- [x] stdin close-on-exec / child cannot read requests (test:
  `test_stdio_server.py::test_claimed_stdio_keeps_noise_and_children_off_the_channel`)
- [x] spurious EOF recovery with a cap (test: `test_stdio_server.py::test_nonblocking_flip_is_recovered`,
  `::test_recovery_budget_turns_into_eof`)
- [x] handler crash answered, loop alive (test: `test_dispatch.py::test_handler_crash_is_answered`)
- [x] unknown key code with path (test: `test_dispatch.py::test_unknown_key_names_its_path`)
- [x] contract check raises under test flag, logs in production (test:
  `test_dispatch.py::test_bad_result_raises_in_strict_mode`, `::test_bad_result_is_logged_when_lenient`)
- [x] first frame is the ready notification (test:
  `tests/contract/tui_bridge/test_bridge_process.py::test_real_child_speaks_ndjson`)
- [x] MCP discovery never blocks startup (no starter in `indus.mcp` yet → skipped; structural)
- [x] gap found: a read that fails with `EAGAIN` while the flag is clear (a receive timeout a child
  set on the socket pair) also counts as spurious — fixed by treating `EAGAIN` as spurious and
  zeroing `SO_RCVTIMEO`, pinned by
  `test_stdio_server.py::test_receive_timeout_on_a_socket_pair_is_recovered`
- [x] gap found: Ctrl+C in the terminal reaches the child too — SIGINT is now ignored and SIGTERM
  closes sessions and exits 0, pinned by
  `test_bridge_process.py::test_sigint_is_ignored_and_sigterm_closes_cleanly`
- [x] hidden command never listed (test: `test_bridge_process.py::test_bridge_is_hidden_from_help`)
- Deliberate differences: the reference marks only stdin close-on-exec and trusts code to pass
  `stdin=`; we also move descriptor 0 to the null device and descriptor 1 to stderr. The reference
  answers inline handler crashes with a server-band code; we use `-32603` (our table has no
  server-band codes). The reference reserves the profile home against a running gateway; there is
  no daemon in this phase (P17), the per-session lock already refuses a second writer.
- Not taken: the crash-log file and thread-stack dump on signals (our log files already record
  the exit), sidecar event mirror, heartbeat refresher, orphan sweep, skin watcher, picker cache
  prewarm (later phases or not needed: no daemon, themes are TS-side).

## Status

Done (2026-10-19). Shared changes listed above; the lead applies the dispatch registration lines as
they are (all handler modules are this slice's).
