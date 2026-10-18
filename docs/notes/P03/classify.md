# Task: Error classifier (P03-15) Phase: 03 Owner: P03-B2 builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/error_classifier.py`
(`FailoverReason` 34, `ClassifiedError` 70, the verdict helpers and rule tables ~456–640, the
stage functions and `_STAGES` 673–1010, status handlers 1028–1100, `_classify_400` 1143, the
reset-window helpers 1242–1270 and the aggregator-upstream helpers 1487–1504). Test names of
`tests/agent/test_error_classifier.py` were not opened (the stage and handler code already names
every edge case we take). Closed before writing.
Evidence: none.

What it does (own words): every failed model call is turned into one verdict from a closed set of
reasons, plus the hints the retry loop and later the credential pool and fallback chain act on
(retry or not, rotate the key, switch provider, compress the context, how long to wait, when a
quota resets). Without it a 401 would be retried like a 503, an over-long prompt would burn the
retry budget, and a key sent to the wrong host would read as "bad key".

Inputs → outputs: `classify(err, *, route=None, profile=None, clock=None)` →
`ClassifiedError{reason, status, message, retryable, should_rotate_credential, should_fallback,
should_compress, retry_after, reset_at, host_hint, code}`. `err` is any exception: our
`ProviderHTTPError` / `ProviderStreamError` / `StaleBreakerTripped`, an httpx transport exception,
or anything else.

Reasons (closed set): auth, auth_permanent, billing, rate_limit, upstream_rate_limit,
upstream_blocked, overloaded, server_error, timeout, connection, ssl_cert, context_overflow,
payload_too_large, model_not_found, content_policy, format_error, role_alternation,
stream_unsupported, incomplete_response, unknown.

Pipeline (first verdict wins): profile hook `profile.classify_error(ctx)` → structured code / type
in the body (OpenAI `error.code`, Anthropic `error.type`, our stream-error kinds) → HTTP status
rules → message patterns (status-less errors) → transport exception types → `unknown`
(retryable).

Status rules: 401 auth (rotate); 402 billing unless a usage-limit message names a reset ("try
again in 20 minutes") → rate_limit with reset; 403 billing phrases → billing, CDN/WAF challenge
markers → upstream_blocked (fallback, key healthy), else auth; 404 model-missing wording →
model_not_found, else unknown with the real message; 408/504 timeout; 413 payload_too_large;
429 overload wording → overloaded, aggregator-wrapped upstream failure → upstream_rate_limit,
quota wording without a reset → billing, else rate_limit; 500/502 server_error, 503/529
overloaded, with request-validation wording → format_error and overflow wording →
context_overflow in any 5xx; other 4xx: role alternation, validation, overflow, model-missing,
content policy wording, else format_error.

Files: `src/indus/providers/classify.py`, `tests/unit/providers/test_classify.py`,
`tests/fixtures/errors/classify_cases.yaml`.

Edge cases / failure modes:
- body not JSON (HTML error page, plain text) → matched as text.
- `Retry-After` on any status is parsed (capped at 600 s, P03-16) into `retry_after`.
- the route key appearing in a server message (echoed `Authorization`) is masked before it is
  stored in `message` (foundation redactor + the exact key).
- a profile hook that raises or returns junk is ignored (logged at debug), the built-in pipeline
  decides.
- OpenAI's generic `invalid_request_error` type is never a verdict on its own (it is stamped on
  overflow 400s too).

Our design differences / decisions:
- Order follows the phase row: structured code before status (the reference checks status first
  and folds codes into the status handlers). A structured code is the most specific signal.
- `connection` is its own reason (the reference folds connect failures into `timeout`), and
  `ssl_cert` / `content_policy` / `stream_unsupported` use our names.
- `context_overflow` and `payload_too_large` are `retryable=False, should_compress=True`: our
  `retry_call` does not compress, so retrying the identical request is pointless; the turn loop
  (P04) compresses and calls again.
- Pattern tables are our own, written from the vendors' documented error bodies (OpenAI error
  codes such as `context_length_exceeded`, `insufficient_quota`, `rate_limit_exceeded`; Anthropic
  error types `overloaded_error`, `rate_limit_error`, `authentication_error`, `api_error`,
  `request_too_large`, `billing_error`, `timeout_error`; "prompt is too long").
- No portal/vendor-only rows (free-tier gateway refusals, Codex replay blobs, Bedrock IAM, xAI
  spend codes, MoA, llama.cpp grammar, image size): those reasons are not in our closed set; the
  provider profile hook is the place for vendor shapes (P08 adds them where needed).
- `reset_at` is an aware datetime only when a clock is passed; `retry_after` always carries the
  (capped) seconds.

Tests we will write: ≥ 60 table rows from `classify_cases.yaml` (status, headers, body, exception →
reason and flags), profile hook override and a raising hook, host hint only for a non-default
host, no message contains the fake key, every reason in the closed set reachable.

## Decisions for the lead
- `classify_error(ctx)` hook contract: `ctx` is our `ErrorContext` (error, status, body, headers,
  code, text, route); the hook may return `None`, a reason string, a mapping
  `{reason, retryable?, should_*?}` or a `ClassifiedError`. P03-01 defines the hook's default
  (returns `None`); any of these shapes is accepted.
- Host hint without a profile: given whenever the route has a host, since the default host is
  unknown; with a profile, only when the route host differs from the profile's host.

Compare checklist (filled after the rewrite; reopened the files above):
- [x] stage order profile → code → status → message → transport → unknown (tests: `test_classify.py::test_profile_hook_wins`, `::test_cases` rows `code-*`)
- [x] 401/402/403/404/408/413/429/5xx/529 rules (rows `s401-*` … `s529-*`)
- [x] 402 periodic usage limit → rate_limit with reset (row `s402-usage-resets`, `test_reset_at_with_clock`)
- [x] 403 WAF markers → upstream_blocked, billing words → billing (rows `s403-*`)
- [x] 429 overload / upstream wrapper / quota wall / plain (rows `s429-*`)
- [x] 5xx validation → format_error, overflow → context_overflow (rows `s5xx-*`)
- [x] transport types timeout / connection / ssl (rows `exc-*`)
- [x] auth host hint (tests: `test_host_hint_only_for_non_default_host`)
- [x] key never in message (test: `test_no_message_contains_the_key`)
- Not taken: a 403 carrying a structured "upstream unavailable" code → overloaded (one gateway's
  shape; a profile hook can add it).
- Not applicable (portal/vendor-only, listed above): free-tier gateway, Codex replay/entitlement,
  thinking-signature and long-context tier (P08 with the Anthropic cache work), image size/corrupt,
  multimodal tool content, reasoning-mandatory, llama.cpp grammar, MoA, Bedrock, xAI spend code,
  disconnect + large session → overflow heuristic (needs session size, P04/P08).

## Status

Done (2026-10-18).

## Integration (lead)

- Traps ledger R01.11: the "Review: spoof table, key-to-host scoping…" line was dropped; the P03-A
  tests in `test_hosts.py` / `test_route.py` now cover it alongside the two classifier tests.
