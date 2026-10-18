# Task: Token accounting — the usage anchor (P08-01) Phase: 08 Owner: wave-10 builder A

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/usage_anchor.py` (whole
file), `agent/context_engine.py` (the engine interface and its lifecycle docstrings),
`agent/image_token_cost.py` (the residual idea and the plausibility band),
`agent/model_metadata.py` (`estimate_messages_tokens_rough` and the wire-shadow helpers around
it, ~2349–2620). Spec: research `01-agent-core.md` §3.19 (token accounting), trap R01.8;
`03-state.md` §3.10. Closed before writing.
Evidence: none.

What it does (own words): the context pressure of a session is measured from what the provider
last *billed*, not from a guess. After every response that carries usage, the anchor remembers
its prompt + completion tokens and which rows that request priced (a row count plus content
fingerprints of the last priced row and of the whole priced prefix). Later, the size of the
conversation is `anchor + rough estimate of the rows appended since` (4 characters per token;
opaque encrypted blobs count zero; images cost a learned per-image amount). When the rows the
anchor priced changed (an edit, a rewind, a compaction, a reload that differs), the fingerprints
no longer match and the anchor is ignored. Without an anchor only a rough whole-transcript
estimate exists, and an estimate is not evidence: an over-threshold estimate makes the gate
answer **wait** (send one request, let the provider count), never **compress** — unless the
provider has shown it never reports usage, or the last real reading was already over the line.
The anchor is persisted on the session row (`model_config.usage_anchor`), so a restarted
process keeps it while the stored transcript still matches; compaction or reset clears it.

Inputs → outputs: provider `Usage` + the messages that were sent → `UsageAnchor` (JSON-safe);
messages (+ anchor) → `Measure{tokens, anchored}`; `(measure, threshold)` → `"ok" | "wait" |
"compress"` (`TokenMeter.verdict`). Image residual: `(real prompt tokens, anchor, new rows)` →
learned per-image cost for `model@host`.

Files: `src/indus/core/compression/__init__.py`, `src/indus/core/compression/anchor.py`,
`tests/unit/core/compression/test_anchor.py`.

Edge cases / failure modes:
- usage missing or `prompt_tokens <= 0` → keep the old anchor; count "usage-less responses" so a
  provider that never reports usage stops the wait from lasting forever;
- the reply row right after the priced prefix is already in `completion_tokens` → skipped by the
  delta walk;
- fingerprint over provider-visible fields only (role, content, api_content, tool_call_id,
  tool_calls) so storage ids and timestamps never break a match;
- `api_content` (the sidecar actually sent) replaces `content` in the estimate;
- reasoning blobs with `encrypted_content`/`data`/`signature` count 0; images 1,500 until learned;
- a learned image cost outside 64…32,768 per image is noise and is not kept;
- a persisted anchor that no longer matches the reloaded transcript is cleared on restore.

Our design differences / decisions:
- Our names: `UsageAnchor`, `TokenMeter`, `ImageCosts`, `AnchorStore`; model_config key
  `usage_anchor`.
- Estimate is characters / 4 (rounded up), as the plan says; the reference counts UTF-8 bytes
  and CJK code points. Kept simple and documented; easy to change in `estimate_text`.
- No per-message memo cache (our estimate is cheap enough; P08-18 soak will tell).
- Image costs are kept per meter (per session) and optionally per `ImageCosts` table shared by the
  host; persistence of that table to the cache dir is left to the host (not wired yet).

Tests we will write:
- anchor + 3 appended rows → anchor total + estimate of exactly those rows (reply row skipped);
- restart: persist through a real P02 store, reload with the same rows → anchor kept;
- edit a priced row → fingerprint mismatch → anchor dropped (and cleared in the store);
- no anchor + huge estimate → `wait`; usage-less provider → `compress`; real reading over → compress;
- encrypted blobs cost 0; `api_content` wins; image residual learned and bounded.

## Decisions for the lead
- Anchor stored in `sessions.model_config["usage_anchor"]` through `sessions.patch_model_config`
  (no new column). Change: one constant in `anchor.py`.

Compare checklist (filled after the rewrite; reopened `agent/usage_anchor.py`,
`agent/image_token_cost.py`, the estimate helpers of `agent/model_metadata.py`):
- [x] Anchor identity = last-row fingerprint + whole-prefix fingerprint + count (test:
  `tests/unit/core/compression/test_anchor.py::test_editing_a_priced_row_drops_the_anchor`)
- [x] Reply row at `base_count` skipped (test: `test_anchor.py::test_anchor_plus_appended_rows`)
- [x] Persisted anchor restored only while it matches, else cleared (tests:
  `test_anchor.py::test_restart_reload_keeps_the_anchor`, `test_anchor.py::test_stale_persisted_anchor_is_cleared`)
- [x] An estimate waits one request without an anchor (R01.8) (test:
  `test_anchor.py::test_no_anchor_and_a_huge_estimate_waits`); after a compaction too
  (`test_anchor.py::test_after_a_compaction_the_estimate_waits_for_real_usage`)
- [x] Usage-less provider / real reading already over: the estimate decides (tests:
  `test_anchor.py::test_usage_less_provider_lets_the_estimate_decide`,
  `test_anchor.py::test_real_reading_over_the_line_lets_the_estimate_decide`)
- [x] Encrypted blobs 0, `api_content` wins, images flat (test: `test_anchor.py::test_estimates_follow_what_is_sent`)
- [x] Image residual learned per model@host, smoothed and banded (test: `test_anchor.py::test_image_cost_learned_from_the_residual`)
- [x] Gap found: a model switch must drop old readings — fixed in `BuiltinEngine.set_route`
  (test: `test_engine.py::test_route_change_clears_the_old_routes_cooldown`)
- Deliberate differences: characters/4 instead of UTF-8 bytes/4 + CJK; no estimate memo cache;
  stale reasoning is charged on every route (no route knowledge here); the learned image table is
  per process (the host may share one `ImageCosts`), not persisted to the cache dir yet.
- Not taken: native-compaction checkpoint latch (no Responses-native compaction planned).

## Status

Done (2026-10-18).

## Integration (lead)

Merged in the P08 wave 1 integration (2026-10-18). The anchor is fed from the session's usage
ledger (one path); usage-less answers reach it too. After a rotating compaction the anchor is
stored on the continuation's row (`TokenMeter.session_id` follows the move).
