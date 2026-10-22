# Task: Preview annotate mode and agent-driven preview automation (P24-25) Phase: 24 Owner: wave-39 builder U1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/src/lib/preview-annotate/`
(file list; `group.ts` all, `tokens.ts` all; `in-page.ts` lines 250–360 for the redaction rules),
`apps/desktop/src/app/chat/right-rail/preview-annotate-host.ts` (all 126 lines, behaviour only).
Research 08 §3.15 (preview row). Closed before writing.
Evidence: none.

What it does (own words): two additions to the preview pane (P20-28).

Annotate mode: the person switches it on, clicks elements on the page in the preview and writes a
note for each; numbered pins mark them. For each pin the guest page itself (inside the webview's
sandbox) records a short selector path and a small piece of the element's markup with anything
secret removed first — the values of password and hidden inputs, secret-looking fields and
attributes (`token`, `key`, `secret`, `password`, `auth`, `session`, `credential` in a name),
`data-secret` content — so nothing secret ever reaches the app. Pins are then batched by DOM
subtree: pins that share their container go into one batch, and a pin inside another pinned
element joins that element's batch. The batch becomes one annotation message that is put in front
of the person; it is sent only when they press Send. Leaving annotate mode, closing the pane or
waiting never sends anything.

Automation: the agent can drive the preview of its own session through two server requests,
`preview.act` (navigate, click, type, scroll, annotate) and `preview.read` (snapshot: title, URL,
visible text, a redacted outline of interactive elements). The window that shows that session's
preview answers; any other window declines with `not_shown` (P17-07). Every action runs as a script
inside the guest page (`executeJavaScript` on the webview), never in the app's renderer. Navigation
stays inside the guest and is limited to http(s) and the session's local files; an untrusted page
still cannot open the OS browser (only trusted clicks are handed off, D-16.20). The desktop-only
tools `drive_preview` and `annotate_preview` turn into these requests; they exist only in sessions
a desktop window created.

Inputs → outputs:
- pins `{selector, note, html, rect}` → batches → message text (Markdown list per batch).
- `preview.act {session_id, action, selector?, text?, url?, dx?, dy?, pins?}` →
  `{ok, detail?}`; `preview.read {session_id, what: "snapshot"}` → `{title, url, text, elements[]}`;
  a window not showing the session → `{declined: "not_shown"}`.
- tools: `drive_preview {action, ...}` (toolset `desktop-preview`), `annotate_preview {pins}`.

Files: `apps/desktop/src/panes/preview/annotate.ts`, `annotate.test.ts`, `automation.ts`,
`automation.test.ts`, `src/indus/tools/builtin/preview.py`, `tests/unit/tools/builtin/test_preview.py`.
Shared: `apps/desktop/src/panes/preview/PreviewPane.tsx` (annotate toolbar, pins panel, automation
host), `apps/desktop/src/panes/strings.en.ts`.

Edge cases / failure modes:
- a password input that the page pre-filled, a textarea named `api_token`, a hidden CSRF input, an
  attribute `data-auth-token="…"` → all `[redacted]` in the clone; the live page is untouched;
- markup snippet capped at 600 characters; note capped at 2,000 characters; at most 50 pins;
- a pin whose element is gone (page navigated) keeps its selector and note;
- one pin → one batch; pins with no element (an area) go into a trailing batch;
- automation: unknown action, a selector that matches nothing, a `javascript:` / `data:` URL, a
  request for another session, a guest not ready → typed failures; args are passed to the guest as
  JSON, never spliced as code;
- the tool outside a desktop session is not registered; a desktop session whose window closed →
  the request is undeliverable and the tool says so.

Our design differences / decisions:
- Batching: union of pins that share a parent path or contain one another (our rule); the
  reference derives a split depth and refines oversized groups.
- `annotate_preview` lets the agent place its own numbered notes on the page for the person (the
  reverse direction of annotate mode); the reference's agent tools are named differently.
- `preview.act` / `preview.read` are new server-request kinds. The protocol models, the broker and
  the client channel do not know them yet, so the Python tool talks to a `PreviewPort` and the
  desktop host takes a plain request object; both sides state the interface (below) and the
  integrator adds the kinds to `indus.protocol.server_requests` and `@indus/client`.

Interface assumed (stand-in until wiring):
- Python: `PreviewPort.request(kind: "preview.act" | "preview.read", payload: dict, timeout: float)
  -> dict`, raising `PreviewDeclined(reason)` (`not_shown`) or `PreviewUndelivered`; offered to the
  handler as the `preview` extra; `register_tools(registry, surface=...)` registers only for `gui`.
