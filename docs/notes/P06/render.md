# Task: Streaming render (P06-05) Phase: 06 Owner: P06 slice B builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/cli_stream_mixin.py`
(delta line buffering, reasoning box, tool progress lines, flush/reset), `hermes_cli/cli_render.py`
(skim: markdown strip helper, final render modes, output history ring). Research `04` §3.6,
`05-tui.md` §3.13–3.16 and trap R05.4. Closed before writing.
Evidence: none.

What it does (own words): turns the session's protocol events into terminal output. Reply text
arrives in pieces; whole lines are printed as soon as they end, the unfinished tail is held (and
offered to the status line as a preview) until its newline or the turn's end. Reasoning is hidden
unless switched on (`/reasoning show`), then shown dim. Each tool call is shown while it runs (the
status line's spinner names the tool and a short preview of its main argument) and leaves one
line in the scrollback when it finishes: mark, emoji, name, preview, duration. Notices and retry or
compression statuses print one line. When the user interrupts, the partial reply is sealed at
once with an "interrupted" marker and every later delta of that turn is dropped (R05.4).

Inputs → outputs: `StreamRenderer.handle(event)` for every protocol event; `seal()` on a local
interrupt; `activity()` for the spinner text; output → a rich `Console`; `ring` keeps the last 200
printed lines (plain text) for a redraw.

Files: `src/indus/cli/repl/render.py`, `src/indus/cli/repl/textutil.py` (new: escape cleaning),
`tests/unit/cli/repl/test_render.py`,
`tests/fixtures/event_streams/*.jsonl`, `scripts/dev/record_event_streams.py` (new: records the
streams from the real service and the fake model).

Edge cases / failure modes:
- Deltas without newlines: held until `turn.done`, then flushed.
- Model text with rich markup (`[bold]`): printed literally, never interpreted.
- Deltas after a seal, or for a turn other than the current one: ignored.
- `turn.done` with a failure: one red line with the reason; budget / iteration exits: a yellow note.
- A tool that never completes (interrupted): its spinner entry is dropped at `turn.done`.
- `display.final_response_markdown: strip`: fences, headings, bold/italic wrappers, inline code and
  link syntax removed per line; lists and quotes kept.

Our design differences / decisions:
- Streamed lines are final; no second full markdown re-render after the stream (it would print the
  reply twice in the scrollback). `render` mode prints lines as the model wrote them.
- Our glyphs, emoji table, spinner verbs and labels.
- No table realignment, no inline diffs, no turn summary (later phases).

Tests we will write:
- Fixture streams (plain answer, tool trail, interrupt mid-delta, approval request) rendered to a
  `Console(record=True)`: golden text.
- Deltas after a seal ignored; reasoning hidden/shown; strip mode; markup not interpreted; ring cap.

## Decisions for the lead
- `tool.started` carries the raw tool arguments (a recorded stream showed a bearer token in clear);
  the renderer masks previews with `redact_output` before shortening them. The event itself
  should probably be redacted at the source for every surface (follow-up for P04/P12 owners).
- Fixtures are recorded from the real stack by `scripts/dev/record_event_streams.py` (new) rather
  than written by hand; the recorder's token is not credential-shaped.

Compare checklist (filled after the rewrite; reopened `hermes_cli/cli_stream_mixin.py`,
`hermes_cli/cli_render.py`):
- [x] Line-boundary flushing, final flush on turn end (`test_render.py::test_plain_answer_golden`)
- [x] Reasoning hidden by default, shown dim, closed when the reply starts
  (`test_render.py::test_reasoning_hidden_by_default`, `::test_reasoning_shown_dim_when_switched_on`)
- [x] Tool start (status line) / complete (scrollback line) (`test_render.py::test_tool_trail_golden`,
  `::test_activity_names_the_running_tool_then_the_partial_line`)
- [x] Strip mode keeps lists and quotes, cron stars and snake_case
  (`test_render.py::test_strip_markdown_line`)
- [x] Seal on interrupt, later deltas dropped (R05.4) (`test_render.py::test_deltas_after_an_interrupt_are_ignored`)
- [x] Partial line mirrored to the status line while it is long in coming
- Deliberate differences: no reply box borders, no table realignment, no inline diffs, no
  think-tag filtering in the delta stream (the core already splits reasoning into
  `reasoning.delta`), no per-tool "preparing" line (no event for it yet).

## Status

Done (2026-10-18). Fixtures: `tests/fixtures/event_streams/{plain_answer,tool_trail,
interrupt_mid_delta,approval_request}.jsonl` + README, recorder `scripts/dev/record_event_streams.py`.

## Integration (lead)

- SECURITY follow-up done at the source: `core/tool_round.py` emits `tool.started` with
  `safety.redaction.redact_arguments(call.args, scope=runtime.scope)` (every string through
  `redact_output`, credential-shaped keys masked whole; the tool still runs with the raw
  arguments), so every surface gets masked arguments. Tests:
  `tests/unit/core/test_tool_round.py::test_tool_started_arguments_are_redacted_at_the_source`,
  `tests/unit/safety/test_redaction.py::test_redact_arguments_masks_strings_and_secret_keys`.
  The renderer's mask stays as defence in depth.
- `tests/fixtures/event_streams/approval_request.jsonl` re-recorded with
  `scripts/dev/record_event_streams.py` after that change (why: `tool.started` now arrives masked,
  and the old recording held a credential-shaped bearer literal). Only that line changed
  (`Bearer rec0…mnop`); the golden's trail line now shows `Bearer [redacted]` because the
  renderer's own mask re-masks the short remainder.
