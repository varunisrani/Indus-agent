# Task: Attached host — the daemon viewer (P19-04) Phase: 19 Owner: wave-29 builder A1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `acp_adapter/gateway_server.py`
(`_client`, `_reattach`, `_request_live_reasoning`, `_notify_unknown`, `_transport_closed`,
`_teardown`, `_events`), `acp_adapter/gateway_reply.py` (how replies reach the editor; for the
event contract). Our P17-15..17 `service/remote/channel.py`, `service/remote/host.py`
(`RemoteSessionService`: journal, outcome unknown, resubscribe from cursor, re-offer),
`service/gateway_control.describe`, `service/ws_auth.stable_profile_id`, `cli/attach_remote.py`
(read-only ledger reads). Parked w28-l2 notes (runtime.describe, admission.receipt). Closed before
writing.
Evidence: none.

What it does (own words): by default the ACP process is one more viewer of the profile's daemon.
It connects lazily, on the first call that needs the daemon: the owner-only control socket answers
`describe` (P17-11; stands in for P17-25's `runtime.describe`), whose `profile_id` must equal this
process's own (`stable_profile_id(home)`) — else `profile_mismatch`, terminal; then a single-use
ticket and the WebSocket (the P17-16 `Channel`, surface `acp`), with `RemoteSessionService` (P17-17)
on top. The first dial never starts a daemon (D-16.83); later redials may recover a daemon that
died (P17-15 `recover` mode, within its budget).

- Creating a session requires the daemon to list `acp` among its creatable sources and advertise
  the editor-policy capability `acp.editor_policy`; otherwise `acp_policy_unavailable`.
- Events arrive through the remote service's listeners: each carries `seq` and the replay epoch;
  the remote service drops what it already applied.
- Transport loss: every waiter on the old connection fails at once with "outcome unknown, do not
  resend" (R09.11) — the remote service does this for turn waits; our own waiters (submit) map
  `TransportLost`/`OutcomeUnknown` to `outcome_unknown`. The channel redials (backoff); on the
  new connection (an `on_ready` hook, before callers' requests flow again) every held session is
  resubscribed from its last `seq` (remote service), then our hook re-attaches the editor MCP
  specs (`session.mutate editor_mcp`), asks for live reasoning again (`config.set show_reasoning`,
  10 s, failures ignored), and announces turns lost in a daemon restart (read-only runtime-ledger
  read: rows in state `unknown`) once per admission.
- Pending approval/clarify prompts are offered again by the daemon on resubscribe; the remote
  service passes each request id to listeners once, so an editor card that is still up is not
  duplicated.
- A channel that failed for good (terminal dial refusal) or was closed is torn down and rebuilt by
  the next call.

Inputs → outputs: `AcpBackend` calls → JSON-RPC over the daemon's WebSocket; events → sink.

Files: `src/indus/acp/attached_host.py`, `tests/contract/acp/test_attached_host.py`,
`tests/fakes/daemon.py` (a shim over the real P17 endpoint that serves the stand-in methods);
shared: `src/indus/protocol/attach.py` (`ACP_EDITOR_POLICY` capability name).

Edge cases / failure modes:
- No daemon at first call → `daemon_unavailable` (or local fallback, P19-03).
- Profile mismatch → `profile_mismatch` (never retried on its own).
- Daemon without `session.mutate` / `config.set` / `session.usage` over WebSocket (today's daemon):
  `-32601` → `mutation_unavailable` / ignored / no usage update.
- Launch options the wire cannot carry yet (anything but cwd/model/toolsets) → refused with a
  pointer to `--local` (as the CLI does, D-16.490).

Our design differences / decisions:
- Built on the landed P17 channel and remote service instead of a second client: reconnect,
  resubscribe-from-cursor and outcome-unknown are theirs; this host adds the ACP-specific hooks.
- Stand-ins (wiring owed, P17-25): `describe` over the control socket for `runtime.describe`;
  `session.mutate`, `config.set`, `session.usage`, `editor` policy on `session.create` over the
  WebSocket; the `acp.editor_policy` capability is defined but not advertised by the daemon yet, so
  a real daemon answers `acp_policy_unavailable` until it honours the policy.

Tests we will write (real P17 endpoint over the ASGI double, `tests/unit/service/remote/wire.Rig`):
- profile mismatch refused; daemon without the capability refused;
- kill the transport mid-turn → the waiter gets outcome-unknown; the reconnect resubscribes from
  the last seq (no duplicate events); a pending approval is offered once across the reconnect;
- lost turn after a daemon restart announced once.

## Decisions for the lead
- See the stand-ins above; `ACP_EDITOR_POLICY = "acp.editor_policy"` is the capability name.
- First dial is discover-only (no auto-start, D-16.83); redials use the recover budget.

Compare checklist (filled after the rewrite; reopened `gateway_server.py` connection/pump):
- [x] lazy connect + profile check
- [x] reattach: resubscribe, MCP, reasoning, prompts, unknown notice
- [x] waiters fail with outcome unknown on transport loss
- Deliberate differences: see above.

## Status

Done (2026-10-20), wave-29 builder A1. Tests: see the task's Verify line; all green with the gate checks. Trap/ACC ledger rows for R09.9/R09.13/R09.14/R09.16 and ACC-M1/M2/M8 left to the lead (follow-up).

## Integration (P19 integrator, 2026-10-20)

Landed on main after P17-23..27, so the stand-ins above were checked against the real daemon:

- `describe`: the host no longer has its own pre-dial `describe`. The channel's dial already checks
  the profile (control-socket `describe` before the ticket, `gateway.ready` after); once connected
  the host reads `runtime.describe` over the WebSocket (again on every reconnection) and checks its
  profile id once more (D-16.609).
- `session.usage`: served by the daemon now (D-16.608). `admission.cancel`: added to the attach
  protocol and served by the daemon — queued inputs only; a claimed one comes back
  `state: started` with its generation and the host interrupts that generation (D-16.607).
- `config.set show_reasoning`: not needed — the daemon streams `reasoning.delta` to every viewer,
  so `show_reasoning` is a no-op and nothing is sent on reconnect (D-16.603).
- `session.mutate` and the `acp.editor_policy` capability are still owed by the daemon (D-16.602):
  a real daemon answers `session/new` with `acp_policy_unavailable`
  (`tests/e2e/acp/test_acp_daemon.py`), and `--local` is the path that creates sessions. The
  contract suites run over the real endpoint with only `session.mutate` shimmed
  (`tests/fakes/daemon.AcpDaemon`, merged with the P17 `ProfileHome` harness).
