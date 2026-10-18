# Task: Human approval over the broker (P05-09) Phase: 05 Owner: wave-2 builder B

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/approval.py` (the human
decision engine: choices, timeout vs withdrawal vs deny, deny reason relay, persistence of
session/always), `tools/approval_human_wait.py` (whole: wait accounting), `tools/approval_gateway_wait.py`
(withdrawal and timeout wording only), `tools/approval_prompt.py` (choice list only). Research
`02-tools.md` §3.13 and §3.24, traps R02.5, R02.9; `05-tui.md` R05.22. Closed before writing.
Evidence: none.

What it does (own words): when the pipeline (P05-08) needs a person, it sends one `approval`
server request through the session's broker and turns the outcome into a decision. The person sees
the command with secrets masked on the server, with long lines wrapped and nothing cut off, and
picks once, session, always or deny. Silence is not consent and a withdrawn question is not a
refusal: both block the command but say so plainly, so the model does not believe the user said
no (R02.5).

Inputs → outputs: `await ask_human(command, detection, ctx) -> ApprovalDecision`.
- `once` → approved for this run only.
- `session` → approved, pattern key stored for this profile + session.
- `always` → approved, the exact command appended to `command_allowlist` of the scope's profile
  through the config writer; a second identical command passes at step 5 without a prompt.
- `deny` → blocked, `deny_reason` = the person's reason, verbatim, also quoted in the message.
- timeout (`approvals.timeout`, 300 s) → blocked, outcome `timeout`, "no answer, nothing ran".
- withdrawn (interrupt, turn end, session closed) or undeliverable (client cannot answer) →
  blocked, outcome `not_answered`.
- Wait time: the broker counts every second a request is open (`human_wait_seconds`), which the
  P04 executor already subtracts from batch deadlines; the decision also carries `waited_s`.
- Stale interrupt (R02.9): a human-approved decision carries a one-shot reset; the terminal tool
  calls `decision.clear_stale_interrupt(flag)` once before its retry loop; a second call does
  nothing.
- Auto-approve per session lives in the approval state; `set_auto_approve(key, on, persist=…)`
  writes the session row first (the service passes `state.sessions.set_yolo`), and
  `restore_auto_approve` re-arms it on resume. A new session starts with approvals on.

Files: `src/indus/safety/approvals/human.py`, `tests/unit/safety/approvals/test_human.py`,
`tests/fakes/broker.py`; shared: `src/indus/protocol/server_requests.py` (`ApprovalAnswer.reason`)
and the regenerated `packages/protocol/src/generated/*`.

Edge cases / failure modes:
- Config write for `always` fails (managed install, unreadable file): the command still runs
  once and the decision says the rule was not saved.
- No scope or no broker: the pipeline never reaches here (step 6 handles it).
- A command with an embedded secret: the request carries the masked text; the command that runs
  is the original.
- Very long single-line commands: wrapped at 100 characters without dropping a character.

Our design differences / decisions:
- The request is the protocol's `approval` server request; no local terminal prompt exists in
  the safety layer (surfaces render it).
- `always` stores the exact command text instead of the pattern key (narrower; category approvals
  stay per session).
- The deny reason travels in a new optional `reason` field on `ApprovalAnswer`.

Tests we will write:
- Fake surface (real broker, scripted client) answers once / session / always / deny-with-reason.
- Timeout via the fake clock → blocked `timeout`; withdraw → blocked `not_answered`; client cannot
  answer → blocked `not_answered`; neither counts as a denial.
- `always` writes the allowlist and the next identical command skips the prompt.
- Broker wait time grows by the time the answer took; `waited_s` on the decision.
- Stale interrupt cleared exactly once.
- Display text: redacted, wrapped, nothing lost.
- Auto-approve: persist called, restore after "resume", new session off.

## Decisions for the lead
- `ApprovalAnswer` gained `reason: str | None` (protocol change, generated TS regenerated).
- `always` stores the exact command (see above).

Compare checklist (filled after the rewrite; reopened `tools/approval.py`,
`tools/approval_human_wait.py`, `tools/approval_gateway_wait.py`):
- [x] Timeout and withdrawal reported apart from deny, neither counted as a denial (tests:
  `test_human.py::test_timeout_blocks_with_timeout_outcome`, `test_human.py::test_withdrawn_is_not_answered`)
- [x] Deny reason verbatim (test: `test_human.py::test_deny_passes_reason_verbatim`)
- [x] Display text redacted server-side and wrapped, not clipped (test:
  `test_human.py::test_request_shows_redacted_wrapped_command`)
- [x] Wait time measured at the source (broker) (test: `test_human.py::test_wait_time_is_reported`)
- [x] Stale interrupt cleared once (test: `test_human.py::test_stale_interrupt_cleared_once`)
- [x] Auto-approve persists before the live switch flips; resume restores; new session off (test:
  `test_human.py::test_auto_approve_persist_and_restore`)
- Deliberate differences: exact-command `always`; no plugin pre/post approval hooks (P13); no
  multi-waiter coalescing (one broker request per decision; the broker serialises answers).
- Not taken: CLI prompt panel (surfaces own rendering); smart-denied once/deny-only choice list
  (P08 will pass `choices`).

## Status

Done (2026-10-18). Shared edits: `src/indus/protocol/server_requests.py`, generated protocol
files. The service wires `set_auto_approve(..., persist=lambda on: sessions.set_yolo(store, sid, on))`.

## Integration (lead)

Merged in the P05 wave 2 integration (2026-10-18, lead's verify tree).
- The terminal tool now calls the real pipeline directly (`indus.safety.approvals.decide` with
  `ApprovalContext`, typed `ApprovalDecision`); C's stand-in `tests/unit/tools/builtin/_stubs_c.py`
  is deleted. `clear_stale_interrupt` stays a method on `ApprovalDecision` as built here (the
  integration list called it a module function; the code and C's call agree on the method).
  The terminal passes the turn's cancel signal only when it can be cleared (`clear()`), else
  `None`. Pinned by `tests/unit/tools/builtin/test_terminal_plan.py::test_stale_interrupt_cleared_once_before_retries`
  with a real `StaleInterruptReset`.
