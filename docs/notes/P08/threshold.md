# Task: Threshold arithmetic (P08-02) Phase: 08 Owner: wave-10 builder A

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/context_compressor.py`
(constants block ~740–1170; `_effective_threshold_percent`, `_effective_input_window`,
`_compute_threshold_tokens`, `_apply_threshold_tokens_cap` ~2730–2800; `resolve_model_threshold`
~2039; `should_compress_info` ~3004; the aux-ceiling assignment in
`agent/conversation_compression.py` ~1905), `website/docs/developer-guide/context-compression-and-caching.md`
(§Configuration). Spec: research `01` §3.19 (trigger threshold), trap R01.7, D-16.24. Closed before
writing.
Evidence: none.

What it does (own words): decides how many prompt tokens a conversation may reach before it is
compressed. The usable input window is the model window minus the output reservation (the whole
window when no reservation is set). The percentage is a per-model override when one matches
(longest substring of the model id wins; `provider:substring` keys apply only to that provider and
beat a bare key of the same substring), else `compression.threshold` (0.50). Windows under
512,000 tokens never trigger below 75 % (the floor only raises). The trigger is the larger of
`window × percent` and 64,000; when the 64,000 minimum is what decided it, it is held to 85 % of
the window; a trigger at or above the window becomes 85 %. An absolute cap
`compression.threshold_tokens` lowers it, and so does a smaller summariser window (the summary
call must be able to read what it summarises), durably. The developer guide's "200K → 100K"
example ignores the 0.75 floor; the code gives 150K, and so do we (R01.7).

Inputs → outputs: `(window, max_output, percent, cap, aux_window)` → trigger tokens;
`(model, provider, overrides, default)` → percent; `CompressionSettings.from_scope(scope)` reads
`compression.*`.

Files: `src/indus/core/compression/threshold.py`, `tests/unit/core/compression/test_threshold.py`,
`src/indus/foundation/config/schema.py` (shared: the `compression` section).

Edge cases / failure modes:
- reservation ≥ window → whole window (never a negative window);
- unknown window (0) → no trigger arithmetic (caller treats as "never compress automatically");
- cap ≤ 0 or non-int → no cap; cap above the window → clamped to the window's 85 % rule result;
- an explicit percent above 0.85 is the user's choice and is not pulled down (only the
  64K-decided and ≥ window cases are);
- trigger is always ≥ 1 and < effective window.

Our design differences / decisions:
- Pure functions plus one frozen `CompressionSettings` dataclass; no compressor object.
- `max_attempts` clamped to 1…10; `timeout_s` floor 1.
- Config keys (ours): `compression.enabled`, `threshold`, `threshold_tokens`, `model_thresholds`,
  `max_attempts`, `timeout_s`, `tail_mode` (`lean`/`legacy`), `target_ratio`,
  `min_tail_user_messages`, `in_place`, `abort_on_summary_failure`, `idle_after_s` (0 = off).

Tests we will write:
- table: 32K, 64K, 128K, 200K, 400K, 1M windows × output reservations × overrides × caps;
- 200K default → 150K (R01.7); 1M default → 500K; 64K → 54,400;
- override resolution (longest key, provider scoping);
- seeded property: trigger < effective window and ≥ 1 for random inputs.

## Decisions for the lead
- The plan's hypothesis property runs as a seeded random loop (hypothesis is not in the dev
  group yet, wave-2 landing note).

Compare checklist (filled after the rewrite; reopened the threshold code of
`agent/context_compressor.py` and `agent/conversation_compression.py` (aux ceiling)):
- [x] Raise-only 0.75 floor below 512K (test: `tests/unit/core/compression/test_threshold.py::test_table`)
- [x] 64K minimum held to 85 % when it decides; ≥ window → 85 %; an explicit share above 0.85 kept
  (test: `test_threshold.py::test_table`)
- [x] Output reservation subtracted; whole window when unset or ≥ window (test: `test_threshold.py::test_effective_window`)
- [x] Absolute cap and a smaller summariser window lower the trigger, durably across route
  changes (tests: `test_threshold.py::test_table`, `test_engine.py::test_smaller_aux_window_lowers_the_trigger`)
- [x] Docs drift: 200K → 150K (test: `test_threshold.py::test_docs_example_follows_code`)
- [x] Longest-key override with provider scope (test: `test_threshold.py::test_model_override_resolution`)
- [x] Trigger always in [1, usable window) (test: `test_threshold.py::test_property_trigger_below_window`, seeded)
- Deliberate differences: none in the arithmetic. `trigger_tokens` is public for slice B's `@`
  references budget.
- Not taken: proactive prune trigger (`proactive_prune_tokens`), micro-compaction cadence.

## Status

Done (2026-10-18). Shared change: `compression` and `context` sections in `foundation/config/schema.py`.
