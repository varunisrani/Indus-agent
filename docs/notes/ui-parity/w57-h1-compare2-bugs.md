# Task: Second-compare bugs: docs theme, pane widths, messaging labels, error card, web model list (W57-H1) Phase: ui-parity Owner: builder w57h1

Status: Done (2026-10-25) for items 1, 3, 4 and 5; item 2 was not a width bug (measured, pinned by a
test, no layout change). Uncommitted in the w57h1 worktree for the lead.

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): no reference source opened; the
second compare's gap list and screenshots were the input.
Evidence: reference and Indus screenshots of the second compare (never in the repo): web `docs`,
`docs-direct-url`, `model-picker`; desktop `right-sidebar`, `files-pane`, `review-pane`, `messaging`,
`error-state`, each `--light` and `--dark`.

What it does (own words):
- Web Documentation page: the panel was a fixed white sheet with navy text (`bg-white`,
  `[color-scheme:light]`, hex colours), so every dark theme showed a bright block. It now uses the
  theme's surface, line and ink tokens like other cards (`/docs` and the Docs nav item are one
  page, so both slugs are fixed).
- Desktop pane widths: the gap list said the Files column and the Ctrl+J right sidebar draw about
  110 px wide. Measured in the running app at 1440x900 they draw 228 px (chat 913 px, Review 367
  px): the split sizes are relative weights, 236 of 1180, over a 1143 px chat area, so D-16.1272's
  width is applied. The 110 figure is the half-scale side-by-side sheet. The terminal prompt wraps
  because the kit's working folder path is about 110 characters long and the xterm screen gets 164
  px of the 228 (the shell rail and padding take the rest). Nothing changed in the layout; a test
  pins the four column shares.
- Desktop Messaging: field labels come from the env key's ending; keys with no known ending showed
  the raw env name in mono (`INDUS_TELEGRAM_ALLOW_BOTS`). Every key the gateway catalogue sends now
  has a label (and help where useful), including the bot-sender policy ("Other bots": none,
  mentions, all) and group allowlists; a key added later reads as words made from its name, with
  `INDUS_` and the platform prefix dropped, never the env name.
- Desktop chat error card: Retry was offered only when the backend marked the failure transient,
  so a provider refusal (HTTP 400) had no Retry, and Open logs only showed for the connection,
  disk and runtime layers. Retry is now offered on every layer but the connection one whenever the
  host can send the failed turn again (the window's chat slot re-sends the turn's message as a new
  input, the existing path); a view with no such slot (the HUD) shows no Retry. Open logs (the
  bridge's log folder, the existing action) is on every layer.
- Web model picker: our REST API lists providers but no vendor's models. It can ask an
  OpenAI-compatible endpoint for its `/models` (`POST /api/model/test-endpoint`). The picker now
  does that for a custom or local endpoint: a saved `providers.<name>` entry with a base URL (and
  its `key_env`), or the main model's own `model.base_url` when its provider is shown. The list
  joins the model in use and the entry's saved models; the typed model id stays as the fallback,
  and a hint line says whether the list came, is coming, or why it did not.

Inputs → outputs: theme tokens → panel colours; layout presets → column shares; gateway catalogue
env keys → field labels; failure layer + host retry slot → card actions; config + test-endpoint →
the picker's model column.

Files: apps/web/src/pages/DocsPage.tsx (+test); apps/desktop/src/panes/layoutStore.test.ts, apps/desktop/src/chat/Transcript.test.tsx;
apps/desktop/src/app/messaging/fieldCopy.ts (+new test), MessagingPage.tsx,
apps/desktop/src/app/manage/strings.en.ts; apps/desktop/src/chat/failure.ts, ErrorCard.tsx,
Transcript.tsx, ChatView.tsx, errorActions.test.tsx (new), transcriptLook.test.tsx,
messageStream.test.ts; apps/web/src/ui/ModelPickerDialog.tsx, apps/web/src/lib/api.ts,
apps/web/src/lib/restTypes.ts, apps/web/src/testing/mockApi.ts, apps/web/src/pages/ModelsPage.test.tsx.

Edge cases / failure modes:
- Picker: endpoint down or refusing the key → the backend's sentence plus "Type the model id
  instead."; saved models still listed; vendor providers make no request at all.
- Picker: the main endpoint is asked without a key (the API takes only a named `key_env`, and the
  main model names none); an endpoint that needs one answers "needs a key" and the field remains.
- Error card: a turn with no prompt found in the transcript makes Retry do nothing (the slot's
  existing rule); Open logs outside the desktop app has no bridge and does nothing (the card only
  renders in the desktop app).
- Messaging: `_PHONE_NUMBER_ID` before `_PHONE_NUMBER`, `_CLIENT_SECRET` before `_SECRET`,
  `_GROUP_ALLOWED_USERS` before `_ALLOWED_USERS` (first match wins).

Our design differences / decisions:
- W57-H1-1: the Docs panel follows the theme (it imitated an embedded white docs site we do not
  have).
- W57-H1-2: no change to side widths; the 110 px report is a measuring slip (half-scale sheet).
  Making the xterm wider would mean hiding the shell rail (new/close terminal), left as is.
- W57-H1-3: unknown messaging keys get a derived label, not the env name; the env name stays in
  the label's tooltip and the input's accessible name (E2E and tests select by it).
- W57-H1-4: Retry follows the host's ability to resend, not the backend's transient flag; Open
  logs on every layer. No Send diagnostics button (the card has Copy error details; diagnostics
  live in Settings › logs) and no footnote.
- W57-H1-5: the web picker lists models only where our backend can: custom/local endpoints via
  `test-endpoint`. No backend change.

Tests we will write: docs panel class tokens; side column shares; every catalogue key labelled;
failure actions per layer; card pills with/without a retry host; ChatView Retry → slot and Open
logs → bridge; picker lists endpoint models, failure hint, vendor provider makes no request.

## Decisions for the lead
- W57-H1-2: the gap line "about 110 px" should read "228 px (the reference about 237 px)"; the prompt wrap
  is the long working folder path. If a wider terminal is wanted, the shell rail could collapse
  into the tab strip.
- W57-H1-5: a real per-provider model list for vendor providers would need a backend route (for
  example a REST twin of the daemon's `model.options`); not added (never invent backend features).

Compare checklist (filled after the rewrite):
- [x] docs / docs-direct-url dark: no white block (test: `DocsPage.test.tsx::draws the panel with theme tokens…`)
- [x] right-sidebar / files-pane width measured 228 px, review 367 px (test: `layoutStore.test.ts::Ctrl+J's Files over Terminal column…`)
- [x] messaging: "Other bots" label with help (test: `fieldCopy.test.ts::labels every key the gateway catalogue sends`)
- [x] error-state: Retry · Choose a model · Open settings · Open logs · Copy error details (tests: `errorActions.test.tsx`, `transcriptLook.test.tsx`)
- [x] web model-picker: fake-model + fake-model-1 listed for the custom endpoint (test: `ModelsPage.test.tsx::lists a custom endpoint's own models…`)
- Deliberate differences: no Send diagnostics or footnote on the error card; vendor providers keep
  the typed field.
- Not taken: brand icons, Telegram QR and setup-guide block on Messaging (not in this slice).
