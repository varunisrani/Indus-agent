# Task: Transports and connection (P13-03) Phase: 13 Owner: wave-10 builder M1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/mcp_tool.py` (task-per-server
model, SDK loading, 60 s connect default, list paging cap), `tools/mcp_tool_lifecycle.py` (pid
ledgers, process-group sweep, shutdown), `tools/mcp_tool_transport.py` (stdio and HTTP/SSE bring-up,
proxy mounts, handshake bound), `tools/mcp_tool_errors.py` (URL validation, client cert shapes,
identity header), `tools/mcp_tool_config.py` (stderr log), `agent/proxy_bypass.py` and `utils.py`
(`socks://` normalisation, NO_PROXY matching; skim). Research `07` §3.16.2, R07.22. Ours:
`safety/child_env.py`, `safety/url_policy.py`, `foundation/blocking.py`, `foundation/clock.py`,
`foundation/scope.py`. SDK: `mcp` 2.3.0 (`mcp.client.stdio`, `mcp.client.streamable_http`,
`mcp.client.sse`, `mcp.client.session`, `mcp.server.mcpserver` for the fakes). Evidence: none.

What it does (own words): opens one live MCP session for one configured server and keeps it
inside a single asyncio task: the SDK transports use anyio cancel scopes that must be entered and
left by the same task, so the task that opens the transport is the one that closes it.
- stdio: the SDK stdio client spawns `StdioLaunch` (P13-01 env, cwd) in a new process group;
  stderr goes to `<home>/logs/mcp-stderr.log` (rotated at 5 MiB, 3 old files) with a header line
  per start so a server's output can be found. On close the SDK closes stdin, waits, then signals
  the group; we then sweep the group once more after a 3 s grace so grandchildren that outlived
  their parent are gone too.
- Streamable HTTP (default for `url`) and legacy SSE (`transport: sse`): the SDK clients over an
  `httpx2` client we build: configured headers + identity header (explicit header of the same name
  wins), proxy from the scope env (`HTTPS_PROXY`/`HTTP_PROXY`/`ALL_PROXY`, `socks://` →
  `socks5://`), `NO_PROXY` with exact hosts, `.suffix`, `*.wildcard`, IPs and CIDR blocks, `*`,
  loopback always direct; mTLS from `client_cert`/`client_key` (a missing file is a clear error
  naming the server); `trust_env` off so only the scope's env counts.
- URL gate: the URL policy's rules that make sense for an operator-configured endpoint: http(s)
  only, host present, the website blocklist, cloud metadata names and addresses refused (also
  after resolving the name). Private and loopback addresses are allowed (local MCP servers are
  normal) and credentials in the URL are allowed (operator config, not model input).
- Handshake: `initialize` first; when the server rejects it as unknown/unsupported, one
  `server/discover` (2026-07-28 servers). The whole open (spawn + handshake) is bounded by
  `connect_timeout` on the injected clock.
- The live session (`LiveSession`) offers paged `tools/list` (≤ 50 pages), `tools/call`, `ping`,
  the negotiated capabilities, and a `dropped` signal set when the transport's read side ends
  (stdio crash, HTTP stream gone). Server notifications are handed to an optional callback
  (P13-08 uses `notifications/tools/list_changed`).

Inputs → outputs: (name, `McpServer`, scope, clock, workspace, options) → `LiveSession` inside
`open_session(...)`; `Connection` = one owner task around it (start / wait ready / close).

Files: `src/indus/mcp/connect.py`, `tests/unit/mcp/test_connect.py`,
`tests/contract/mcp/test_transports.py`, `tests/fakes/mcp/{__init__,stdio_server,http_server}.py`.

Edge cases / failure modes:
- server never answers `initialize` → `McpConnectTimeout` after `connect_timeout` (fake clock).
- command missing → spawn error (permanent for P13-04).
- unresolved `${VAR}` in url/headers → `McpUnresolvedRef` before any network.
- missing cert/key file → `McpClientCertMissing` naming the server and the path key.
- metadata URL → `McpUrlRefused`.
- stdio server crash mid-session → `dropped` set; in-flight calls fail with connection closed.
- SDK errors arrive wrapped in exception groups: unwrapped to the first real cause.

