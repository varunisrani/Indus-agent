# Task: Retry and backoff (P03-16) Phase: 03 Owner: P03-B2 builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/retry_utils.py` (all),
`agent/turn_recovery.py` (`compute_error_backoff` 1401–1450 only). Test files
`tests/agent/test_retry_utils.py` / `test_retry_delay_parsers_shared.py` not opened (the parser
module itself lists the phrasings). Closed before writing.
Evidence: none.

What it does (own words): when a model call fails with something worth retrying, wait and try
again — as long as the server asked for, when it named a time, else a jittered exponential wait so
many sessions hitting the same limit do not retry in lock step. Waits are slept on the injected
clock in 200 ms slices so an interrupt is honoured at once. Retry chatter stays quiet when a
later attempt succeeds; it is handed over only when every attempt failed, except that a wait over
a minute is announced as it starts (a person notices that). The final verdict travels on
`RetryFailed` so the credential pool and fallback chain (P08) can act on it.

Inputs → outputs:
- `parse_retry_after(value, *, now=None, cap=600)` → seconds or None. `value`: header mapping
  (`retry-after-ms`, `retry-after`), header value (seconds or HTTP date), number, JSON mapping or
  text (`retry_after`, `retry_delay`, `retryDelay` "20s", `resets_in_seconds`, `retry_after_ms`,
  nested under `error`, `details[]`, `metadata`), or free text ("try again in 20s", "resets in 1h
  5m", "retry after 3 seconds", "available again in 90s", "wait 5 minutes").
- `backoff(attempt, *, rng)` = min(2 · 2^(attempt−1), 60) + U(0, 0.5 · that).
- `await retry_call(fn, *, max_retries, clock, on_wait, classify, rng)` → `fn`'s result, or
  `RetryFailed{classified, error, attempts, waits}`.
- `clamp_max_retries(agent.api_max_retries)` → integer ≥ 1 (default 3).

Files: `src/indus/providers/retry.py`, `tests/unit/providers/test_retry.py`.

Edge cases / failure modes:
- server wait above 600 s → 600 (a pathological value must not park a session for hours).
- HTTP date in the past → 0; date measured from the injected clock's `now`.
- negative / NaN / empty values → None.
- non-retryable verdict (auth_permanent, format_error, content_policy, model_not_found, …) →
  `RetryFailed` after one call, nothing buffered is lost (handed to `on_wait`).
- exceptions that are not provider/transport failures (a bug: `TypeError`, an assertion) are
  not retried or wrapped; they propagate as they are.
- `on_wait` raising never breaks the loop.
- cancellation while waiting propagates within one slice (≤ 200 ms of clock time).

Our design differences / decisions:
- one parser for headers, bodies and text instead of separate helpers; the reference tries an
  explicit "retry after" before "resets in", we take the first lead-in in reading order (they
  rarely both appear; the shorter explicit wait is usually first in the sentence).
- seeded `random.Random` passed in instead of a time-and-counter seed: tests are deterministic,
  callers pass nothing and get an OS-seeded generator.
- vendor-specific adaptive schedules (one coding plan's 30/60/90/120 s overload ladder) and the
  free-tier announce rules are not taken; P08 can add a profile hook if needed.
- `max_retries` counts retries, so the default 3 means up to 4 calls.

Tests we will write: parser table (≥ 15 phrasings incl. HTTP date relative to the fake clock and
9,999 s → 600), seeded backoff bounds, the retry loop on a fake clock (3 failures then success → 4
calls with the computed waits, `Retry-After: 7` used instead of backoff, non-retryable → 1 call,
a 120 s wait announced immediately, buffered waits flushed only on failure, cancellation during a
wait exits within one slice), `clamp_max_retries`.

## Decisions for the lead
- `RetryFailed` reuses the `provider.error` code (no new code); it carries the classified verdict.
- Buffered chatter is delivered through the same `on_wait` callback (each `RetryWait` says whether
  it was announced live), so callers need one hook, not two.

Compare checklist (filled after the rewrite; reopened the two files above):
- [x] numeric / HTTP-date / headers in both casings (test: `test_retry.py::test_parse_table`)
- [x] free-text grammars: retry after N s, resets in Xh Ym, quota delay fields (rows `text-*`, `json-*`)
- [x] 600 s cap (row `cap-9999`)
- [x] backoff formula min(2·2^(n−1), 60) + U(0, 0.5·d) (test: `test_backoff_bounds_and_seeded_sequence`)
- [x] Retry-After wins for any retryable error (test: `test_retry_after_used_instead_of_backoff`)
- [x] 200 ms slices, interrupt honoured (test: `test_cancel_during_wait_exits_within_one_slice`)
- [x] chatter buffered, long waits announced (tests: `test_long_wait_announced_immediately`, `test_buffered_waits_dropped_on_success`)
- Deliberate differences: seeded rng parameter; one parser; `max_retries` counts retries.
- Not taken: the coding-plan overload ladder, free-tier wait copy, `format_reset_window` copy
  helper (terminal copy is P04/P08).

## Status

Done (2026-10-18).

## Integration (lead)

- `parse_retry_after` is now also what `transports.http_error_from_response` uses (the transports'
  private `_retry_after_seconds` was removed).
