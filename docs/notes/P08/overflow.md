# Task: Overflow recovery (P08-06) Phase: 08 Owner: wave-10 builder A

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/turn_overflow.py` (whole
file), `agent/model_metadata.py` (context-limit error parsers ~1216–1400:
`parse_context_limit_from_error`, `get_context_length_from_provider_error`,
`parse_available_output_tokens_from_error`, `is_output_cap_error`, the probe tier list).
Spec: research `01` §3.19 (overflow recovery), trap R01.8. Closed before writing.
Evidence: none.

What it does (own words): when the provider says a request does not fit, the turn gets one
bounded recovery instead of an error. The P03 classifier gives the reason; this module decides:

- **payload too large (413)**: compress and retry; progress is scored in serialised bytes (the
  token estimate ignores image bytes), a shrink of at least 5 % counts;
- **output cap too large** ("max_tokens too large": the input fits, input + output does not):
  clamp the output cap from the error's stated available tokens (minus a 64-token margin, and
  never above `window − estimated input`), compress as well, retry; an output-cap error that states
  no number fails at once (compression cannot fix it);
- **prompt too long**: adopt the provider-stated window only when the error states one smaller
  than ours (and remember it as provider-confirmed); for an unknown model (window from the
  fallback) with no stated limit, step down the probe tiers 256K, 128K, 64K, 32K, 16K, 8K; then
  compress and retry;
- a provider-proven overflow bypasses the compression cooldown for **one** bounded attempt and
  never clears it (R01.8);
- out of attempts, nothing to compress, or no progress → a typed terminal result
  (`compression_exhausted`, reason, retryable flag, user text).

Inputs → outputs: `ClassifiedError` + current window/cap + sizes → `OverflowPlan{action: retry |
fail | not_overflow, output_cap?, new_window?, bypass_cooldown, failure?}`; the loop carries out
the plan with the engine.

Files: `src/indus/core/overflow.py`, `tests/unit/core/test_overflow.py`; shared update
`src/indus/core/loop.py` (catch the overflow verdict before ending the turn).

Edge cases / failure modes:
- a stated limit not lower than ours → ignored (never raise the window from an error);
- limit parse sanity band 1,024…10,000,000;
- clamp result < 1 → 1;
- overflow inside a stream after text was shown stays P03's terminal stub (R01.6), not here;
- an overflow on the grace (toolless) request → no recovery (turn already ending).

Our design differences / decisions:
- Parsers are ours, in `overflow.py`, reusing P03's `context_length.parse_overflow_limit` for the
  stated-window case and adding output-cap parsers for the documented vendor wordings (OpenAI
  "maximum context length … in the completion", Anthropic `max_tokens: N > M`, "available_tokens",
  "supports at most N completion tokens", "limited to N").
- Probe tiers only for unknown models (the reference dropped probing for known windows).
- No local-server "unexplained rejection" branch (local server slots are P21 territory) — noted.

Tests we will write:
- each error shape from a fake model / error bodies; clamp arithmetic; tier probe sequence;
- cooldown bypass once and not cleared; terminal result carries reason + retryable;
- loop: a 400 "prompt is too long" → compress → retry → answer.

Compare checklist (filled after the rewrite; reopened `agent/turn_overflow.py` and
the parsers in `agent/model_metadata.py`):
- [x] 413 scored in bytes, no progress → terminal (test: `tests/unit/core/test_overflow.py::test_payload_too_large_without_progress_is_terminal`)
- [x] Output-cap clamp from the stated budget, margin 64 (tests: `test_overflow.py::test_output_cap_clamp_arithmetic`,
  `test_overflow.py::test_loop_output_cap_clamps_the_retry`, `test_overflow.py::test_output_budget_parsing`)
- [x] Unparseable output-cap error fails fast (test: `test_overflow.py::test_output_cap_without_a_number_fails_fast`)
- [x] Gap found: some vendors' output-cap refusal classifies as `format_error` in P03 — the
  stated budget now marks it an overflow (test: `test_overflow.py::test_a_validation_error_stating_an_output_budget_is_an_overflow`)
- [x] Stated window adopted only when lower, remembered as provider-confirmed (tests:
  `test_overflow.py::test_prompt_too_long_adopts_a_stated_lower_limit`, `test_overflow.py::test_loop_recovers_from_prompt_too_long`)
- [x] Probe tiers for unknown-window models (test: `test_overflow.py::test_tier_probe_sequence`)
- [x] Cooldown bypassed once, not cleared (test: `test_overflow.py::test_bypass_is_one_bounded_attempt`)
- [x] Terminal result typed (test: `test_overflow.py::test_terminal_result_carries_reason_and_retryable`)
- Deliberate differences: no GitHub-Models hint, no MiniMax special case, no vision-strip retry;
  probe tiers only when the window was the fallback guess (the reference keeps a known window).
- Not taken: local-server "unexplained rejection" branch (P21).

## Status

Done (2026-10-18). Shared change: `core/loop.py` returns an overflow verdict to `recover_overflow`.

## Integration (lead)

Merged in the P08 wave 1 integration (2026-10-18) unchanged. The optional "output budget wording"
mapping in P03's classifier was not added: `overflow.is_overflow` already reads the stated budget
from the message, so the loop recovers either way. R01.10 stays with P08-13.
