# Task: browser page tools (P10-13) Phase: 10 Owner: wave-1 builder I

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/browser_tool.py` (tool
schemas, navigate flow, secret/policy checks, post-redirect check, bot-detection titles, snapshot
fields, click/type/scroll/back/press, console and eval, get_images, private-page guards),
`tools/browser_tool_snapshot.py` (line-boundary truncation, stored full copy, redaction of browser
output), `tools/browser_tool_eval_policy.py` (skimmed: URL literals in JS, current-page re-check).
Closed before writing. Evidence: none.

What it does (own words): nine tools drive the task's page.
`browser_navigate(url)` normalises the URL and passes it through the P05 gate
(`url_policy.check_url`: scheme, secret-bearing URLs such as `?token=…`, the website blocklist,
cloud metadata and private addresses unless `security.allow_private_urls`), opens it, then checks
the address the page actually landed on; a redirect onto a refused address moves the page to
`about:blank` and the call fails. A page title that looks like a bot wall adds a warning. The
result carries the final URL, the title and a compact snapshot, so the model can act at once.
`browser_snapshot(full=false)` returns the page as an outline: each interactive element gets a ref
`@eN`; the compact view lists interactive elements and headings, the full view all visible text.
An outline over `browser.snapshot_threshold` (15,000 chars, floor 1,000) is cut at a line boundary
with a note naming the saved full copy (`<home>/cache/web/browser-snapshot-<hash>.txt`, redacted,
at most 2,000,000 chars) and the `read_file` call for the rest; with `browser.snapshot_summary` and
an auxiliary model the cut part is replaced by the model's summary instead. Pending dialogs and
the frame list (P10-14) are merged in. `browser_click(ref)`, `browser_type(ref, text)` (clears
first; the typed text is shown redacted in the result), `browser_scroll(up|down)` (about half a
screen), `browser_back`, `browser_press(key)`, `browser_get_images` (src, alt, natural size;
`data:` URLs left out), `browser_console(clear, expression?)` (console messages and uncaught
errors; with an expression, the JS is evaluated after the eval policy: every `http(s)://`
literal in the code must pass the URL gate). After back and after an evaluation the current page
URL is checked again; a page on a refused address returns no content. Text from the page (outline,
console, evaluation results) is redacted before it reaches the model; the tool round fences every
`browser_*` result of 32+ characters as untrusted data (P04 `untrusted.py`). All browser tools are
barriers (never run beside other calls).

Inputs → outputs: tool calls → JSON text `{success, …}` or `{success: false, error}`.

Files: `src/indus/tools/builtin/browser/{tools,snapshot}.py`,
`tests/unit/tools/builtin/browser/test_tools.py`, `tests/integration/tools/test_browser_local.py`,
`tests/fixtures/web/*.html`.

Edge cases / failure modes:
- Ref given as `e5`, `@e5`, `[e5]` → all accepted; unknown ref → error telling to snapshot again.
- Page mutated after the snapshot → refs are kept on elements via a per-session random attribute,
  so a page cannot forge them; a removed element gives "ref not found".
- Navigation to a page whose load raises a dialog → `DialogBlocking` result with the dialog.
- Snapshot write fails → plain truncation note without a path.
- Aux summary fails → falls back to truncation.

Our design differences / decisions:
- The outline is produced by our own in-page walker (role, accessible name, value, state) rather
  than a CLI's accessibility snapshot; refs are page attributes, not indexes.
- The URL gate is P05's: credential-named query parameters are refused here too (the task row
  asks for it), and private addresses are refused for the local browser as well unless
  `security.allow_private_urls` is on.
- The sensitive-primitive denylist for evaluation (`restrict_evaluate`) is not taken; only the URL
  literal check (asked by the row) and the current-page re-check.

Tests we will write:
- unit (fake driver): metadata URL refused, `?token=` refused before any navigation, redirect onto
  a private address blocked and page blanked, bot title warning, ref resolution and errors, type
  clears, scroll/back/press, images without `data:`, console buffers and clear, eval policy,
  20 kB snapshot truncated with a saved path, summary path.
- integration (marker `browser`, real Chromium, local static server): navigate → snapshot → click →
  type → back.

## Decisions for the lead
- Scroll step 500 px; console buffer 500 messages and 200 errors per session.
- Intended differences for D-ids: P05's URL gate for the local browser too (private addresses
  refused unless `security.allow_private_urls`; `?token=`-style URLs refused); re-check after click
  and key press; own outline walker; no `restrict_evaluate` denylist.

Compare checklist (filled after the rewrite; reopened `tools/browser_tool.py` 479–1220 and
`tools/browser_tool_snapshot.py`):
- [x] schemas written in our own words; same names and arguments
- [x] URL gate before any browser starts, post-redirect re-check that blanks the page
  (`test_tools.py::test_metadata_address_is_refused_before_any_browser_starts`,
  `::test_url_with_a_token_is_refused_before_fetch`,
  `::test_redirect_to_a_private_address_is_blocked_after_the_redirect`)
