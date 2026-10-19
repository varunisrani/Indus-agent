# Task: API server runs, sessions and jobs (P15-31) Phase: 15 Owner: wave-20 builder W5

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/platforms/api_server_runs.py`
(run stream backlog, status table, admission, execution, events, approval, steer, stop,
resolve-unknown, the sweep and its TTLs), `gateway/platforms/api_server.py` (`/api/*` route
registration, the jobs handlers and their limits, `/api/cron/fire`, sessions list/get). Evidence:
none.

What it does (own words): a client that wants to watch and steer an agent turn starts a *run*
instead of a chat completion. `POST /v1/runs` answers 202 with a run id at once; the turn runs in
the daemon. `GET /v1/runs/{id}/events` streams what happens (text deltas, tool start/finish,
approval and clarify requests, the end) as SSE with sequence numbers, so a client can reconnect
with `Last-Event-ID` and get what it missed. Unlike chat completions a run is *attended*: when the
agent wants approval the run waits (`waiting_for_approval`) and the client answers with
`POST /v1/runs/{id}/approval`; `/clarify` answers a question, `/steer` adds guidance to the
running turn, `/stop` interrupts it. `GET /v1/runs/{id}` gives the pollable status. Runs belong to
the key that started them; another key gets 404. At most `max_concurrent_runs` API turns (default
10, shared with chat completions and responses) run at once; the next gets 429. A run's event
buffer is dropped 300 s after the run started unless someone is listening; its terminal status is
kept for 3,600 s after it ended. Under `/api` the same key reaches stored sessions (list, get) and
the P14 cron jobs (list, create, get, update, delete, pause, resume, run) plus `/api/cron/fire`.

Inputs → outputs:
- `POST /v1/runs` `{input, session_id?, instructions?}` (+ optional `X-Indus-Session-Id`) → 202
  `{"run_id", "status": "started", "session_id"}`.
- `GET /v1/runs/{id}` → `{"object": "indus.run", "run_id", "status", "session_id",
  "generation", "created_at", "updated_at", "last_event", "output"?, "error"?, "approval"?}`;
  statuses `queued`, `running`, `waiting_for_approval`, `waiting_for_input`, `stopping`,
  `completed`, `failed`, `cancelled`, `unknown`, `resolved`.
- `GET /v1/runs/{id}/events` → SSE, `id: <seq>` + `data: {"event", "run_id", "seq", ...}`;
  events `run.started`, `message.delta`, `reasoning.delta`, `tool.started`, `tool.completed`,
  `approval.request`, `approval.responded`, `clarify.request`, `clarify.responded`,
  `run.steered`, `run.stopping`, `run.completed` / `run.failed` / `run.cancelled` /
  `run.unknown`, `run.resolved`; a truncated replay starts with `replay.truncated`.
- `POST /v1/runs/{id}/approval` `{choice: once|session|always|deny, request_id?, reason?}`;
  `/clarify` `{answers: {question_id: [..]}, request_id?}`; `/steer` `{input}`; `/stop`;
  `/resolve-unknown` `{generation}`.
- `GET /api/sessions?limit&offset&query` → `{"object": "list", "data": [session…]}`;
  `GET /api/sessions/{id}` → the session plus its async completions.
- `GET|POST /api/jobs`, `GET|PATCH|DELETE /api/jobs/{id}`, `POST /api/jobs/{id}/pause|resume|run`,
  `POST /api/cron/fire {job_id}` → 202.

Files: `src/indus/gateway/http/runs.py`, `src/indus/gateway/http/admin_routes.py`,
`tests/contract/gateway/test_api_server_runs.py`.

Edge cases / failure modes:
- subscribe before the first event: the stream opens with a comment so the client sees headers.
- a slow subscriber (its queue full) is dropped with a closing comment; others go on.
- `Last-Event-ID` older than the backlog (1,000 events) → `replay.truncated` first.
- `/approval` with nothing pending → 409; a bad choice → 400; `request_id` naming another request
  → 409; answering an approval settles it once (a second answer → 409).
- `/steer` when the run is not running → 409; `/stop` on a finished run returns its status.
- `/resolve-unknown`: only a run in status `unknown` (its turn's outcome could not be read) and only
  with its exact `generation` → `resolved`; anything else 409.
- jobs: id must be 12 lowercase hex → 400; name over 200 characters or prompt over 5,000 → 422
  (body validation); unknown id → 404; a bad schedule → 400 with the store's message; only
  `name`, `schedule`, `prompt`, `deliver`, `skills`, `repeat` are accepted (no scripts,
  workdirs or models from HTTP).
- cron store unavailable → 503.

Our design differences / decisions:
- Runs live in memory (one process); no idempotency keys, no durable run records (the reference
  keeps an idempotency store). A daemon restart forgets runs; boot recovery (P15-26) handles the
  turn itself.
- "Generation checked" for `/resolve-unknown` reads our run generation (the session's turn
  generation from `submit`); a run becomes `unknown` when the service cannot report its turn's
  result (the wait raised instead of returning).
- `/api/cron/fire` is guarded by the API key like every other route (the reference takes a
  remote scheduler's signed token instead; that remote scheduler is not part of this product).
- Wakes: the reference self-POSTs its chat endpoint to wake a stateless API session when
  background work finishes. Our sessions stay attached in the daemon's session service (closed
  after `idle_close_seconds` with nothing running), and the service starts the follow-up turn
  itself (`deliver_internal`), so no self-POST exists. Each such turn's final text is written
  as a durable row for pollers (`<home>/gateway/api_completions.jsonl`, newest 1,000 kept) and
  shown by `GET /api/sessions/{id}`. P15-19's delivery ledger can replace this file later.
- Clarify answers go through the session's server-request broker, so the answer is checked
  against the question (unknown question id → 400).

Tests we will write:
- a run that asks for approval: the event stream shows `approval.request`, the status says
  `waiting_for_approval`, `/approval once` settles it, the stream ends with `run.completed`;
  a second `/approval` → 409; a deny reaches the model as denied.
- 11 concurrent runs: the 11th → 429; after one ends a new one is accepted.
- terminal status still there after 3,599 s, gone after 3,601 s (fake clock); the event buffer is
  swept after 300 s.
- reconnect with `Last-Event-ID` replays only later events.
- steer and stop; resolve-unknown accepted only with the right generation.
- another key gets 404 for a run.
- jobs: create (201 chars → 422), list, get, update, pause, resume, delete, bad id → 400, run →
  202 through a recording cron backend; `/api/cron/fire` → 202.
- sessions: list and get read the store.

## Decisions for the lead
- The completions file is a stand-in for P15-19's ledger rows (`CompletionLog` port in
  `turns.py`); swap it when the ledger lands.

Compare checklist (filled after the rewrite; reopened `api_server_runs.py`, the `/api/*` part of
`api_server.py`):
- [x] 202 at once with the run id; status `queued`/`running`/`waiting_for_approval`/terminal
  (`test_api_server_runs.py::test_a_run_waits_for_approval_and_the_approval_route_settles_it`)
- [x] backlog 1,000 events, subscriber queue 256, slow subscriber dropped, `replay.truncated`,
  `Last-Event-ID` resume, an opening comment so headers flush (`::test_last_event_id_replays_only_later_events`)
- [x] approval choice aliases (`approve`/`allow` → once), bare `yes` refused, 409 with nothing
  pending, a request id naming another request refused (`::test_a_run_waits_for_approval…`)
- [x] steer refused unless running; stop marks `stopping` and hard-interrupts; stop of a finished
  run returns its status (`::test_steer_and_stop`)
- [x] stream TTL 300 s unless subscribed, terminal status 3,600 s, sweep every 60 s
  (`::test_terminal_status_is_kept_an_hour_and_events_five_minutes`)
- [x] concurrency cap shared with chat/responses (`::test_the_eleventh_concurrent_run_gets_429`)
- [x] runs scoped to the key that started them (`::test_a_run_belongs_to_the_key_that_started_it`)
- [x] jobs: 12-hex ids, name ≤200, prompt ≤5,000, field whitelist, 404/400 mapping, run in the
  background (`::test_jobs_crud_runs_and_limits`)
- [x] resolve-unknown checks the generation (`::test_resolve_unknown_needs_the_runs_generation`)
- Deliberate differences: in-memory runs (no idempotency store, no durable run status, no owner
  pid/start-time records); validation failures are 422 (the reference answers 400); `/api/cron/fire`
  is API-key guarded (no remote scheduler token); no wake self-POST (the service starts follow-up
  turns itself; their text is a durable row for pollers); `message.interim`, `reasoning.available`
  and `subagent.*` events are not emitted (`reasoning.delta` instead); a `/clarify` answer goes
  through the session broker.
- Not taken: room grants and their permissions, live bot-chat owner hand-off, session authority
  admission (P17 makes the daemon the shared backend).

## Status

Done (2026-10-19). New files only (plus the shared rows listed in `api-server-openai.md`).
