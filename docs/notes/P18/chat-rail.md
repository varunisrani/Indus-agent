# Task: Chat right rail (P18-27) Phase: 18 Owner: builder P2 (wave 32)

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `web/src/components/ChatSidebar.tsx` (header
comment, the two sockets, imports), `web/src/lib/events-reconnect.ts`, `web/src/lib/eventsFeedClient.ts`.
Our side: `src/indus/web/ws/events_fanout.py` (`/api/events` subscriber contract), `src/indus/web/ws/
rpc_sidecar.py`, `src/indus/web/routes/{models,files,sessions}.py`, `packages/protocol/src/generated/
{catalog,types}.ts` (event names, `SessionInfo`, `MessageComplete`), `apps/tui/src/slash/localCommands.ts`
(`/reasoning`, `/image`), `apps/tui/src/pickers/modelPickerState.ts` (effort words).
Evidence: none (prework against the fake WS server and the typed REST mock).

What it does (own words): beside the terminal, a rail shows what the terminal cannot: which model is
answering, the profile's recent sessions to jump between, where a new chat will start, the reasoning
effort, a copy button for the last answer and an image drop/paste target. It listens to the chat's
event feed (`/api/events?channel=<the host's channel>`), which the backend fans out from the PTY
child. The model badge prefers the model named by the feed's latest `session.info` (the model really
answering, which a provider fallback can change) and otherwise shows the configured model from
`GET /api/model/info`. The rail never talks to the chat itself except by typing into the terminal the
same slash commands a person would (`/reasoning <level>`, `/image <path>`), so there is still one chat
implementation (R09.1). Anything the rail does can fail without touching the terminal: it sits in an
error boundary, its sockets and queries are its own, and its notices stay inside it. On screens
narrower than 1,024 px it folds into a slide-over opened from a "Details" button.

Inputs → outputs: feed frames (protocol events, or JSON-RPC notifications whose method is the event
type), `/api/model/info`, `/api/sessions?order=recent`, `/api/files` + `/api/files/upload`, local
storage → badge, session list, workspace choice for the next new chat, keystrokes into the PTY.

Files: `apps/web/src/chat/{ChatRail.tsx,eventsFeed.ts}`, tests `apps/web/src/chat/{eventsFeed.test.ts,
ChatRail.test.tsx}`.

Edge cases / failure modes:
- Feed reconnect: 1 s, 2 s, 4 s … capped at 30 s, at most 15 attempts, then stop with a "reconnect or
  reload" notice and a Reconnect button; the counter resets after an open. Each attempt (ticket fetch
  plus handshake) has 15 s. 4401 / 4403 stop at once with "your login expired, reload the page"; 4400
  (bad channel) stops quietly. Frames that are not JSON objects with an event type are ignored.
- Session list: title, else preview, else the id; relative time of last activity; message count;
  source. A click resumes that session in the same terminal (new PTY URL with `resume=<id>`); "New
  chat" starts fresh (new attach token, `fresh=1`, picked workspace as `cwd`); Refresh refetches.
- Workspace: remembered per profile in local storage (selection and a most-recent-first list of up to 8
  paths), plus the live session's folder from `session.info`; "Other path…" takes an absolute path
  (relative input refused in the form; the server refuses a missing folder with red text + 1011). It
  only applies to the next new chat.
- Reasoning picker: minimal / low / medium / high / extra high / off; shows the live level from
  `session.info`; refused with a notice while the terminal is not connected.
- Copy last answer: the text of the latest `message.complete`; disabled until there is one; clipboard
  failure shows a notice.
- Image paste/drop: images only; uploaded through the Files API to `<files root>/indus-chat-uploads/
  <time>-<safe name>`, then `/image <path>` is typed into the terminal; failures show in the rail.

Our design differences / decisions:
- The rail drives reasoning effort and image attachment through the TUI's own slash commands instead of
  a second RPC path, so the terminal shows the result exactly as the TUI does.
- No model picker in the rail (the Models page, P18-24, owns model changes).
- The JSON-RPC sidecar (`/api/ws`) is not used by the rail yet: nothing in our row needs connection
  state beyond the feed and the PTY phase.

Tests we will write:
- eventsFeed: URL has the channel and no credential; delays 1 s → 30 s; stops after 15 attempts; 4401 and
  4403 stop with the reload notice; 15 s connect timeout counts as a failed attempt; open resets the
  counter; frame parsing (event object, JSON-RPC notification, junk).
- ChatRail (with ChatHost, fake terminal, fake WS server, typed REST mock): badge from `session.info`
  beats the configured model; configured model when the feed is silent; a session click redials the PTY
  with `resume=<id>`; New chat dials with `fresh=1`, a new attach token and the remembered workspace as
  `cwd`; a throwing rail leaves the terminal mounted; a 4403 feed close shows the reload notice;
  reasoning picker types `/reasoning high`; copy last answer.

## Decisions for the lead
- "Projects + discovered git repos" for the workspace picker need a backend list route that does not
  exist in the generated route list (no `/api/projects` or repo discovery in P18-11..16). The picker
  offers remembered paths, the live session's folder and "Other path…" today; add the route and feed it
  into `workspaceOptions` (ChatRail.tsx) as a follow-up.
- Uploaded chat images go to `indus-chat-uploads/` under the Files root (the user's home, or the locked
  root); nothing cleans them up yet.
- Feed frames: the TUI publisher does not exist yet; the rail accepts both a bare protocol event
  (`{type, …}`) and a JSON-RPC notification (`{method, params}`), so whichever the TUI owner picks works.

Compare checklist (filled after the rewrite; reopened `web/src/components/ChatSidebar.tsx` header and
sockets, `web/src/lib/events-reconnect.ts`, `web/src/lib/eventsFeedClient.ts`):
- [x] Feed backoff 1 s → 30 s, 15 attempts, 15 s connect budget incl. ticket (tests: `eventsFeed.test.ts::waits
  1 s, 2 s, 4 s … capped at 30 s and stops after 15 attempts`, `::gives each attempt 15 s, a hung ticket included`)
- [x] 4401/4403 terminal with a reload notice (tests: `eventsFeed.test.ts::stops at %i with the reload notice`,
  `ChatRail.test.tsx::a 4403 on the feed shows the reload notice and the terminal stays open`)
- [x] Model badge: live `session.info` first, configured model second (tests: `ChatRail.test.tsx::shows the
  configured model until the feed names the model answering`, `::says so when no model is configured and the feed
  is silent`)
- [x] Rail failures never reach the terminal (test: `ChatRail.test.tsx::a rail that throws leaves the terminal
  mounted and connected`)
- [x] Session click resumes in place, same attach token and channel (test: `ChatRail.test.tsx::lists recent chats and
  a click resumes in place with a new PTY URL`)
- Deliberate differences: the reference retries the feed on every code but 1000/4401/4403; we also stop
  on 4400 (a bad channel id never heals) and retry 1000. The reference's model picker and reload prompt
  live in its sidebar; ours stay on the Models page.
- Not taken: the JSON-RPC sidecar badge and credential warnings, the flap guard that refills the budget
  only after 10 s of uptime (an open resets our counter at once) — candidates for P18-30.

## Status

Done (2026-10-20) as prework against the fake WS server and REST mock; workspace discovery route is a
follow-up (above).

Integration (wave 32, P18c integrator, 2026-10-21): decisions numbered D-16.654. Still open: the TUI's publishing to `/api/pub` (TUI owner, so the rail has no live feed against the real server yet) and the projects + discovered-repos route for the workspace picker.
