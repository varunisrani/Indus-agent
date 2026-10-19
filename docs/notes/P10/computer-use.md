# Task: computer_use desktop control (P10-20) Phase: 10 Owner: wave-7 builder U

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/computer_use/tool.py`
(whole: action table, hard blocks, approval scopes, per-session backend cache, result shaping,
capture envelope, aux-vision fallback, element caps, cache files), `tools/computer_use/backend.py`
(whole: element / capture / action result shapes, backend and provider contract),
`tools/computer_use/schema.py` (whole: action enum and parameters), `tools/computer_use/doctor.py`
(skimmed: health rows, the Accessibility / Screen Recording permission rows and their hints).
Closed before writing. Evidence: none.

What it does (own words): one model tool, `computer_use`, lets the agent look at and operate
desktop apps without taking the machine away from the person: input is delivered to the target
window in the background, the pointer is never moved and focus is never stolen. The tool talks to a
*desktop driver*. The built-in driver is an external program (the user installs it; we only start
it) spoken to over its stdin/stdout, one JSON object per line. Each tool action maps to one driver
operation: `capture` (mode `som` = screenshot with numbered marks plus the element list, `vision`
= plain screenshot, `ax` = accessibility tree as text), `click`, `double_click`, `drag`, `scroll`,
`type`, `key`, `set_value`, `wait`, `list_apps`, `focus_app`. Everything except `capture`,
`list_apps` and `wait` changes what the person sees, so it is asked for through the P05 approval
building blocks first (manual by default, D-16.53); a denied action never reaches the driver.
Some input is refused outright before any approval (logout / lock / force-empty-trash shortcuts,
typed text that our command detector rates hardline). Without the driver the tool is not offered
and `indus doctor` says why.

Inputs → outputs: tool args `{action, mode?, app?, element?, x?, y?, button?, to_x?, to_y?,
to_element?, direction?, amount?, text?, keys?, value?, ms?}` (`app` also names the app for
`focus_app`) → JSON text
(`{"ok": true, "action": ..., ...}` / `{"error": ...}`) or, for image captures on a route that
takes images, the multimodal envelope `{multimodal, content: [text, image_url], text_summary}`.
Settings: `computer_use.driver` (`stdio`, or a plugin driver's name), `computer_use.driver_cmd`
(env `INDUS_CUA_DRIVER_CMD` wins), `computer_use.action_timeout_s` (30). Doctor rows under
`computer_use`.

Driver wire (ours): request `{"id": n, "op": "<op>", "args": {...}}`; answer
`{"id": n, "ok": true, "result": {...}}` or `{"id": n, "ok": false, "error": "..."}`. The first
request is `hello`; the answer's `result` carries `ready` (bool), `name`, `version`, optional
`permissions` `{accessibility, screen_recording}` booleans and an optional `reason` when not ready.
Capture result: `{mode, width, height, image (base64), mime, app, window, elements: [{index, role,
label, bounds: [x, y, w, h], app}], tree (text, ax mode)}`. The driver must exit when its stdin
closes.

Files: `src/indus/tools/builtin/computer_use/{__init__,tool,driver}.py`,
`tests/unit/tools/builtin/computer_use/{__init__,test_tool}.py`, `tests/fakes/desktop_driver.py`;
shared: `src/indus/tools/builtin/__init__.py` (module row), `src/indus/tools/toolsets.py`
(`computer_use` toolset, core list, `DEFERRED_BY_DEFAULT`), `src/indus/foundation/config/schema.py`
(+ pinned-defaults test), `src/indus/cli/doctor/checks.py` (one row).

Edge cases / failure modes:
- No command configured, or the program is not on PATH, or `hello` fails / says not ready → probe
  false (tool absent); doctor names the exact cause and the fix.
- Driver exits, closes stdout, writes garbage or does not answer within the action timeout → the
  call returns one bounded error (stderr tail capped) and the driver is killed and forgotten; the
  next call starts a fresh one. A driver error answer (`ok: false`) is passed back without a
  restart.
- Unknown action → error listing the valid ones (no silent aliasing); missing required arguments
  (`type` without text, `drag` without an end, `focus_app` without a name) → error before approval.
- Approval: auto-approve / `INDUS_YOLO_MODE` / `mode: off` → allowed; no one to ask (cron, `-z`,
  unattended, no broker) → the run kind's `approvals.*_mode` (deny by default); remembered for the
  session per action (`computer_use:<action>`); smart mode asks the judge first.
- Captures: element list capped at 100 entries and labels at 120 chars (full list spilled to a
  private cache file when cut); a screenshot saved privately under `<home>/cache/computer_use/`
  (newest 20 kept) so it can be shared as `MEDIA:<path>`; main route without image support → the
  auxiliary `vision` task describes it (text), or a text result with the element list if that
  fails too.
- Several sessions: one driver per (profile, session); calls on one driver are serialised.
- The event loop changed under a cached driver (tests, host restart) → the old one is discarded.

Our design differences / decisions:
- Our own stdio JSON-lines protocol and op names instead of the reference's MCP-speaking driver;
  the driver program is the user's choice (`driver_cmd`), we ship none.
- No foreground "delivery ladder", `bring_to_front`, `raise_window`, `right_click`/`middle_click`
  actions or `list_windows`: the row asks for background-only input (`button` covers right and
  middle on `click`). No `capture_after`, no screenshot de-duplication, no permission-mode
  escalation of the driver under auto-approve, no "human has control" lease (Bot Desktop is not in
  our plan).
- Plugin seam: `register_driver(name, factory, ready)`; `computer_use.driver` picks one (P13 fills).
- Deferral: the tool-search bridge (C4) is not built yet; `toolsets.DEFERRED_BY_DEFAULT` lists
  `computer_use` so the bridge can hide it by default when it lands.
- Readiness of the stdio driver is cached per command for 300 s (on top of the registry's 30 s) so
  building tool arrays does not spawn the driver every turn.

Tests we will write (fake driver = `tests/fakes/desktop_driver.py` run with our Python, logging
every request to a file):
- click maps coordinates and button 1:1; each other action reaches the driver as its op.
- capture som → envelope with image part and element lines; ax → JSON text with the tree; route
  without vision and no aux → text with the screenshot path.
- approval denied → the driver log has no `click`; session approval asked once; no broker → denied
  by the unattended policy; capture / list_apps / wait never ask.
- hard blocks: logout shortcut and a hardline typed command refused without asking.
- missing driver → tool absent from a registry pass, doctor row explains; not-ready driver too;
  macOS permission rows when the driver reports them.
- driver crash mid-call → bounded error; next call restarts it and works; hang → timeout error.
- schema has the 11 actions; toolset / deferred constant.

## Decisions for the lead
- Wire protocol and op names above are ours (no third-party driver speaks it yet); a P13 plugin
  can adapt an existing driver. Change by registering another driver.
- `computer_use` joins `CORE_TOOLS` (the api-server bundle already subtracts it); it is probe-gated,
  so tool arrays only change where a driver is configured.
- Host seam not wired here: `DEFAULT_TOOL.release(session_id)` on session close and
  `await DEFAULT_TOOL.shutdown()` at exit (service/code_host); until then drivers die with their
  stdin when Indus exits.

Compare checklist (filled after the rewrite; reopened `tools/computer_use/tool.py`,
`backend.py`, `schema.py`):
- [x] Action set and 1:1 mapping; defaults (left button, scroll down by 3, wait 1 s, ≤ 30 s)
  (test: `test_tool.py::test_click_maps_coordinates_and_button`,
  `::test_each_action_reaches_the_driver_as_its_op`, `::test_list_apps_returns_the_apps`)
- [x] Drag needs a start and an end; set_value needs an element and a value; focus_app needs an app
  (test: `::test_bad_arguments_fail_before_anything_runs`)
- [x] Only capture / list_apps / wait skip approval (focus_app asks too); session grant per action;
  nobody to ask → blocked (tests: `::test_denied_approval_never_reaches_the_driver`,
  `::test_session_approval_is_asked_once_per_action`, `::test_looking_and_waiting_never_ask`,
  `::test_nobody_to_ask_blocks_unless_the_policy_approves`, `::test_auto_approve_runs_without_asking`)
- [x] Hard refusals before approval, combos split on `+`, `-` and spaces with key aliases
  (test: `::test_hard_refusals_come_before_approval`, `::test_blocked_combo_reads_aliases_and_ignores_harmless_keys`)
- [x] Capture shaping: 100 elements, 120-char labels, full list spilled to a private file, zero
  rect = unknown position, private screenshot copy (newest 20), envelope on an image route,
  aux-vision on a text route, element list alone when that fails
  (tests: `::test_som_capture_returns_the_envelope`, `::test_ax_capture_is_text_with_the_tree`,
  `::test_capture_on_a_text_route_asks_the_vision_side_task`,
  `::test_capture_without_any_vision_falls_back_to_the_element_list`,
  `::test_long_element_lists_are_capped_and_spilled`, `::test_screenshots_keep_only_the_newest`)
- [x] Gap found: images below 8x8 are refused by providers — fixed by validating the screenshot
  (`media.imaging.inspect`, type sniffed from the bytes) and leaving tiny or unreadable ones out,
  pinned by `::test_unusable_screenshots_are_left_out`
- [x] Gap found: wrong action names get the nearest real action named (never run in its place),
  pinned by `::test_bad_arguments_fail_before_anything_runs`
- [x] One driver per (profile, session), released on request; failure drops it and the next call
  starts afresh (tests: `::test_release_stops_the_session_driver`,
  `::test_a_crash_is_a_bounded_error_and_the_next_call_restarts`,
  `::test_a_hung_driver_times_out_and_is_killed`, `::test_garbage_output_counts_as_a_crash`,
  `::test_a_refusal_from_the_driver_keeps_it_running`)
- [x] Probe and doctor (tests: `::test_missing_driver_leaves_the_tool_out_and_doctor_explains`,
  `::test_driver_not_ready_is_absent_and_says_why`, `::test_doctor_permission_rows`,
  `::test_doctor_check_is_registered`); plugin driver seam (`::test_a_registered_plugin_driver_is_used_when_selected`)
- Deliberate differences: background-only input (no foreground ladder, `bring_to_front`,
  `raise_window`, driver verdict ladder); `right_click` / `middle_click` folded into `button`;
  `x`/`y` instead of a coordinate pair; no `list_windows`, `pid`/`window_id` targeting or
  `capture_after`; typed text is refused only when our detector rates it hardline (risky but not
  hardline text is named in the approval prompt instead of a separate pattern list); an
  unconfigured or unknown driver hides the tool (the reference keeps an unknown backend visible to
  surface an error) and `indus doctor` explains; the driver protocol is our own JSON lines.
- Not taken: screenshot de-duplication across captures, HiDPI bounds-scale hints (our protocol
  says bounds are screenshot pixels), downscaling before the aux vision call (no Pillow), the
  "human has control" lease, driver permission-mode escalation under auto-approve, the input
  target mismatch guard (`app` is not an input argument here).

## Status

Done (2026-10-19). Shared files touched: `tools/builtin/__init__.py`, `tools/toolsets.py`,
`foundation/config/schema.py` + `tests/unit/foundation/config/test_schema.py`,
`cli/doctor/checks.py` + `tests/unit/cli/commands/test_doctor.py` (check order),
`tests/unit/tools/test_toolsets.py`, `tests/unit/cli/setup/test_wizard.py` (bundle rows). The
golden tool arrays (`tests/golden/prompt_surface/*/tools.json`) now differ by the new
`computer_use` entry; not regenerated (lead). Host release/shutdown wiring handed to the lead.
