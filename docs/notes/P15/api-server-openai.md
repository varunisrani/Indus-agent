# Task: API server, OpenAI-compatible routes (P15-30) Phase: 15 Owner: wave-20 builder W5

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/platforms/api_server.py`
(constants block, middleware, `_check_auth`, the route table, health/models, content
normalisation helpers), `gateway/platforms/api_server_openai_routes.py` (message/input parsing,
chat-completions and responses handlers, the two SSE writers). Evidence: none.
Interop source: OpenAI's public OpenAPI document, `openai/openai-openapi` at commit
`33248a638234a53846ed3ab4b2a7c77069bc1030` (2026-10-19, MIT; OpenAPI 3.1.0, API 2.3.0). The schema
pieces our tests validate against are extracted from its `openapi.json` into
`tests/fixtures/gateway/openai_schema/openai_trimmed.json` (component closure only, descriptions
dropped; the extraction script lives outside the repo).

What it does (own words): the gateway daemon serves an HTTP API that OpenAI-compatible clients
(chat front ends, SDKs, scripts) can point at. Every request, including the health checks and
requests from the same machine, needs a bearer key that `indus gateway api-key create` made
(D-16.26). A chat completion or a response is one agent turn on a session of the
`indus-api-server` toolset (core minus clarify, TTS and computer use). Without an
`X-Indus-Session-Id` header each request gets a new session; with it the stored session is resumed
and the request's earlier messages are ignored (the store holds the history). Nobody watches these
turns, so the agent runs them `unattended`: a flagged command is denied on the spot instead of
waiting for an approval nobody can give (R02.27). Streaming answers are SSE: chat chunks with
`data: [DONE]` at the end, Responses events with a sequence number; an idle stream gets a
`: keepalive` comment every 10 s. Without the key every route answers 401; bodies over 10 MiB 413.

Inputs → outputs:
- `GET /health`, `GET /v1/health` → `{"status": "ok", "platform": "indus-agent", "version"}`.
- `GET /v1/models` → `{"object": "list", "data": [{"id": <model_name>, "object": "model",
  "created", "owned_by": "indus"}]}`.
- `POST /v1/chat/completions` `{messages, stream?, model?}` → `chat.completion` JSON or
  `chat.completion.chunk` SSE; header `X-Indus-Session-Id` on the answer.
- `POST /v1/responses` `{input, instructions?, previous_response_id?, store?, stream?}` →
  `response` JSON or Responses SSE; `GET /v1/responses/{id}` → the stored response (store=true).
- Config `gateway.api_server`: `enabled` (false), `host` 127.0.0.1, `port` 8642,
  `cors_origins` [] (none), `max_concurrent_runs` 10 (0 = no cap; shared with P15-31 runs),
  `model_name` "indus-agent", `idle_close_seconds` 900.
- Keys: `indus gateway api-key create [--label]` prints the key once; `list` shows id, label and
  creation time (never a value); `revoke ID`. Values live in the profile's `SecretStore` as
  `INDUS_API_KEY_<ID>`, labels in `<home>/gateway/api_keys.json` (no values).

Files: `src/indus/gateway/http/api_server.py`, `src/indus/gateway/http/openai_wire.py`,
`src/indus/gateway/http/turns.py` (new: one agent turn over the session service for every HTTP
surface), `src/indus/gateway/http/serving.py` (new: bind with retries, uvicorn task, shared
middleware), `src/indus/gateway/http/__init__.py` (new: `HttpHost`, `start_http_surfaces`),
`src/indus/state/api_keys.py` (new: the key ring, shared by the CLI and the server — D-16.246),
`src/indus/cli/commands/gateway_api_key.py`, `tests/contract/gateway/test_api_server_openai.py`,
`tests/contract/gateway/_http_kit.py`, `tests/fixtures/gateway/openai_schema/`,
`tests/unit/state/test_api_keys.py`, `tests/unit/cli/commands/test_gateway_api_key.py`,
`tests/unit/gateway/http/test_serving.py` (the P15-24 stand-in `_stubs_w5.py` was removed at
integration; `tests/integration/gateway/test_http_part.py` runs the surfaces in the real daemon). Shared: `foundation/config/schema.py` (+ test),
`pyproject.toml` (deps), `THIRD_PARTY_NOTICES.md`, `docs/plan/phases/00-layout-and-conventions.md`
§4 pin rows.

Edge cases / failure modes:
- no key at all configured → the server refuses to start (a log line names
  `indus gateway api-key create`); a request without `Authorization` → 401, a wrong key → 401;
  keys compared in constant time against every listed key (no early exit); a key revoked by the
  CLI while the daemon runs stops working on the next request (the index is read per request).
- body: `Content-Length` over the cap → 413 before reading; a chunked body is counted while read
  and cut at the cap → 413. Invalid JSON → 400 in the OpenAI error envelope.
- content: text parts capped at 64 KiB each; arrays over 1,000 items → 400; image data URLs over
  5 MB → 413; `file` / unknown part types → 400; an empty last user message → 400.
- session header: over 256 characters, control characters or characters outside
  `[A-Za-z0-9._:-]` → 400; an unknown id → 404; a session busy elsewhere → 409.
- more API turns than `max_concurrent_runs` → 429 (`rate_limit_error`).
- the client disconnects mid-stream → the turn is interrupted.
- a failed turn with no text → 502 (`server_error`); partial text → 200 with
  `finish_reason: "length"` or the error in the last chunk.
- an `Origin` header not in `cors_origins` → 403 (CORS is off by default; preflight refused).
- bind: `EADDRINUSE` retried 5 times, 1 s apart on the injected clock, then a clear error.

Our design differences / decisions:
- Auth on every route, the health checks included (lead brief: "bearer-key auth on every route";
  the reference leaves `/health` open).
- No key, no server: the reference falls back to open on the default profile when no key is set.
- One `SessionService` per HTTP surface, built by the daemon from a `SurfaceSpec` (toolsets,
  answerable request kinds, platform); turns are `unattended` through
  `agent.runtime.run_context`, which the P05 approval policy already denies instantly.
- No implicit session from a conversation fingerprint: no header → a new session (the row says
  so). For a new session the request's system messages and earlier messages are folded into the
  first user text as a labelled block (the session service takes text, not a history).
- Images: http(s) URLs are passed to the model as `[image: URL]` lines; data URLs (≤5 MB) are
  written once under `<home>/cache/api_server/images/<sha256>.<ext>` and referenced by path, so
  `vision_analyze` can open them. Nothing outside the profile is ever served.
- Responses: `previous_response_id` resumes that response's session; `conversation`,
  idempotency keys and replay of stored streams are not taken (later phase if wanted). Stored
  responses are kept in memory (bounded, 1,000), not durable.
- The "30 s heartbeat" is a status callback the daemon can hand in (`on_status`, request counters
  every 30 s on the injected clock); streams carry the 10 s keepalive comment.
- Errors use the OpenAI envelope `{"error": {"message", "type", "param", "code"}}`; error text is
  redacted before it leaves.
- Request size cap 10 MiB (10,485,760 bytes).

Tests we will write:
- every route answers 401 without a key (loopback transport), 401 with a wrong key, 200 with one;
  a revoked key stops working; nothing logs a key value.
- 10 MiB + 1 byte → 413 (length header and chunked); 64 KiB text part cap; 1,001 items → 400;
  5 MB + 1 data URL → 413.
- chat completion non-streaming and streaming against the fake model; every chunk validates against
  `CreateChatCompletionStreamResponse`, the JSON answer against `CreateChatCompletionResponse`,
  `/v1/models` against `ListModelsResponse`, every Responses event against its event schema.
- session header resumes (second turn sees the first in the model's messages); no header → new
  session; unknown id → 404; bad id → 400.
- a flagged command is denied instantly (fake clock unchanged, no server request sent).
- `clarify` is not in the tool list of an API session.
- 11th concurrent turn → 429; keepalive comment after 10 s of silence (fake clock).
- CORS: off by default (403 for a foreign origin), configured origin gets headers.

## Decisions for the lead
- Config lives under `gateway.api_server` (reference: `platforms.api_server.extra`).
- `indus gateway api-key` is a click group in `cli/commands/gateway_api_key.py`
  (`api_key_group`); P15-28's `gateway` group mounts it (`gateway_group.add_command(api_key_group)`
  or a lazy row `("api-key", "indus.cli.commands.gateway_api_key:api_key_group", …)`).
- The key ring lives in `indus.state.api_keys` (layer rule D-16.246).
- Dependencies: `fastapi==0.142.2` (2026-09-30, MIT) and `uvicorn==0.54.0` (2026-09-25,
  BSD-3-Clause, already locked through `mcp`) as core dependencies, as the phase file says
  ("fastapi/uvicorn already in base per stack"); new transitive: `annotated-doc==0.0.5` (MIT).
  Both are on or before `exclude-newer` 2026-10-04. uv.lock regenerated in the worktree only.

- The daemon wiring (for P15-24): `await start_http_surfaces(HttpHost(scope, services=factory,
  store=handle))` on the daemon's loop; `factory(spec: SurfaceSpec) -> SessionService` builds one
  service per surface from the spec (toolsets, `answerable_kinds`, platform; `untrusted` → no MCP
  or plugin toolsets); `await surfaces.aclose()` at shutdown before the services close;
  `HttpHost.on_status(name, data)` gets the API server's counters every 30 s for the runtime
  status file; `surfaces.problems` lists a surface that did not start (the daemon decides whether
  that is fatal). Integration (lead): `gateway.http.part.HttpPart` (BACKGROUND, only when a
  surface is enabled) does this with `daemon.surface_service(spec)` (`process.build_surface_service`:
  MCP for the API server only, never plugins; the webhook surface gets neither); a surface that
  cannot start is a `surfaces.<name>.problem` row of the status file and never fails the daemon;
  API-started cron runs are cancelled and awaited when the API server closes.

Compare checklist (filled after the rewrite; reopened `api_server.py` (constants, middleware,
auth, route table, health/models, content normalisation), `api_server_openai_routes.py`):
- [x] bearer key compared as bytes in constant time; wrong / missing → 401 (test:
  `test_api_server_openai.py::test_every_route_needs_a_key_on_loopback_too`)
- [x] 10 MB request cap by length and while reading (`::test_a_body_over_10_mib_is_refused_with_413`)
- [x] 64 KiB text parts, 1,000-item arrays, 5 MB image data URLs, file parts refused
  (`::test_content_limits`)
- [x] security headers on every answer, errors included; CORS only for listed origins, preflight
  answered (`::test_cors_is_off_by_default_and_follows_the_allow_list`)
- [x] stream flags coerced from strings (`"true"`) (`::test_streamed_chat_frames_match_the_chunk_schema`)
- [x] keepalive comment after 10 s idle (`::test_an_idle_stream_gets_a_keepalive_comment_every_10_s`)
- [x] a final text that arrived without deltas is sent once before the finish chunk
- [x] a disconnect interrupts the turn (`::test_a_client_that_goes_away_interrupts_its_turn`,
  `test_serving.py::test_a_streaming_client_that_hangs_up_interrupts_its_turn`)
- [x] session header length/charset checks, resume from the store (`::test_the_session_header_resumes_and_no_header_starts_fresh`)
- [x] concurrency cap → 429 `rate_limit_error` (`::test_more_turns_than_the_cap_get_429`)
- [x] bind retried on `EADDRINUSE` 5 × 1 s (`test_serving.py::test_bind_retries_a_busy_port_on_the_clock`)
- [x] flagged command denied at once in an unattended API turn (`::test_a_flagged_command_is_denied_at_once`)
- Gap found and fixed: a stream whose client left before the first frame never ran its generator's
  cleanup (gate slot and turn leaked) — `TurnStream` releases in the response itself.
- Deliberate differences: no implicit session from a conversation fingerprint (the row: no header →
  new session); auth on `/health` too; no key → no server; `finish_reason` stays in OpenAI's enum
  (failures carry an `indus` object instead of `"error"`); tool progress is not sent as custom SSE
  events in chat streams (strict OpenAI clients; runs carry tool events); the `X-…-Session-Key`
  memory-scope header, `Idempotency-Key` replay, `conversation`, `DELETE /v1/responses/{id}` and
  durable response storage are not taken.
- Not in P15 (no later row found; raise with the lead if wanted): `/health/detailed`,
  `/v1/capabilities`, `/api/model/options`, `/v1/skills`, `/v1/toolsets`, session create / patch /
  delete / messages / fork / chat / model-lock under `/api/sessions/{id}`, room grants,
  browser-control and artifact routes (K39 → P24), `/api/platforms/{p}/events` (P16 inbound
  listener kit).

## Status

Done (2026-10-19). Shared files touched: `foundation/config/schema.py` (+ test: `gateway.api_server`
rows), `pyproject.toml` (fastapi, uvicorn), `uv.lock` (regenerated in the worktree only),
`THIRD_PARTY_NOTICES.md`, `00-layout` §4 pin rows, `acceptance-map.md` (ACC-K35-1),
`traps-ledger.md` (R02.27). `indus gateway api-key` is ready to be mounted by P15-28.
