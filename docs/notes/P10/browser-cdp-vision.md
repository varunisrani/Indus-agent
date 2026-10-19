# Task: browser vision, CDP, dialogs and /browser (P10-14) Phase: 10 Owner: wave-1 builder I

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/browser_tool_vision.py`
(native attach vs auxiliary analysis, screenshot path), `tools/browser_tool.py` lines 1225–1320
(vision capture), `tools/browser_cdp_tool.py` (schema, timeout clamp, private guard, result
redaction, availability gate), `tools/browser_dialog_tool.py` (schema, respond flow),
`tools/browser_supervisor.py` and `_dialogs` (skimmed: reconnect budget 5, dialog policies and
300 s safety timeout), `hermes_cli/cli_commands_mixin.py` `/browser` handlers (connect URL
normalising, disconnect, status). Closed before writing. Evidence: none.

What it does (own words): `browser_vision(question, annotate=false)` saves a screenshot of the
current page under `<home>/cache/screenshots/` (with `[N]` labels on the elements that have refs
when `annotate` is set). When the main model can see images the screenshot is returned as a
multimodal result (the image part plus a short text with the path); otherwise the auxiliary
`vision` task answers the question about it and the answer (redacted) comes back with the path.
Either way the path can be shown to the user as `MEDIA:<path>`.
`browser_cdp(method, params, timeout=30)` sends one Chrome DevTools Protocol command over the
session's own CDP channel (page level; `Browser.*`/`Target.*` at browser level); the timeout is
clamped to 1–300 s; the result is redacted except protocol-binary fields. The tool is offered only
when a CDP endpoint is configured and answers when the tool array is built.
A supervisor per session listens to the page: console messages and errors (for
`browser_console`), the frame list, and native dialogs (alert, confirm, prompt, beforeunload). A
dialog waits for `browser_dialog(accept|dismiss, prompt_text?)` under the default
`browser.dialog_policy: must_respond`, and is dismissed after `browser.dialog_timeout_s` (300 s);
`auto_dismiss` / `auto_accept` answer at once and keep a record. Pending dialogs appear in every
snapshot. For a session attached over CDP, a lost connection is retried with backoff; after 5
failures in a row the supervisor gives up and the session is replaced on next use.
`/browser connect <cdp-url> | disconnect | status | use local` switches the profile's browser
choice; sessions notice the change on their next use and reconnect.

Inputs → outputs: tool calls → JSON or the multimodal envelope; `/browser …` → lines on the REPL.

Files: `src/indus/tools/builtin/browser/{cdp,vision}.py`,
`tests/unit/tools/builtin/browser/test_cdp_vision.py` (also covers `/browser`),
`src/indus/cli/slash/browser_cmds.py`; shared: `service/commands/registry.py` (`use`
subcommand), `cli/slash/dispatch.py` (handler table).

Edge cases / failure modes:
- `browser_vision` with neither native vision nor an aux client → error that keeps the path.
- Aux call fails → error with the screenshot path and a note.
- `browser_dialog` with nothing pending, or two pending and no id → error naming the ids.
- `browser_cdp` with params not an object; unknown method → CDP error text; timeout → error.
- `/browser connect` to an address that does not answer → nothing changes, says why.

Our design differences / decisions:
- `browser_cdp` has no `target_id` / `frame_id`: it runs on the session's own page (Playwright
  CDP session), browser-level domains on the browser session. The supervisor is built on
  Playwright's page events instead of a second WebSocket.
- `browser_dialog` is offered with the browser toolset, not only with a CDP endpoint, because our
  supervisor sees dialogs of the local browser too.
- `/browser connect` does not launch a debug Chrome for the user; it attaches to one that answers.

Tests we will write:
- no CDP endpoint → `browser_cdp` absent from the tool array; with one → present.
- fake CDP channel: method round trip, timeout clamped to 300, error text, binary field kept.
- dialog captured, shown in snapshot, accepted with prompt text; auto policies; safety timeout.
- reconnect: 5 failures after attach → supervisor stops; a success resets the count.
- vision: native route → envelope; non-vision route → aux fake called with the image; annotate.
- `/browser connect` switches the next session's driver to attach; `use local`; `disconnect`;
  `status`.

## Decisions for the lead
- Host wiring needed: the tool round should offer `aux` (the agent's bound `AuxClient`) and
  `native_vision` (route supports images) as handler extras; until then `browser_vision` answers
  with the screenshot path and a note. **Integration (lead):** done — `core/tool_round.handler_extras`
  offers `aux` (`SessionRuntime.aux`), `native_vision` (`providers.route.sees_tool_images`; the
  slice's `main_vision` name was folded into J's `native_vision`) and `subagent`.
- Intended differences for D-ids: `browser_cdp` without `target_id`/`frame_id`; `browser_dialog`
  offered without CDP; viewport screenshots; `/browser connect` never launches a browser;
  `/browser use local` instead of an engine switch; no system note to the model on connect.

Compare checklist (filled after the rewrite; reopened `tools/browser_tool_vision.py`,
`tools/browser_cdp_tool.py`, `tools/browser_dialog_tool.py`, `tools/browser_supervisor.py`
(constants, respond flow) and the `/browser` handlers):
- [x] `browser_cdp` absent without a live endpoint (`test_cdp_vision.py::test_no_cdp_endpoint_means_no_browser_cdp_in_the_tool_array`,
  `::test_a_dead_endpoint_keeps_browser_cdp_out`, `::test_a_live_endpoint_brings_browser_cdp`)
- [x] timeout default 30, clamp 1..300 (`::test_clamp_cdp_timeout`, `::test_cdp_timeout_is_clamped_to_300`)
- [x] round trip; errors carry the protocol docs link (`::test_cdp_round_trip_and_levels`,
  `::test_cdp_argument_and_protocol_errors`); timeout keeps the session (`::test_cdp_timeout_reports_and_keeps_the_session`)
- [x] text redacted, protocol-binary fields kept, flagged ones only with `base64Encoded: true`
  (`::test_cdp_results_are_redacted_but_binary_fields_kept`)
- [x] `Page.navigate` / `Runtime.evaluate` guarded (`::test_cdp_guards_refused_addresses`)
- [x] dialog captured, in snapshots, answered with prompt text, id needed for several; policies
  must_respond / auto_dismiss / auto_accept; 300 s safety dismiss; history of 20
  (`::test_dialog_is_captured_shown_and_accepted`, `::test_prompt_dialog_gets_the_text`,
  `::test_two_open_dialogs_need_an_id`, `::test_auto_policies_answer_at_once`,
  `::test_unanswered_dialog_is_dismissed_after_the_safety_timeout`); real prompt in
  `test_browser_local.py::test_dialog_vision_and_cdp`
- [x] reconnect: 5 failures after attach then give up, success resets (`::test_reconnect_gives_up_after_five_failures`,
  `::test_reconnect_success_resets_the_count`)
- [x] vision: native attach vs auxiliary `vision` task; path kept on failure; annotate labels
  (`::test_vision_on_a_non_vision_route_asks_the_auxiliary_model`,
  `::test_vision_on_a_vision_route_attaches_the_image`, `::test_vision_failure_keeps_the_path`)
- [x] `/browser connect|disconnect|status|use local` (`::test_browser_connect_switches_the_sessions_driver`,
  `::test_browser_use_local_overrides_the_config`, `::test_browser_connect_to_nothing_changes_nothing`)
- Deliberate differences: the reference's raw tool opens a fresh WebSocket per call and accepts
  `target_id` / `frame_id`; ours uses the session's Playwright CDP channels and has neither.
  `browser_dialog` is available with the browser toolset, not only with CDP. Screenshots are of
  the viewport (the reference takes the full page); the auxiliary call uses the `vision` task's
  configured timeout (the reference defaults to 120 s). `/browser connect` attaches only to a
  browser that already answers; it does not launch one. `/browser use local` replaces the
  reference's engine switch.
- Not taken: launching a debug Chrome from `/browser connect`, the system note to the model after
  connect/disconnect, OOPIF routing through the supervisor, the embed-size budget for native
  screenshots (P10-15 owns the vision embed budget; screenshots are attached as PNG).

## Status

Done (2026-10-19). Shared files: `service/commands/registry.py` (`/browser` args and `use`
subcommand), `cli/slash/dispatch.py` (handler table), `tests/unit/service/commands/test_registry.py`
(`/browser` is the one P10 command with a CLI handler; help grouping golden). New:
`cli/slash/browser_cmds.py`. Acceptance rows ACC-C23-1, ACC-C26-1.
