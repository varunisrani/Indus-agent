# Task: MCP serve — approval tools wired to the daemon (P19-17) Phase: 19 Owner: wave-30 builder M1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `mcp_serve.py` (pending approvals in
the bridge, `permissions_list_open`, `permissions_respond` — hollow: nothing fills the list and an
answer never reaches the gateway, R09.10), `tests/test_mcp_serve.py` (approval lifecycle and the
permissions end-to-end cases). Research 09 §3.12, R09.10; D-16.30.
Evidence: none.

What it does (own words): when the profile's daemon is running and offers its approval queue, the
MCP server adds two tools. `permissions_list_open` lists every approval prompt that is waiting for
an answer in any session the daemon hosts (messaging chats, attached CLI/TUI sessions): which
conversation asks, the command (redacted), why, and which answers are allowed.
`permissions_respond {id, decision}` answers one of them through the same generation-bound control
every other surface uses (P17-05): the first answer anywhere wins, a later one is told the prompt
is already settled. The bridge also turns prompts appearing and disappearing into
`approval_requested` / `approval_resolved` events in its queue, so `events_wait` wakes on them.
Without the daemon (or with a daemon that does not offer the queue) the two tools are not listed at
all (D-16.30): a client never sees tools that cannot work.

Inputs → outputs:
- `permissions_list_open {}` → `{count, approvals[{id, session_key, session_id, kind, command,
  description, choices[allow-once|allow-session|allow-always|deny], generation}]}`.
- `permissions_respond {id, decision}` → `{resolved: true, approval_id, decision}`, or
  `{resolved: false, approval_id, reason: settled | stale_generation | not_allowed}`, or `{error}`.
- events: `{type: approval_requested, approval_id, session_key, command}` when a new prompt is seen,
  `{type: approval_resolved, approval_id, session_key, decision?}` when it is gone (`decision` only
  when this server answered it).

Files: `src/indus/mcp_serve/approvals.py`, `src/indus/service/messaging_bridge.py` (verbs and
capability), `src/indus/gateway/daemon/bridge_verbs.py` (`approvals-open`, `approvals-respond`),
`tests/contract/mcp_serve/test_approvals.py`.
Shared (minimal): `src/indus/service/daemon_host.py` (`open_requests(session_id)`: a plain read of
a hosted session's open server requests), `src/indus/gateway/daemon/side_services.py` (register
the verbs), `src/indus/gateway/control_socket.py` (verb names known and owner-only).

Edge cases / failure modes:
- Daemon absent at start → tools absent, even if it starts later (the list is fixed at start; the
  client restarts the server to pick them up). Daemon gone after start → the tools answer
  `{error: "daemon not running"}`.
- Unknown id → `{error}`; a decision outside the four → `{error}` naming them; a decision the
  prompt does not offer (e.g. `allow-always` where permanent approval is not allowed) →
  `{resolved: false, reason: not_allowed}`.
- Second answer, or an answer after another surface answered → `{resolved: false, reason:
  settled}`; the turn moved on (generation changed) → `stale_generation`.
- Command text passes the redactor again before it leaves the daemon (defence in depth; the
  request already carries a display-redacted command).
- Only approvals are listed (clarify/sudo/secret prompts need typed input and stay with the
  surfaces that can show them).
- A prompt in a session with no viewer that can show approvals is settled `undeliverable` at once
  by the daemon (P17-07) and never reaches the queue.

Our design differences / decisions:
- Real wiring to the daemon (the reference's two tools were hollow, R09.10, D-16.30), plus
  `allow-session` (our approval choices: once / session / always / deny).
- The verbs are owner-only on the control socket; the capability is advertised by the
  `approvals-open` answer itself (`queue: "approvals.v1"`), checked once at server start.
- Events come from watching the daemon's queue each bridge tick that a client is listening for;
  the control socket has no push stream.

Tests we will write (`tests/contract/mcp_serve/test_approvals.py`, a real `DaemonSessionHost`
with a fake model and a viewer, a real control socket):
- no daemon → the two tools are not in the list.
- open prompt → listed with a redacted command and its choices; respond → the daemon's turn gets
  the answer for that generation; second respond → `{resolved: false, reason: settled}`.
- `approval_requested` / `approval_resolved` appear in `events_poll`.
- bad decision, unknown id.

## Decisions for the lead
- Open approvals present when the watcher first runs are announced as `approval_requested` (they are pending work, not history).
- A daemon without a session host refuses `approvals-open` (`unavailable`), so the bridge offers no approval tools.
- Approvals in a session with no viewer able to show them are settled `undeliverable` by the daemon at once (P17-07) and never reach the bridge; the MCP server is not a viewer. Follow-up if MCP-only approval is wanted: let the bridge register as an approval-capable viewer.
- Decision names `allow-once` / `allow-session` / `allow-always` / `deny` map to our
  `once` / `session` / `always` / `deny`.
- The approval watcher polls `approvals-open` every 1 s (`approvals.WATCH_SECONDS`), only after the
  tools are registered.
- Verb names `approvals-open`, `approvals-respond`; capability string `approvals.v1`.

Compare checklist (filled after the rewrite; reopened `mcp_serve.py`, `tests/test_mcp_serve.py`):
- [x] list empty / with approvals (test: `tests/contract/mcp_serve/test_approvals.py::test_an_open_prompt_is_listed_answered_once_and_announced`)
- [x] respond allow / invalid decision / unknown id / second answer settled (test: `tests/contract/mcp_serve/test_approvals.py::test_an_open_prompt_is_listed_answered_once_and_announced`)
- [x] answered on another surface first → settled here (test: `tests/contract/mcp_serve/test_approvals.py::test_a_prompt_answered_elsewhere_first_is_settled_here`)
- [x] tools absent without a daemon, or with a daemon that has no session host (test: `tests/contract/mcp_serve/test_approvals.py::test_without_a_daemon_the_approval_tools_are_not_listed`, `tests/contract/mcp_serve/test_approvals.py::test_a_daemon_without_a_session_host_offers_no_queue`)
- [x] the hollow gap (R09.10): nothing filled the list and answers never left the process — ours reads and answers the daemon's queue; the answer reaches the waiting tool with the listed generation (same test)
- Deliberate differences: decisions add `allow-session`; ids are the daemon's request ids (`srq-…`); `approval_resolved` carries `decision` only when this server answered; the reference listed only approvals "observed" by the bridge.
- Not taken: clarify / sudo / secret prompts (typed input; the surfaces that render them keep them).

## Status

Done (2026-10-20). Approval tools wired to the daemon and tested (4 tests). Shared: `src/indus/service/daemon_host.py` (`open_requests(session_id)`), `docs/plan/16-what-we-change.md` (D-16.30 cell points here), side_services / control_socket as in P19-16. Traps R09.10, ACC-M11-1.
