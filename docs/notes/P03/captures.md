# Task: Recorded captures and replay (P03-11) Phase: 03 Owner: P03-B1 builder

Read: none (our own format, per the task row).
Evidence: none.

What it does (own words): a capture is one real request/response exchange saved as JSON so CI can
replay a vendor's exact bytes, in the exact chunking, through our transports without a network.
`ReplayTransport(capture)` is an `httpx.MockTransport` that checks the outgoing request equals the
recorded one (method, path, JSON body; a readable diff on mismatch) and serves the recorded chunks.
`scripts/dev/record_capture.py` records new captures with a real key (developer only).

Inputs → outputs: capture file → MockTransport; recorder CLI (`--provider --model --scenario`) →
`tests/fixtures/captures/<api_mode>/<scenario>.json`.

Format: `{meta: {recorded_at, provider, model, api_mode, scenario, doc_url, doc_date,
indus_version, synthetic, chat_request}, request: {method, url, headers, body}, response: {status,
headers, chunks: [base64]}}`. `meta.chat_request` is the provider-neutral request the scenario
sends, so a test can rebuild it and the transport must produce `request.body` from it.

Files: `scripts/dev/record_capture.py`, `tests/fakes/model/replay.py`,
`tests/fixtures/captures/{chat_completions,anthropic_messages}/{text,tool_call}.json`,
`tests/unit/providers/test_replay.py`.

Edge cases / failure modes:
- request headers kept only from the allow-list (`content-type`, `anthropic-version`); response
  headers only `content-type`; so keys, cookies, org and request ids never reach the file.
- the whole file text is run through the redactor; then every secret the scope knows (store values,
  credential-like env values, the route key) is searched for in the file text *and* in the decoded
  chunks; any hit refuses the save (`CaptureRefused`), naming no secret.
- the URL keeps scheme, host and path only (no query).
- replay ignores volatile body fields (`metadata`, `user`).

Our design differences / decisions:
- No real key was available to this builder, so the four captures are synthetic:
  `meta.synthetic: true`, bytes written by hand from the vendors' documented examples (OpenAI
  OpenAPI description; Anthropic streaming guide), split across chunk boundaries on purpose.
  Review line in the traps/acceptance ledgers until the lead records real ones.

Tests we will write: recorder with a fake upstream (key in a response header dropped; key echoed in
the body refuses); replay serves identical bytes in the same chunks; changed body fails with a
diff; every capture file validates and contains no credential shape.

Compare checklist: none (our own format; nothing read).

## Decisions for the lead
- The recorder's `_record` imports `indus.providers.route.resolve` and
  `indus.providers.http.client_for` (P03-A); it cannot run in this worktree until that slice lands.
- Record the four real captures: `uv run python scripts/dev/record_capture.py --provider openrouter
  --model <id> --scenario text` (and `tool_call`; then `--provider anthropic`).

## Status

Done with synthetic captures (2026-10-18). Review: real captures to be recorded by the lead.

## Integration (lead)

- `test_replay.py` builds the real `RuntimeRoute` instead of `StubRoute`.
- `scripts/dev/record_capture.py`: the guessed `client_for(route, timeouts=None)` used as a context
  manager is now `client_for(route, timeouts=timeouts_for(route, scope))` with `close_all()` in a
  `finally` (the client is pooled); the `type: ignore[import-not-found]` comments were dropped.
