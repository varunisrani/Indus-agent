# Task: Send result, error classifier and retry policy (P15-04) Phase: 15 Owner: wave-18 builder G1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/platforms/base.py`
(`SendResult`, `classify_send_error`, `is_chat_level_not_found`, `_send_with_retry` and its helper
predicates). Research 06 §3.15, R06.3, R06.4. Evidence: none.

What it does (own words): every outbound platform call answers with a `SendResult`. A failure is
classified into one of seven kinds by an ordered rule table over the error text, the exception's own
class name and a few exception classes; the first rule that matches wins and anything unmatched is
`unknown`, which is never treated as harmless. Read/write timeouts mean the message may have landed,
so they are never retried and never re-sent as plain text; connection-level failures (refused,
reset, connect timeout, DNS) and explicit rate limits are retried. "Not found" only marks the whole
chat dead for a chat-level reading; a missing thread, topic or message keeps the chat alive, and the
sub-chat reading wins when both appear. The retry loop: up to 2 retries with backoff 2 s × 2^n plus
0–1 s jitter (injected RNG and clock); a server `retry_after` replaces one backoff; a wait longer
than the adapter's inline cap (60 s default, Telegram 5 s) is not slept: the typed flood failure is
returned with the wait so the ledger owns it; the classification is refreshed after every attempt;
retries that run out on a non-rate-limit failure send one "delivery failed" notice, retries that run
out on a rate limit send nothing (a notice would land inside the same penalty); a formatting failure
falls back to plain text once; a split payload that partly landed is never re-sent whole — only the
remainder the adapter proves undelivered is sent.

Inputs → outputs: exception and/or error text → `ErrorKind`, chat-level flag, ambiguity flag;
an attempt function + content → final `SendResult` (with all delivered ids).

Files: `src/indus/gateway/core/send.py`, `tests/unit/gateway/core/test_send.py`.

## API

`indus.gateway.core.send`:
- `class ErrorKind(StrEnum)`: `TOO_LONG, BAD_FORMAT, FORBIDDEN, NOT_FOUND, RATE_LIMITED, TRANSIENT, UNKNOWN`.
- `@dataclass(frozen=True) class SendResult`: `ok: bool`, `message_id: str | None`, `error: str | None`,
  `retryable: bool = False`, `retry_after: float | None`, `continuation_ids: tuple[str, ...] = ()`
  (earlier chunk ids in send order; `message_id` is the last), `kind: ErrorKind | None`,
  `chat_level: bool = False` (not_found means the whole chat), `maybe_delivered: bool = False`
  (timeout: the platform may have it), `partial: bool = False` (part of a split payload is visible),
  `remainder: str | None` (text the adapter proves undelivered; only with `partial`),
  `unsupported: bool = False` (clean refusal of an optional operation), `skipped: bool = False`
  (interim edit dropped by the send slot).
  Constructors: `SendResult.sent(message_id, continuation_ids=())`,
  `SendResult.failure(error=None, *, exc=None, retry_after=None, partial=False, remainder=None,
  delivered_ids=())`, `SendResult.refused(what)`, `SendResult.skip()`.
  Properties: `ids` (all delivered ids in order), `chat_unreachable` (forbidden, or chat-level
  not_found), `is_flood` (rate limited with a server wait).
- `classify_send_error(exc=None, text="") -> ErrorKind`; `is_chat_level_not_found(exc=None, text="")`;
  `is_ambiguous_timeout(exc=None, text="")`.
- `@dataclass(frozen=True) class RetryPolicy(max_retries=2, base_delay=2.0, jitter=1.0,
  inline_wait_cap=60.0)`.
- `async send_with_retry(attempt, content, *, clock, policy=RetryPolicy(), rng=random.random,
  notice=DELIVERY_FAILED_NOTICE, is_final=None) -> SendResult` where
  `attempt(text: str, plain: bool) -> Awaitable[SendResult]`. Exceptions from `attempt` become
  classified failures. The notice is sent with `attempt(notice, True)`.
- `DELIVERY_FAILED_NOTICE` (our copy).

Edge cases / failure modes:
- error text and class both empty → `unknown`, not retryable.
- "Bad Request: message thread not found" + "chat not found" both present → sub-chat wins.
- `RetryAfter(97*60)` first answer → returned at once, no sleep, no notice.
- server wait 4.5 s with Telegram cap 5 s → slept, jitter clamped so the sleep never passes the cap.
- read timeout on the first attempt → exactly one attempt, no plain fallback.
- transient then bad-format → loop breaks, plain fallback once.
- transient then partial with a remainder → only the remainder is sent; ids of both parts returned.
- partial without a remainder → returned as is.
- `attempt` raising → classified like a returned failure.
- forbidden / not_found / too_long → returned without a plain re-send (plain text cannot fix them).