Our design differences / decisions:
- SDK 2.3.0 shuts stdio servers down itself (stdin close, 2 s, SIGTERM to the group, 2 s, SIGKILL);
  the row's "3 s grace then kill the tree" is met by our extra group sweep 3 s after the SDK
  returns (POSIX; Windows relies on the SDK's job object).
- No content-type preflight, no SSE fallback after a Streamable HTTP rejection, no response body
  cap, no redirect-header stripper, no parent-death supervisor (not in the row); noted for later.
- The live endpoint and OAuth (`auth: oauth`) are P13-09; until then an OAuth server connects
  without an auth handler and fails with the server's 401 (permanent).

Tests we will write:
- contract (fake servers on the SDK server side): stdio initialize/list/call/close; HTTP the same
  in process (ASGI transport); stderr captured to the log; process tree gone after close.
- unit: connect timeout (fake clock, a transport that never answers); mTLS missing file error;
  unresolved header ref; metadata URL refused; proxy plan from scope env (socks alias, NO_PROXY
  CIDR / wildcard / loopback); identity header precedence; stderr log rotation.

## Decisions for the lead
- MCP URLs skip the private-address and secret-in-URL rules of the URL policy (operator config);
  metadata, blocklist and scheme rules apply. Change in `connect.check_endpoint`.
- Handshake order is `initialize` then `server/discover`, not the SDK's discover-first default.

Compare checklist (filled after the rewrite; reopened `tools/mcp_tool_transport.py`,
`tools/mcp_tool_lifecycle.py`, `tools/mcp_tool_errors.py`):
- [x] stdio initialize / list / call / close against the SDK fake (`tests/contract/mcp/test_transports.py::test_stdio_initialize_list_call_close`)
- [x] Streamable HTTP in process: headers, identity header, list, call (`::test_http_initialize_list_call_close`)
- [x] connect timeout with a server that never answers, fake clock (`::test_connect_timeout_with_a_silent_server`, `tests/unit/mcp/test_connect.py::test_connect_timeout_on_a_server_that_never_answers`)
- [x] mTLS missing file names the server, before any request (`test_connect.py::test_missing_client_certificate_names_the_server`)
- [x] proxy env honoured, `socks://` normalised, NO_PROXY with CIDR / wildcard / suffix / port, loopback direct (`::test_http_proxy_from_the_scope_env`, `test_connect.py::test_proxy_choice`)
- [x] stderr captured to the log with a start header, tail in the error of a server that dies at start (`::test_stdio_stderr_goes_to_the_log`, `::test_stdio_server_dying_at_start_reports_its_stderr`)
- [x] process tree gone after close, also a grandchild that outlived the server (`::test_process_tree_is_gone_after_close`)
- [x] handshake: initialize first, discover only when initialize is refused as unknown/unsupported (`test_connect.py::test_handshake_falls_back_to_discover_only_when_initialize_is_refused`)
- [x] `tools/list` paging capped at 50 pages (`test_connect.py::test_tool_listing_follows_cursors_and_stops_at_50_pages`)
- [x] gap found: a server without the optional `ping` would have been reconnected every keepalive — fixed in P13-04 (`tools/list` fallback after -32601), pinned by `tests/unit/mcp/test_lifecycle.py::test_a_server_without_ping_is_probed_with_a_tool_listing`.
- Message types (MCP 2025-11-25 / 2026-07-28 via SDK 2.3.0): `initialize` handled; `server/discover` handled (fallback); `tools/list` handled (paged); `tools/call` handled; `ping` handled (keepalive); `notifications/tools/list_changed` delivered to `ConnectOptions.on_notification` (P13-08 acts on it); `notifications/{prompts,resources}/list_changed`, `notifications/message`, progress: delivered to the same hook, ignored for now; `resources/*`, `prompts/*`: reachable through `LiveSession.raw`, later (P13-05/06); `sampling/createMessage`, `elicitation/create`: later (P13-10, `ConnectOptions.session_kwargs` carries the callbacks); OAuth: later (P13-09, `ConnectOptions.auth`).
- Deliberate differences: the reference finds child pids by diffing `/proc` child lists around the spawn; we tag the run (`INDUS_MCP_RUN_ID=<random>` in the child env) and sweep tagged processes after the SDK's own shutdown (SIGTERM, 3 s grace, SIGKILL). The SDK's own sequence (stdin close, 2 s, SIGTERM to the group, 2 s, SIGKILL) runs first and cannot be tuned without patching it. The SDK merges `HOME LOGNAME PATH SHELL TERM USER` from the process env under ours (names only, no credentials).
- Not taken: content-type preflight, SSE fallback after a Streamable HTTP rejection, response body cap, redirect header stripper (SDK 2.3.0 follows only same-origin redirects itself), parent-death supervisor, Windows job self-attach, `ssl_verify`, live-endpoint files.

## Status

Done (2026-10-19). `src/indus/mcp/connect.py`, `tests/unit/mcp/test_connect.py`, `tests/contract/mcp/{__init__,test_transports}.py`, `tests/fakes/mcp/{__init__,stdio_server,http_server}.py`. New dependency `mcp==2.3.0` (+ 13 transitive rows in THIRD_PARTY_NOTICES); `uv.lock` regenerated here for testing (lead re-locks). Legacy SSE has no in-process contract test: `httpx2.ASGITransport` buffers whole responses, so an SSE stream never arrives; the SSE wiring is pinned by `test_connect.py::test_sse_transport_builds_its_client_through_the_factory`.
