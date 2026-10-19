# Task: recorded event streams and mock gateway (P12-06) Phase: 12 Owner: wave-8 builder R1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`):
`ui-tui/src/app/createGatewayEventHandler.ts` (only the event switch, to list what each fixture has
to exercise). Research `05-tui.md` §3.5. Evidence: none.

What it does (own words): eight hand-written NDJSON files replay realistic sessions so every
renderer test runs without Python: a plain streamed reply; commentary with three tool calls, an
inline diff and a todo update; an approval mid-turn then completion; a clarify with two questions
(one multi-select); an interrupt mid-stream whose stored partial is longer than the sealed text; a
failed turn; a 10,000-token reply; session list and model options payloads. `mockGateway` loads a
fixture into `@indus/client`'s fake server and replays it in order with injected timing (fake
timers in tests). Fixtures are written from our own protocol models, never captured from the
reference; P12-33 regenerates them from real bridge runs.

Line format (one JSON object per line; `delay_ms` waits before the line, scaled by the timing):
- `{"event": {...}}` — a protocol event; `seq` is stamped by the gateway (per session, from 1).
- `{"notify": {"method": "gateway.ready", "params": {...}}}` — another notification.
- `{"ask": {...ServerRequest}}` — a server request; the replay waits for the client's answer.
- `{"await_call": "session.interrupt"}` — the replay waits until the client calls the method.
- `{"result": {"method": "session.list", "value": {...}}}` — answer that method with the value.
- `{"repeat": N, "event": {...}}` — the event N times, `{n}` in string fields replaced by 1..N.
- `#` lines are comments.

Validation: every event (after stamping) against its `$defs` type in
`packages/protocol/src/generated/schema.json`, notifications against `NOTIFICATIONS`, asks against
`ServerRequest`, results against the method's result type, with a small JSON-Schema checker of ours
(`testing/schemaCheck.ts`, the subset the generated schema uses).

Files: `apps/tui/src/testing/{mockGateway,schemaCheck}.ts`, `apps/tui/src/testing/fixtures/*.ndjson`
(8), `apps/tui/src/testing/mockGateway.test.ts`.

Edge cases / failure modes: a line that fails validation throws at load with file:line; unknown
line kinds throw; the replay can be stopped; awaiting an answer that never comes keeps the replay
paused (tests answer or stop it).

Our design differences / decisions: fixtures live as data, not captures; seq stamping keeps them
short and editable; `repeat` keeps the 10k-token reply small on disk.

Tests we will write: every fixture validates; each replays in order under fake timers; ask waits for
the answer; await_call waits for the call; result lines answer the method; an invalid line fails.

Compare checklist (filled after the rewrite; reopened the event switch):
- [x] streaming, tool start/progress/complete, todo, status kinds, notice, server request (approval,
  clarify), interrupt, turn failure, usage/title, picker payloads each appear in a fixture
- [x] every step validates (tests: `mockGateway.test.ts::<name> validates against the generated protocol schema`)
- [x] replay in order under fake timers; ask/await_call/result behaviour (tests in `describe("replay")`)
- Deliberate differences: fixtures use our event names; the reference's interim/rendered/error events
  have no counterpart in our protocol, so no fixture carries them.
- Not taken: subagent, voice and billing streams (no protocol events yet); P12-33 regenerates.

## Status

Done (2026-10-19). `apps/tui/src/testing/{mockGateway,schemaCheck,harness}.ts`, 8 fixtures,
`mockGateway.test.ts` (22). Shared: none.
