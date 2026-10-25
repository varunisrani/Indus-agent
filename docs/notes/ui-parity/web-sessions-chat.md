# Task: Web Sessions + Chat parity (W52-a) Phase: ui-parity Owner: builder w52a

## Status

Done (2026-10-24). Sessions finished on the foundation and Chat rebuilt; both themes re-captured and
set beside the reference (`$SP/w52a/compare.pdf`, 9 of 10 slugs; the consent strip is N/A).

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): none of the reference source during this
task; the wave-52 spec `w52-specs/web-sessions-chat.md` (written from the reference Sessions page, its
source table, the delete/confirm dialogs, the shared-metrics strip, the Chat page, its side panel,
session list, workspace picker, copy-last button and model picker dialog) is the starting point.
Evidence: the owner's screenshots of the running reference web dashboard, slugs `sessions`,
`session-detail`, `sessions-search`, `sessions-prune`, `empty-state`, `metrics-consent-banner`,
`chat`, `chat-exchange`, `chat-sidebar-closed`, `model-picker` (light and dark). None are in the repo.

What it does (own words): finishes the Sessions page on the wave-51 foundation (multi-pick source
menu, search as you type that replaces the list, history rows with a checkbox, live pill, inline
rename and icon actions, selection bar with bulk delete, a 20-row pager, a prune dialog that checks
by itself, role-tinted transcripts with amber tool strips) and rebuilds the Chat page frame: a rounded
terminal card, floating "copy last response" and "panel" buttons, a 240 px side panel that can be
collapsed (remembered per browser), a model card with a connection badge driven by the feed's phase,
a model picker dialog that saves the main model, and the header title following the session title.

Inputs → outputs: session list/stats/search/messages/rename/delete/export/prune endpoints, the chat
PTY and events feed, `/api/model/{info,options,set}` → the two pages; nothing new on the server.

Files: `apps/web/src/pages/SessionsPage.tsx`, `pages/sessions/{sources.ts,SourceIcon.tsx,
SourceMenu.tsx (new),SessionRows.tsx,SessionDetail.tsx,PruneDialog.tsx}`, `chat/ChatHost.tsx`,
`chat/ChatRail.tsx`, `chat/ModelPickerDialog.tsx` (new), `chat/terminal.ts` (terminal colours);
foundation files, additive: `shell/PageHeader.tsx` (`HeaderTitle` title override), `ui/Dialog.tsx`
(optional class slots on `Modal`), `index.css` (`--indus-terminal-bg/fg` + `bg-terminal`,
`text-terminal-fg` utilities; `mark` now amber text on amber 30 %); tests
`pages/SessionsPage.test.tsx`, `chat/ChatRail.test.tsx`, `e2e/dashboard.spec.ts`.

Edge cases / failure modes:
- search typed then cleared quickly → debounced, the stale answer is ignored (keyed by query);
- bulk delete hits a 409 → stops, keeps the rest selected, shows our conflict sentence;
- prune while the daemon runs → the dry run itself answers 409; the notice shows and Prune stays off;
- storage blocked → the side panel's collapsed state lasts for the page only;
- no per-provider model list on our server → the picker offers the current model plus a typed id.

Our design differences / decisions:
- W52-sc-1: the shared-metrics consent strip is N/A (no telemetry, D-16.49); the pressure banner keeps
  the foundation look.
- W52-sc-2: prune keeps the dry run but runs it by itself (on open and 300 ms after an edit).
- W52-sc-3: "Import sessions" shows disabled ("Not available yet"); no import endpoint here.
- W52-sc-4: the model picker lists our providers; the model column has the current model and a typed
  model id; Switch calls `/api/model/set` (scope main) and the side panel says to start a new chat.
- W52-sc-5: the chat feed moves up into the host so the copy button and the header title can read it;
  the rail gets the feed state as a prop.

Tests we will write: Sessions (source menu multi-pick, debounce search replaces list, selection bulk
delete, prune auto preview), Chat (panel collapse persists, copy overlay, phase badge, model picker
switch).

## Decisions for the lead
- see W52-sc-1…5 above.
- W52-sc-6: "Recent sessions" shows the 5 newest of the filter, not "the 5 newest that are not
  active": our `is_active` means "live" and stays on after the CLI exits (known backend bug), which
  would empty the card.
- W52-sc-7: the reasoning-effort card is always shown (we cannot tell whether a model supports it);
  the reference shows it only for models that do.
- W52-sc-8: the header title falls back to the stored session title (looked up after each answer)
  when the feed has not sent a `session.title` event.