- Desktop: `PreviewAutomation.handle({kind, request_id, session_id, ...}) -> Promise<answer |
  {declined: "not_shown"}>`.

Tests we will write:
- password value never present in the outgoing annotation (jsdom page with a filled password,
  hidden input, secret attribute);
- two pins in one subtree → one batch; disjoint subtrees → two; nested pin joins its container;
- annotate with no explicit send sends nothing (mode off, pane closed, time passes);
- `preview.act` for a session not shown → `not_shown`; scripts carry args as JSON; navigate refuses
  non-web URLs; snapshot redacts;
- Python: tool absent outside desktop sessions; `not_shown` → a clear tool error; argument checks.

## Decisions for the lead
- New server-request kinds `preview.act` / `preview.read` need the protocol + client wiring above.
- `annotate_preview` semantics (agent → person notes) chosen; change by renaming the action.

Compare checklist (filled after the rewrite; reopened the files in `Read:`):
- [x] redaction set: password / hidden inputs, secret-named fields (name, id, autocomplete) and attributes, `data-secret` content and fields; text taken from the scrubbed clone only (tests: `annotate.test.ts::no password, hidden value or secret attribute leaves the guest`, `::the outgoing annotation never carries the password`)
- [x] gap found: the first version read the pick's text from the live element, which leaked a secret textarea's and a `data-secret` span's content — fixed by reading text from the scrubbed copy, pinned by the test above; typed values of plain fields are not copied either (the reference does not copy them)
- [x] markup budget 600 characters (test: `::markup is capped and selectors are short paths`)
- [x] container + inner pins stay one batch; unanchored pins in one trailing batch; batch order follows the first pin (tests: `::two pins in one subtree make one batch`, `::disjoint subtrees stay apart, nested pins join their container, loose pins trail`)
- [x] the install script is built by concatenation and is self-contained after the build transform (test: `::the install script is self-contained and idempotent`)
- [x] automation declines `not_shown`, runs in the guest with JSON args, navigation web/file only, secret fields not typed into (tests: `automation.test.ts`)
- Deliberate differences: union-by-container batching instead of a derived split depth with refinement; `annotate_preview` = agent → person notes; snapshot of interactive elements with secret values redacted.
- Not taken: crop screenshots per pin, CSS property capture, area (drag) pins, oversized-group refinement.

## Status

Done (2026-10-21). Wiring owed: `preview.act` / `preview.read` kinds in `indus.protocol.server_requests` + `@indus/client` (decline answer `not_shown`), the desktop request store routing them to `windowPreviewAutomation.handle`, a `PreviewPort` over the session broker offered to tool handlers as `preview`, and `preview.register_tools(registry, surface=…)` called for sessions a `gui` viewer created. Shared: `PreviewPane.tsx` (annotate toolbar, pin panel, automation attach), `d2Panes.tsx` (session id + add-to-chat), `en.ts`.

## Wiring (wave 42, WB — D-16.799)

Wired: protocol kinds `preview.act` / `preview.read` (`PreviewActRequest`, `PreviewReadRequest`;
answers `{ok, error, result}`), `PREVIEW_REQUEST_KINDS` / `PERSON_REQUEST_KINDS`; codegen rerun.
The generic `server_requests` feature now means the kinds a person answers (TUI, CLI, ACP never get
preview requests); the desktop window advertises `server_requests.preview.act|read`
(`CLIENT_FEATURES`) and its `ServerRequestStore` routes them to `windowPreviewAutomation` (never a
card): the window showing the session's preview answers, others decline through the channel (4404);
`declineAnswer` covers the new kinds. Every window declining settles the request undeliverable with
`error.data.indus_code = server_request.not_shown` → `PreviewDeclined`; no capable window →
`PreviewUndelivered`; a timeout → `TimeoutError`. The tools build `BrokerPreview` over the session's
broker (no new handler extra) and exist only in daemon sessions a desktop window created
(`SessionMutations.shape` → `with_preview`: a `SessionRegistry` with the tools, `desktop-preview` in an
explicit selection; the stored row's source keeps it after a reload or restart). Tests:
`tests/unit/service/test_preview_requests.py`, `apps/desktop/src/gateway/previewRequests.test.ts`,
`packages/client/src/channel.test.ts` (decline answer). ACC-N12-2 updated.