- [x] bot-detection title warning (`::test_bot_wall_title_adds_a_warning`)
- [x] compact outline attached to navigate; first navigation 120 s, later 60 s
  (`::test_navigate_returns_title_and_a_compact_outline`)
- [x] snapshot threshold 15,000 (floor 1,000), line-boundary cut, redacted saved copy ≤ 2,000,000
  under `cache/web`, read_file note (`::test_big_snapshot_is_truncated_with_a_saved_path`,
  `::test_threshold_setting_and_floor`); summary path (`::test_big_snapshot_can_be_summarised_by_the_auxiliary_model`)
- [x] type clears first and shows the text redacted (`::test_type_fills_the_field_and_redacts_what_it_shows`)
- [x] scroll 500 px, back re-checked, press (`::test_scroll_back_and_press`,
  `::test_key_press_landing_on_a_private_address_is_blocked`)
- [x] images without `data:` (`::test_images_leave_out_inline_data`); console messages, errors,
  clear (`::test_console_reads_and_clears`); evaluation with JSON parsing and the URL-literal
  policy, page re-check after evaluation (`::test_console_expression_is_evaluated`,
  `::test_eval_policy_refuses_code_that_reaches_a_refused_address`,
  `::test_eval_that_moves_the_page_to_a_private_address_is_withheld`)
- [x] all browser tools barriers; `browser_*` fenced by P04 `untrusted.py` (prefix already listed)
- [x] real Chromium: navigate → snapshot → click → type → back
  (`tests/integration/tools/test_browser_local.py::test_navigate_snapshot_click_type_back`)
- Deliberate differences: the reference applies the private-address rule only to cloud browsers
  and lets credential-named query parameters through; we use P05's gate for every browser (the
  task row asks for `?token=` to be refused; `security.allow_private_urls` opens private
  addresses, the metadata floor stays). The re-check also runs after click and key presses, not
  only after back and evaluation. The outline comes from our own in-page walker; refs live in a
  random per-session attribute.
- Not taken: the opt-in sensitive-primitive denylist for evaluation (`restrict_evaluate`), the
  supervisor fast path for evaluation, Lightpanda fallbacks, recordings.

## Status

Done (2026-10-19). Files: `tools/builtin/browser/{tools,snapshot,driver}.py`,
`tests/unit/tools/builtin/browser/test_tools.py`, `tests/integration/tools/test_browser_local.py`,
`tests/fixtures/web/`, `tests/fakes/browser.py`. Acceptance rows ACC-C21-1 and ACC-C21-2.
Follow-up: requests the page makes on its own (sub-resources, scripts) are not run through the
gate; a Playwright route filter on main-frame navigations would close redirect chains that pass
through a private address before landing on a public one.

## Integration (lead, P10 I+J+K landing): request guard (SSRF fix)
The slice checked only the addresses a tool sends the browser to and the page it lands on, so a
page could make the browser fetch a private or metadata address by itself (images, frames,
`fetch`, form posts, WebSockets), and a redirect chain could pass through one. Fixed with
`tools/builtin/browser/guard.py` (D-16.171): `SessionManager` installs a `RequestGuard` on every
session before first use (`BrowserHandle.guard(gate)`; the gate is `url_policy.check_url_async`
under the session's scope and the manager's resolver; a guard that cannot be installed closes the
browser). The route covers the whole context of a launched browser (service workers blocked) and
our own tab of an attached one. Refused requests are aborted unsent; an allowed one is sent by
Playwright with `max_redirects=0`; a sub-resource's redirect chain is followed hop by hop in the
guard (each target checked first; 303 and 301/302-after-POST become GET) and the page gets the
last answer; a navigation's chain is walked with `HEAD` (GET when refused) and every hop checked
before the browser gets the first redirect back. WebSockets are checked before they connect;
`data:`/`blob:`/`about:`/`javascript:` pass, other schemes are refused; any guard error aborts.
Tests: `tests/unit/tools/builtin/browser/test_guard.py` (fakes) and
`tests/integration/tools/test_browser_ssrf.py` (real Chromium, two local servers: a page that
embeds, redirects to (image, frame, `fetch`), opens a socket to and navigates through a refused
`127.0.0.1` address; the private server sees no request; with the guard removed the same test
fails on the private server's hits).
Open seam: Playwright sends the hops of a redirect the browser follows without routing them, so a
server that answers the browser's walk differently from the guard's can still receive one `GET`
at a refused address (the page re-check refuses the landing). A guard on the network side (a local
filtering proxy for the launched Chromium) would close it.