Compare checklist (from the spec; L = light, D = dark):

Sessions (`sessions`)
- [x] L [x] D header: display title, count chip right of title, "Prune old sessions" outlined with archive icon at right
- [x] L [x] D content edges x=263/1407, top y=75, 16 px block gaps
- [x] L [x] D stats strip: 5 groups, 18 px values, green Active value, 12 px labels, 1 px border box 56 tall
- [x] L [x] D toolbar: Chats/Automation/All segmented (filled selected), source menu button with filter icon + chevron, Overview/History segmented, Import sessions button
- [x] L [x] D Recent sessions card: clock + wide caps title, divider, bordered rows (serif title, mono model meta, preview), outlined source badge with icon on the right
- [x] L [x] D colours match §0 (bg, fg, secondary, hairlines)
Session detail (`session-detail`)
- [x] L [x] D History rows 60 tall: checkbox, source icon, serif title, meta with gaps, badge + play/pencil/download/red trash
- [x] L [x] D expanded: divider, role-tinted blocks (user/assistant/tool fills from §3), bold coloured labels + time
- [x] L [x] D tool-call strip amber with chevron, name left, call id right
Search (`sessions-search`)
- [x] L [x] D search box replaces the view toggle, X clear inside, no Import button, no Search button
- [x] L [x] D result rows show snippet with amber marks; list replaced (no separate results card)
Prune (`sessions-prune`)
- [x] L [x] D 360 wide dialog, wide caps title (white / near-black), serif description, label + full-width number input, Cancel outlined + red Prune
- [x] L [x] D scrim: dark blurred / grey blurred
- [x] dry-run preview line works; daemon-running notice shows
Empty (`empty-state`)
- [x] L [x] D Automation selected, no view toggle, search + Import present, centred faded clock + "No sessions in this filter"
Consent strip (`metrics-consent-banner`)
- [x] N/A recorded (W52-sc-1) (no metrics feature); [ ] TopStrip not built (optional; follow-up)
Chat (`chat`, `chat-exchange`)
- [x] L [x] D terminal card: radius 8, black / #f5f8fc, shadow, 12 px padding, terminal fg colours
- [x] L [x] D copy last response floating button bottom-right
- [x] L [x] D side panel 240: X, model card (MODEL caps label, mono model + chevron, phase badge `live` green), SESSIONS + refresh, WORKSPACE + icon + refresh + select, New chat outlined, mono session rows with meta, current row accent
- [x] header title switches to the session title after the first turn; back to "Chat" on New chat
- [x] badge shows connecting/live/closed, never "answering"
Panel closed (`chat-sidebar-closed`)
- [x] L [x] D card full width, "panel" floating button top-right, state survives reload
Model picker (`model-picker`)
- [x] L [x] D 720x720 raised dialog, page-bg scrim, caps serif title, mono "current:" line, filter input with icon
- [x] L [x] D provider column 200 wide, selected row fill + 2 px bar, CURRENT tags; model column rows with check icon
- [x] L [x] D footer note left; Refresh Models / Cancel outlined; Switch filled (disabled until a change)
- [x] switching saves via `/api/model/set` and the rail shows the amber "start a new chat" notice
Checks
- [x] biome, tsc, vitest (web), web Playwright e2e, `python -m tooling.checks --require-ref` (similarity/banned/licenses/assets)
- [x] no reference strings longer than 4 words in UI copy; no Hermes/Nous names; no reference assets

- Deliberate differences: "Import sessions" is disabled (no import endpoint, W52-sc-3); every seeded
  session shows `live` (backend `is_active` stays on after exit, out of scope); the TUI inside the
  terminal is our own CLI's rendering; the model column lists only the current model plus a typed id
  (W52-sc-4); a reasoning card sits under the model card (W52-sc-7).
- Not taken: the messaging-platforms card above Recent sessions; the TopStrip restyle of the
  pressure banner; "Delete empty (N)" (no endpoint).

## Integration (wave 53)

W52-sc-1..3 and -6 are D-16.1193, W52-sc-4, -5, -7, -8 are D-16.1194. The picker moved to the shared `ui/ModelPickerDialog.tsx` (D-16.1205; `chat/ModelPickerDialog.tsx` removed, the chat rail owns the `api.model.set` request and keeps its "Start a new chat" notice). The auth e2e specs (`auth.spec.ts` ×3, `auth-gates.spec.ts` ×1) now wait for "No sessions yet": on a fresh stack the Recent sessions card shows only while the list loads, then the empty state replaces it (D-16.1209).
