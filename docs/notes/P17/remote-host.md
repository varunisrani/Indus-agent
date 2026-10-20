# Task: Remote session host (P17-17) Phase: 17 Owner: wave-27 builder K6

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/gateway_agent.py` (submit
params and receipt, the frame pump into callbacks, server-request answers, withdrawal, the result
shape, `_failed`), `hermes_cli/gateway_connection.py` (resubscribe after a redial, session state
caches). Research 08 §3.9 (prepared-submission journal), research 09 R09.11, research 05 R05.21.
Our P04 `SessionService` / `InProcessHost` (the interface surfaces use), P17-13/14 WS methods
(`session.create/resume/close`, `prompt.submit`, `session.interrupt/steer`,
`server_request.answer/decline`, `session.subscribe`, `events.since`). Closed before writing.
Evidence: none.

What it does (own words): `RemoteSessionService` is the session service a surface uses when the
agent runs in the daemon. It has the same calls as the in-process service — create, resume,
submit, wait, interrupt, steer, answer a server request, events, live listeners, close — and
turns each into a JSON-RPC call over the attach channel (P17-16), so the REPL cannot tell where
the agent runs. Events arrive as notifications and are turned back into the same event models
(`TurnStarted`, `MessageDelta`, `TurnDone`, …); server requests arrive as offers and are handed on
as the same request models, remembering the generation each offer named so an answer names it.

- Create is idempotent by a client request id: every create carries an `idempotency_key` (one the
  caller gave, else a fresh one); when the reply is lost with the socket, the same create is sent
  again with the same key after the reconnect and returns the same session (R05.21).
- Submit: before anything is sent, a prepared-submission journal entry is written (an atomic
  0600 file under `<home>/state/attach-journal/`, directory 0700) holding the session, the text
  and the request id. The submit carries that id as the admission request id. An accepted answer
  retires the entry. A refusal from the daemon (busy, empty, conflict) retires it too — nothing
  was admitted. "Not sent" retires it. A lost transport after the submit left marks it `unknown`
  and raises `OutcomeUnknown` ("outcome unknown, do not resend"); it is never resent on its own
  (R09.11). `retry(request_id)` resends it on the person's say-so with the same id, which the
  daemon's ledger answers with the existing admission when the first one got through (no second
  run); `discard(request_id)` drops it.
- A turn that is running when the transport goes: `wait` answers `OutcomeUnknown` at once; nothing
  is resubmitted. On reconnect the host re-subscribes every session it had open with its replay
  cursor (`session.subscribe {epoch, cursor}`): missed events are replayed once (duplicates by seq
  are dropped), open server requests are re-offered; a `session.replay_gap` is passed on so the
  surface reloads a snapshot.
- `RemoteHost` is the blocking facade for the REPL (the same method names as `InProcessHost`:
  `create`, `resume`, `submit`, `wait`, `interrupt`, `steer`, `redirect`, `answer_server_request`,
  `events`, `close_session`, `feed`, `call`, `run`), running the channel and the service on its own
  loop thread.

Inputs → outputs: `RemoteSessionService(channel, journal, *, surface=)`; `await start()`;
`create(params) → SessionCreateResult`; `resume(id) → SessionResumeResult`;
`submit(id, text) → PromptSubmitResult` (or `OutcomeUnknown` / `ProtocolError` / `NotSent`);
`wait(id, turn_id) → RemoteTurn {turn_id, generation, reason, failure}`; `interrupt(id, generation?,
hard=)`, `steer(id, text)`, `answer_server_request(id, request_id, answer)`, `events(id, since)`,
`subscribe(id, listener)`, `on_request(id, listener)`, `close(id)`, `aclose()`;
`journal.pending()`, `retry(request_id)`, `discard(request_id)`.

Files: `src/indus/service/remote/host.py`, `src/indus/service/remote/journal.py`,
`tests/unit/service/remote/test_host.py`. Shared: `src/indus/protocol/methods.py`
(`PromptSubmitParams.request_id`, optional), `packages/protocol/src/generated/*` (regenerated),
`src/indus/gateway/ws/methods.py` (a client request id is the admission request id under a stable
per-surface principal, so an exact retry over a new connection finds the first admission).

Edge cases / failure modes:
- reply to `session.create` lost → one session after the retry;
- socket killed after the daemon admitted the input but before the answer → `OutcomeUnknown`, the
  entry kept as `unknown`, exactly one admission in the ledger; an explicit retry returns it;
- socket killed mid-turn → `wait` fails `OutcomeUnknown`; after reconnect the missed deltas arrive
  exactly once and the turn's end is still delivered to listeners;
- interrupt / steer / answer without a known generation → the running turn's generation from the
  last `turn.started` (the daemon refuses a stale one with "nothing happened");
- journal: unreadable or foreign files are skipped; at most 256 entries are listed.

Our design differences / decisions:
- The reference returns a whole agent-result dict per turn; our surfaces render from events and
  only need the turn's end, so `wait` returns `RemoteTurn`.
- `redirect` is not served by the daemon yet (`-32601`); the call is passed through and the
  refusal raised as `ProtocolError`.

Tests we will write (`tests/unit/service/remote/test_host.py`, real `WsEndpoint` +
`DaemonSessionHost` over the fake model, in-loop ASGI transports with a fault switch):
- dropped `session.create` reply + retry → one session;
- killed socket after admission → outcome unknown, journal kept, one admission, explicit retry
  dedups;
- killed socket mid-turn → outcome unknown, reconnect replays missed deltas exactly once;
- an approval answered through the host names the offered generation;
- journal file modes, retire, unknown, listing; `RemoteHost` blocking facade round trip.

## Decisions for the lead
- `OutcomeUnknown` is a Python exception (code `runtime.outcome_unknown` text) until P17-25 adds
  the stable protocol code.
- Principal for client request ids: `attach:<surface>` (one OS user owns every attached client;
  ids are 128-bit random), so an explicit retry from a new connection is an exact retry.

Compare checklist (filled after the rewrite; reopened `gateway_agent.py`, `gateway_connection.py`):
- [x] idempotent create (test: `test_host.py::test_a_lost_create_reply_then_retry_makes_one_session`)
- [x] no automatic resubmit; journal kept as unknown; explicit retry is an exact retry (test: `test_host.py::test_a_socket_lost_after_admission_is_outcome_unknown`)
- [x] replay after reconnect exactly once (test: `test_host.py::test_a_reconnect_replays_missed_deltas_exactly_once`)
- [x] answers and controls name the generation (tests: `test_host.py::test_an_approval_is_answered_with_its_offered_generation`, `::test_controls_name_the_running_generation`)
- [x] a refusal is not a lost connection (test: `test_host.py::test_a_refusal_from_the_daemon_is_a_protocol_error_and_retires_the_entry`)
- [x] gap found: the submit's `turn_id` is the admission id while events carry the service's
  turn id, so `wait(turn_id)` matched nothing — the host now maps an admission to its turn by
  generation (started at once) or by the first later turn to start (queued), pinned by
  `test_host.py::test_an_accepted_submit_retires_its_entry` and the replay test.
- Deliberate differences: no agent-result dict (`RemoteTurn` instead; surfaces render events);
  no image staging (`image.attach_bytes`) or `/moa` override in the submit (P17-18 refuses
  `--image` in attach mode); the reference's settle timeout after an interrupt is the CLI's
  (P17-18, 10 s).
- Not taken: `session.mutate` (model / reasoning switches) and `prompt.receipt` with stored
  results — not served by the daemon yet (P17-25 declares `admission.receipt`).

## Interface for other slices (K7 CLI attach)

```python
from indus.service.remote.host import RemoteSessionService, RemoteHost, RemoteTurn, OutcomeUnknown
from indus.service.remote.journal import Journal
svc = await RemoteSessionService(channel, Journal(home), surface="cli").start()
await svc.create(params) / resume(id) / submit(id, text) -> PromptSubmitResult
await svc.wait(id, turn_id=None) -> RemoteTurn{session_id, turn_id, generation, reason, failure}
await svc.interrupt(id, generation=None, hard=False) / steer(id, text) / redirect(id, text)
await svc.answer_server_request(id, request_id, answer) / decline_server_request(id, request_id)
svc.events(id, since) / subscribe(id, cb) / on_request(id, cb) / on_gap(id, cb); await svc.close(id)
await svc.pending_submissions(id) / retry(request_id) / discard(request_id); await svc.aclose()
OutcomeUnknown(TransportLost): .session_id .request_id .turn_id .code = "runtime.outcome_unknown"
host = RemoteHost(async_build_returning_started_service).start()   # InProcessHost's blocking names
```
`RemoteHost.wait` returns `RemoteTurn` (not `TurnResult`).

## Follow-ups
- Compaction child ids: events under a new session id are followed only once a `resume` /
  `subscribe` answer names it (P17-25's `session.updated` / child-id event would close it).
- Admission → turn matching for queued inputs is by order until `admission.state` (P17-25)
  names the admission on the wire.

## Status

Done (2026-10-20). Shared: `src/indus/protocol/methods.py` (`PromptSubmitParams.request_id`),
`packages/protocol/src/generated/{schema.json,types.ts}` (regenerated with
`python -m indus.protocol.codegen`), `src/indus/gateway/ws/methods.py` (client request id →
admission request id under `attach:<surface>`), `docs/internals/traps-ledger.md` (R05.21,
R09.11), `docs/internals/acceptance-map.md` (ACC-K51-4).

## Integration (P17-15..22 integrator, 2026-10-20)

Added for the CLI attach (small, tested in `test_host.py`): `key_of(session_id)` (the daemon's
ledger key), `turn_of(session_id, turn_id)` (the turn and generation an admission became), and
`discard_unknown(session_id, admission_id, generation)` over the new daemon-only method
`admission.discard` (`protocol/attach.py`; `{discarded, reason: stale_generation | not_found}`;
another session's admission reads as not found; the lost turn is closed in its transcript).
