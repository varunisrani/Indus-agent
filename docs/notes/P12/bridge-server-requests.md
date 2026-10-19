# Task: Server requests over stdio (P12-30) Phase: 12 Owner: wave-7 builder G

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tui_gateway/server_requests.py`
(whole), `tui_gateway/contracts/server_requests.py` (request names, params and result shapes;
skimmed), `tui_gateway/agent_callbacks.py` (`_agent_cbs`, `_wire_callbacks`: which deadlines each
kind gets, the secret save path), `tui_gateway/server.py` `_ask` / `_clarify_timeout_seconds`
(found next to `write_json`). Research `05-tui.md` §3.17, R05.3. Ours:
`docs/notes/P04/session-service.md` part 2 (broker), `src/indus/protocol/requests_port.py`,
`src/indus/service/broker.py`, P05 notes (approval timeout, sudo prompt).
Evidence: none.

What it does (own words): when the agent needs a person (approve a command, answer questions, give
a sudo password or a secret) the session's broker turns the question into a server request. Over
stdio the bridge sends it as a `server_request` notification and the client answers with the
`server_request.answer` method. The bridge sits between each agent and its broker as the
`ServerRequestPort`: it fills in the deadline each kind gets in a terminal UI, keeps per-question
locks for multi-question clarify cards, and only lets requests out to a client that said it can show
them. A client that never declared `server_requests` gets nothing: the ask fails at once and the
tool takes its fallback (R05.3), instead of the agent waiting minutes for a card nobody sees.

Inputs → outputs:
- `client.capabilities {features}` with `server_requests` (all four kinds) or
  `server_requests.<kind>` → the service's answerable kinds; result lists the accepted features.
- broker request → `server_request` notification (`ApprovalRequest` / `ClarifyRequest` /
  `SudoRequest` / `SecretRequest` model as params, `deadline_s` filled).
- `server_request.answer {session_id, request_id, answer}` → `{accepted}`; a late answer for a
  withdrawn id → `SERVER_REQUEST_NOT_FOUND` (nothing else happens).
- `clarify.lock {session_id, request_id, question_id, answer[]}` → `{remaining[]}`; the last lock
  answers the request with every locked answer.
- withdrawal (timeout, interrupt, turn end, session close) → exactly one
  `server_request.cancelled {request_id, reason}` event.

Deadlines: sudo 120 s and secret 300 s (the bridge's values win over the asker's, a card needs the
time); clarify: the asker's value, else `agent.clarify_timeout` (default 300 s; ≤ 0 = no limit);
approval: the asker's value (P05 reads `approvals.timeout`), else 300 s.

Files: `src/indus/tui_bridge/server_requests.py`, `tests/contract/tui_bridge/test_server_requests.py`.

Edge cases / failure modes:
- no capabilities → `RequestUnsupported` raised at the asker in well under 10 ms, nothing written.
- a kind the client did not list → the same.
- timeout on the injected clock → one cancel event (`timed_out`), the asker gets `RequestWithdrawn`;
  a clarify that times out with some answers locked returns those answers (the rest skipped).
- interrupt / turn end / close → the broker withdraws (one event each); a second withdrawal is a
  no-op.
- secret request for a closed session → refused (`RequestWithdrawn(session_closed)`), nothing sent.
- secret and sudo values never appear in logs or events: the dispatcher never logs params, the
  answer goes straight to the waiting asker; requests carry only the prompt.
- a clarify lock for an unknown question → `INVALID_PARAMS` with the path; for a request that is no
  longer open → `SERVER_REQUEST_NOT_FOUND`.
- kinds the TUI cannot show (anything outside the four) never reach it: the protocol has no other
  kinds, and a broker refuses unknown kinds as a programming error.

Our design differences / decisions:
- landed P04 wire: request notification + answer method (not peer request/response frames), and
  `server_request.cancelled` instead of a separate cancel notification name.
- capability per kind (`server_requests.<kind>`) as well as the blanket flag, so a client that
  cannot show secrets is never asked for one.
- secret storage stays with the asking tool (the skill tool stores the value through the scope's
  `SecretStore`, keychain by default); the bridge does not store it a second time.
- re-delivery of open requests after reconnect is P17; here a restarted child has none (the turn
  died with it); `session.resume` still lists open ones for an in-process reattach.

Tests we will write:
- no capabilities → approval refused in < 10 ms, no frame written.
- sudo with no asker deadline times out at 120 s on the fake clock with exactly one cancel event.
- late answer after the timeout → `SERVER_REQUEST_NOT_FOUND`, no second event.
- clarify locks: partial locks then the last lock resolves; a timeout keeps the locked answers.
- secret answer value never in caplog records or emitted frames.
- an end-to-end approval through a fake-model turn over the dispatcher (answer accepted, turn
  completes).

## Decisions for the lead
- Added on the lead's request (TS client): `server_request.decline {session_id, request_id,
  reason?}` — the server answers with that kind's refusal (approval deny with the reason, clarify
  all skipped, no sudo password, no secret); `gateway.ping` → `{pong, protocol_version}` for
  heartbeats (pinned by `test_server_requests.py::test_decline_answers_with_the_kinds_refusal`,
  `::test_ping`).
- The bridge's sudo deadline (120 s) overrides P05's 45 s CLI prompt time for bridge sessions.
- `KNOWN_CLIENT_FEATURES` is no longer empty: `server_requests` and `server_requests.<kind>`.

Compare checklist (filled after the rewrite; reopened `server_requests.py`, `agent_callbacks.py`):
- [x] unanswerable client fails fast without writing a frame (test: `tests/contract/tui_bridge/test_server_requests.py::test_without_capabilities_nothing_is_sent`)
- [x] one cancel per withdrawal; late answer dropped (test: `test_server_requests.py::test_sudo_times_out_at_120_seconds_with_one_cancel`, `::test_late_answer_is_refused`)
- [x] batch clarify locks; locked answers survive a timeout (test: `test_server_requests.py::test_clarify_locks_resolve_on_the_last_answer`, `::test_clarify_timeout_keeps_locked_answers`)
- [x] secret refused for a closed session (test: `test_server_requests.py::test_secret_for_a_closed_session_is_refused`)
- [x] secret never logged (test: `test_server_requests.py::test_secret_value_never_logged_or_emitted`)
- Deliberate differences: no window-owned bridges, no "not shown" declines (desktop-only kinds are
  not in our protocol); the clarify deadline `≤ 0` means unlimited as in the reference.
- Not taken: vault, setup-beat, theme, tour, preview/terminal/window read requests (desktop or later).

## Status

Done (2026-10-19).
