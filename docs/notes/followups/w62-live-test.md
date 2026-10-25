# Task: live run of every surface against a fake ChatGPT, side by side with the reference (W62) Phase: followups Owner: Agent 4

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`, to compare behaviour only):
`web/src/App.tsx` (route list), `hermes_cli/web_server_chat.py` (`_resolve_chat_argv`: where a
fresh chat starts), `ui-tui/src/domain/details.ts` (section defaults),
`ui-tui/src/components/messageLine.tsx` (finished turns: thinking shown folded),
`hermes_state_sessions.py` (`INTERNAL_LISTING_SOURCES`).
Evidence: none (screenshots and captures of both products stayed in the session scratchpad).

What it does (own words): the owner asked for the product to be run and every page, button and
feature tried with a fake ChatGPT, bugs fixed, and the result compared with the reference. Both
products ran against one free-form OpenAI-compatible fake (Chat Completions + Responses, `run:` →
terminal tool call, `think` → reasoning deltas) kept outside the repo: CLI one-shot and tool call,
`indus dashboard` (19 pages crawled, every safe button clicked), web Chat (reply, tool row,
approval allow and deny, `/help`, `/usage`, `/model`, `/new`), cron create and run now, webhook
route, profile create, key save, skill create, ChatGPT device-code sign-in start, the desktop
Electron lane and the unit suites.

Fixes:
- Web Chat asked `GET /api/sessions/<id>` for a brand-new chat before it was stored (a 404 and a
  console error on every new chat): the header's title lookup now waits for the first answer, or
  runs at once for a resumed session.
- TUI: a finished turn dropped its reasoning; the reference keeps it on the reply, folded. Our
  finished replies now carry it behind one `▸ thought` row (nothing when the thinking section is
  hidden); the live turn is unchanged.
- TUI pager (`/help` and other long output): a line wider than the card wrapped to two rows but was
  counted as one, so the page was taller than its slot, the layout squeezed a row to zero height
  and the next row was drawn over it (`/reasoning` vanished under `/fast`). The pager now wraps
  lines to the card width itself, pages over screen rows and never lets a row or the footer wrap.
- Web System page: the hidden restore-zip picker had no positioned parent inside the scrolling
  `main`, so it stretched the whole document by 260 px (a second page scrollbar). Its row is now
  the positioning parent.
- Web client: `GET /api/tools/keys` (wave 60) is profile scoped on the server but was missing from
  the client's scoped families (the route-list test was red).
- Desktop Settings › Providers › Accounts: wave 61's ChatGPT card hid the "Set up a provider" card,
  so onboarding (custom endpoint, other providers) could no longer be re-run from Settings (the
  Electron lane was red). The setup card shows again beside the ChatGPT card.
- `src/indus/protocol/field_docs.json` regenerated (stale line numbers after the Windows gateway
  commit; the generated-twins guard was red).

Checked and left as they are (deliberate): a fresh web chat starts in the profile's home folder,
not the dashboard's launch folder (P18-08); one-shot sessions sit under Automation, not Chats
(`AUTOMATION_SOURCES`); no host facts (OS, Python, uptime) on System yet (parked W59-K2).

Files: `apps/web/src/chat/ChatHost.tsx`, `apps/web/src/chat/ChatRail.test.tsx`,
`apps/web/src/lib/api.ts`, `apps/web/src/pages/system/OperationSections.tsx`,
`apps/tui/src/{app/App.tsx,components/MessageRow.tsx,components/Transcript.tsx,state/transcriptStore.ts,turn/controller.ts}`
and tests, `apps/tui/src/cards/{Pager.tsx,CardView.tsx,Pager.test.tsx}`,
`apps/desktop/src/settings/pagesB/providers.tsx`, `chatgptAccount.test.tsx`,
`src/indus/protocol/field_docs.json`.

Compare checklist:
- [x] fresh chat not looked up before its first answer (test: `ChatRail.test.tsx::a fresh chat is looked up in the store only after its first answer`)
- [x] reasoning kept on the first reply of its turn (test: `controller.test.ts::keeps the turn's reasoning on its first reply`)
- [x] finished reasoning folded, hidden with the section (test: `Transcript.test.tsx::folds a finished reply's reasoning into one row, or hides it`)
- [x] pager counts wrapped rows (tests: `Pager.test.tsx::wraps long lines into rows at word breaks`, `::counts wrapped rows, so no row is drawn over another`)
- [x] setup card beside the ChatGPT card (test: `chatgptAccount.test.tsx::keeps the provider setup card beside the ChatGPT card`; Electron `chat.electron.test.ts::Settings › Providers re-runs onboarding …`)
- Deliberate differences: the reference shows a token estimate on the folded thinking row; we show
  none (no token tallies, P12 tool-trail note).

## Status

Done (2026-10-25).
