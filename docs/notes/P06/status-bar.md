# Task: Banner, recap and status bar (P06-06) Phase: 06 Owner: P06 slice B builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/cli_status_bar_mixin.py`
(segments, width tiers, context colours, context bar, model truncation),
`hermes_cli/cli_session_mixin.py` (resume helpers), `hermes_cli/cli_agent_setup_mixin.py` (the
recap collector and panel it points to). Research `04` §3.6. Closed before writing.
Evidence: none.

What it does (own words):
- Banner at start: product name and version, model, terminal backend, working directory, tool and
  skill counts, and a one-line key hint.
- Recap on resume ("Earlier in this conversation"): the last `display.resume_exchanges` (10)
  exchanges; user text cut at `resume_max_user_chars` (300), assistant text at
  `resume_max_assistant_lines` (3) lines then `resume_max_assistant_chars` (200); the very last
  assistant reply in full; a count of older hidden messages; tool-only assistant rows skipped.
- Bottom toolbar, rebuilt every `display.cli_refresh_interval` (1.0 s): model (cut past 26 chars) │
  context used/max (`~` when estimated) │ fill bar │ cost (`n/a` until P08) │ compressions │
  background jobs │ turn duration │ title │ auto-approve warning. Width tiers: ≥76 full, 52–75
  compact (no context detail, no fill bar, ` · ` separators), <52 minimal (model, duration,
  auto-approve warning). Context colour: <50 % green, 50–80 yellow, 80–95 orange, ≥95 red.

Inputs → outputs: `StatusSnapshot` → `status_fragments(snapshot, width)` (prompt_toolkit style
fragments) and `status_text(...)` (plain); `banner(info)` and `recap(rows, settings)` → rich
renderables.

Files: `src/indus/cli/repl/status.py`, `src/indus/cli/repl/banner.py`,
`tests/unit/cli/repl/test_status.py`.

Edge cases / failure modes:
- Unknown context size: `--`; zero max: no percentage.
- Stored text with control characters: escape sequences removed before display.
- Narrow terminals: nothing wraps; the bar is cut to the width.
- Status source raising: the bar falls back to the model name only.

Our design differences / decisions:
- Our labels, glyphs and colours; no battery, git branch, latency, cache-hit or pet segments.
- The title is a plain segment, not right-aligned.

Tests we will write:
- Width-tier goldens at 40/60/100 columns; colour thresholds at 49/50/80/81/95; model truncation;
  `~` estimate mark; recap truncation table; banner contents.

## Decisions for the lead
- When the bar does not fit, whole segments are dropped (title, context detail, jobs,
  compressions, cost, bar, duration, percentage) and then the model name is shortened, so the
  auto-approve warning is never the part that gets cut.
- `StatusSnapshot` is filled by the REPL from `ReplOptions` (model, title, auto-approve) and its own
  turn timing; context use, cost and job counts come from an optional `status_source` callable that
  slice A (or P08) supplies.

Compare checklist (filled after the rewrite; reopened `hermes_cli/cli_status_bar_mixin.py`,
`hermes_cli/cli_agent_setup_mixin.py`, `hermes_cli/cli_session_mixin.py`):
- [x] Tier boundaries 52/76 and segment order (`test_status.py::test_width_tier_goldens`,
  `::test_tier_boundaries`)
- [x] Context colour thresholds (`test_status.py::test_context_colour_thresholds`)
- [x] Compression count coloured as it grows — gap found on compare, added
  (`test_status.py::test_compression_count_colour`)
- [x] Recap truncation, hidden count, last reply in full, tool-only rows skipped
  (`test_status.py::test_recap_*`)
- Deliberate differences: no battery, git branch, latency, cache-hit, goal or pet segments; no
  model-alias reverse lookup; recap shows no event rows (model switch etc.) until those rows exist.

## Status

Done (2026-10-18).
