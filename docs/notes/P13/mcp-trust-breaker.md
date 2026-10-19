# Task: Trust tier and circuit breaker (P13-07) Phase: 13 Owner: wave-16 builder N1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/mcp_tool.py` (breaker state
and constants, trust tier comments), `tools/mcp_tool_handlers.py` (trust gate and breaker checks in
the call path), `tools/mcp_tool_server_run.py` (auth-failure park and credential stamp; skimmed).
Ours: `safety/approvals/{decision,human,policy,smart}.py` (P05), the computer_use and execute_code
gates (same shape), `plugins/hooks.py` (`PluginToolHooks._approve`). Research 07 R07.24, R07.25.

What it does (own words):
- **Trust gate.** `trust: untrusted` (and any value the config does not recognise, already parsed
  as untrusted by P13-01; a missing value is `full`) → every call to a tool whose discovered
  `readOnlyHint` is not exactly JSON `true` needs a person's approval before the RPC. The SDK
  coerces `"yes"`, `1`, `"true"` to `True`, so the hint is read from the raw JSON-RPC response:
  `connect.py` taps the read stream and keeps each listed tool's raw hint (`LiveSession.raw_hints`).
  Hints are captured at discovery and frozen until the next listing (`list_changed`, reconnect,
  reload). Missing raw hint → not read-only (fail closed).
- **The approval** goes through a new generic per-tool gate, `safety/approvals/tool_gate.py`, with
  the same order as the computer_use / execute_code gates: no profile → blocked; auto-approve or
  `approvals.mode: off` → run; nobody can answer (cron / single query / unattended or no broker) →
  the context's policy key (`deny` default); approved earlier for this session (pattern key) → run;
  smart mode with a judge → the judge first; then the person through the session broker (once /
  session / deny). A gate that raises blocks. The same gate is the default for a plugin
  `pre_tool_call` `approve` directive (`PluginToolHooks` without an explicit `ask_approval` now asks
  through it, using the tool context's scope, session, broker and run context) — the HANDOFF seam.
- **Breaker** per server, keyed by (profile, server): 3 consecutive failures → open for 60 s (calls
  short-circuit with "server unreachable, retry in N s"); then half-open: one probe call goes
  through, others still short-circuit; probe success closes, probe failure re-opens for 60 s.
- **OAuth in the background (R07.24).** Discovery, reconcile and reload run with interactive auth
  off (`trust.interactive_auth_allowed()` is false unless a caller enters `allow_interactive_auth()`,
  e.g. `indus mcp login`): the OAuth layer (P13-09) must check it before opening a browser. A
  connect refused with 401/403 parks the server (P13-04 already treats it as permanent) and is
  recorded with the credential stamp at that time (`AuthParking`); one warning is logged. Later
  discovery passes (new sessions) do not nudge an auth-parked server while the stamp is unchanged —
  only the lifecycle's timed self-probe (300 s) retries the token; a changed stamp (re-login) or
  `host.reauthorised(name)` wakes it at once.

Inputs → outputs: tool call + frozen hint + server trust → run / refusal text; call outcomes →
breaker state; auth refusals → park record.

Files: `src/indus/mcp/trust.py`, `src/indus/safety/approvals/tool_gate.py` (new),
`tests/unit/mcp/test_trust.py`, `tests/unit/safety/approvals/test_tool_gate.py`; shared:
`src/indus/mcp/connect.py` (raw hint tap), `src/indus/plugins/hooks.py` (approve default).

Edge cases: `readOnlyHint: "yes"` (prompts), `readOnlyHint` absent (prompts), annotations absent
(prompts), typo `trust: untrsted` (untrusted), approval gate raising (blocked), denial (no RPC
sent), timeout of the person (blocked, not a refusal), breaker during half-open with two calls at
once (one probe), background 401 (parked, no browser, no token retry from new sessions).

Our design differences / decisions:
- The approval is the P05 human step with once / session / deny (no "always": the command
  allowlist is for shell text). Session approval key `mcp:<server>:<tool>`.
- YOLO / `approvals.mode: off` also pass the trust gate (same rule as computer_use).
- Unattended contexts use the existing policy keys (`approvals.cron_mode`, …), default deny.

Tests we will write: untrusted server: write tool prompts (fake approval denies → no RPC sent),
`readOnlyHint: true` tool runs without prompt, `readOnlyHint: "yes"` prompts; typo trust →
untrusted; breaker opens after 3 failures, half-open probe after 60 s (fake clock); background 401
→ parked, no browser call recorded, new sessions do not retry the token; tool gate order cases;
plugin approve directive asks through the gate.

## Decisions for the lead
- YOLO / mode off bypass the untrusted gate; unattended policy keys apply.
- `PluginToolHooks` default approval now asks the person (it blocked outright before).

Compare checklist (filled after writing; reopened `tools/mcp_tool.py`,
`tools/mcp_tool_handlers.py`):
- [x] untrusted (and unknown tier) gates calls whose hint is not exactly true; missing = full (`tests/unit/mcp/test_trust.py::test_untrusted_write_tool_asks_and_a_denial_sends_nothing`, `::test_full_trust_never_asks`)
- [x] malformed hint fails closed, read from the wire (`::test_read_only_is_exactly_true`, `tests/unit/mcp/test_discovery.py::test_read_only_hints_come_from_the_wire`, `tests/contract/mcp/test_host_live.py::test_stdio_server_tools_are_prefixed_and_callable`)
- [x] hints frozen until the next listing (`::test_hints_stay_frozen_until_the_next_listing`)
- [x] approval failure blocks (`tests/unit/safety/approvals/test_tool_gate.py::test_a_failing_gate_blocks`)
- [x] breaker 3 / 60 s / half-open probe, "rejected" vs "unreachable" (`::test_breaker_states`, `::test_breaker_says_rejected_when_the_server_answered`, `::test_breaker_opens_after_three_failures_then_probes`)
- [x] auth park with credential stamp; discovery does not nudge it; timed probe still retries (`::test_background_401_parks_without_a_browser_and_new_sessions_leave_it`)
- [x] plugin approve through the gate (`tests/unit/safety/approvals/test_tool_gate.py::test_plugin_approve_asks_through_the_gate`)
- Deliberate differences: once / session / deny (reference: accept / deny); YOLO and mode off pass
  the gate; unattended contexts use the P05 policy keys; the hint is taken from the raw JSON-RPC
  result (the SDK coerces strings and numbers to booleans).

## Status

Done (2026-10-19). `src/indus/mcp/trust.py`, `src/indus/safety/approvals/tool_gate.py`,
`tests/unit/mcp/test_trust.py`, `tests/unit/safety/approvals/test_tool_gate.py`; shared:
`src/indus/mcp/connect.py`, `src/indus/plugins/hooks.py`.