Our design differences / decisions:
- Plain-text fallback only for `bad_format` and `unknown` (non-ambiguous) failures; the reference
  re-sends plain text after any non-network failure, which doubles requests into a dead chat.
- Ambiguous timeouts are checked before the transient rule, so a wrapper text such as
  "NetworkError: ReadTimeout" is still not retried (stricter R06.4).
- The jitter is clamped so no inline sleep exceeds the inline cap (the reference compares only the
  server wait with the cap).
- `send_with_retry` is a free function over an `attempt` callable (testable without an adapter);
  `Adapter.send_with_retry` wires it to the adapter (P15-05).
- Our own needle lists (vendor error strings are interop facts: Telegram, Discord, Slack readings).

Tests we will write:
- classifier table (30+ rows) incl. both-readings, exception classes, empty input.
- fake clock: backoff sequence 2+j, 4+j; server wait replaces one backoff; wait over cap → no sleep
  and typed flood failure; jitter clamp; no sleep ever above cap.
- read timeout attempted once; transient → exhausted → one notice; rate limit exhausted → no notice;
  flood refusal → no notice; bad format → plain once; reclassification mid-loop; partial resume;
  partial without remainder not re-sent; raising attempt.

## Decisions for the lead
- Plain fallback restricted to `bad_format`/`unknown` (see above).
- Notice copy: "Sorry, I could not deliver my last reply because of a connection problem. Ask again
  if you still need it." (ours).
- HTTP 5xx readings are not retried (may have been processed); they classify `unknown`.

Compare checklist (filled after the rewrite; reopened `gateway/platforms/base.py` send parts):
- [x] classifier table, 45 rows incl. both readings, exception classes, empty input (`tests/unit/gateway/core/test_send.py::test_classifier_table`, `::test_table_has_thirty_rows_and_every_kind`)
- [x] every reference reading covered: too long, parse/entity errors, blocked/deactivated/rights/not a member, chat/thread/topic/message not found, flood/too many requests/retry after/rate limit, connect-level patterns, read/write timeouts not retried
- [x] backoff 2+j, 4+j; server wait replaces one backoff (`::test_transient_backoff_then_success`, `::test_server_wait_replaces_one_backoff`)
- [x] wait over the cap: typed flood failure, no sleep, no notice (`::test_long_flood_wait_is_handed_over_without_sleeping_or_notice`, `::test_telegram_cap_refuses_a_six_second_wait`, `::test_flood_appearing_mid_loop_is_handed_over`)
- [x] no inline sleep above the cap (`::test_no_inline_sleep_ever_exceeds_the_cap`)
- [x] read timeout attempted once (`::test_read_timeout_is_attempted_exactly_once`, `::test_timeout_after_a_retry_stops_everything`)
- [x] exhausted transient → one notice; exhausted rate limit → nothing (`::test_transient_exhausted_sends_one_notice`, `::test_rate_limit_exhausted_sends_nothing_more`)
- [x] reclassification per attempt; plain fallback once (`::test_reclassification_after_each_attempt`, `::test_bad_format_falls_back_to_plain_text_once`)
- [x] partial split: remainder only, never whole (`::test_partial_split_resumes_only_the_remainder`, `::test_failed_remainder_keeps_its_proof_for_the_next_retry`, `::test_partial_without_remainder_is_never_resent_whole`, contract `no-whole-resend`)
- Deliberate differences: plain fallback only for `bad_format`/`unknown`; ambiguity checked before the connect rule; jitter clamped under the cap; no bare `network` needle (too broad: it would retry wrapped read errors) — `networkerror` as a class/text reading instead; partial delivery is explicit (`partial` + `remainder`) instead of a flag inside a raw response.
- Not taken: the reference's plain-text fallback banner prefix (the sender, P15-17, decides the plain wording); per-platform `is_final` refusals beyond the hook.

## Status

Done (2026-10-19). `src/indus/gateway/core/send.py`, `tests/unit/gateway/core/test_send.py`. The delivery ledger (P15-19) reads `is_flood` / `retry_after`; dead targets (P15-17) read `chat_unreachable`.
