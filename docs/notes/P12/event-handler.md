# Task: event handler and flush scheduler (P12-10) Phase: 12 Owner: wave-8 builder R1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`):
`ui-tui/src/app/createGatewayEventHandler.ts` (the event switch, the session filter in front of it,
`handleReady`, the status/notice/stderr helpers), `ui-tui/src/config/timing.ts`,
`ui-tui/src/config/limits.ts`. Research `05-tui.md` §3.5, §3.13, §3.30, traps R05.6. Evidence: none.

What it does (own words): one function turns every event the client delivers into store updates.
It first drops events of a session other than the focused one, and events whose `seq` it has
already seen for that session (replays). Reply text is appended raw — a rendered fragment never
replaces it. Screen updates are batched: the live text is copied into the turn store at most every
16 ms, every 80 ms while the user types (typing counts as idle 250 ms after the last key), every
96 ms while scrolling. Status kinds go to the status slot and, for the noisy ones, the activity
lane, and the status returns to "working…"/"ready" after 4 s (6 s for goal updates). Notices wait
while a turn runs and show at its end; their timer starts when they become visible; a later notice
with the same key replaces or (empty text) clears it. A withdrawn server request closes its card;
a sudo/secret card that timed out leaves a hint on how to try again. Bridge stderr lines that look
like failures become one de-duplicated activity warning; protocol noise shows "protocol warning"
for 4 s.

Event table (our protocol, `packages/protocol` `EVENTS` + client `transport.*`):

| Event | Handling |
|---|---|
| `gateway.ready` | theme name applied; `onReady` hook (session bootstrap is P12-27); seq memory reset |
| `session.info` | `ui.info`, title, `running` |
| `session.title` | `ui.title` |
| `session.usage` | `ui.usage`, replaced only when a counter changed |
| `turn.started` | controller `start` |
| `message.delta` | controller `delta` (raw text) |
| `reasoning.delta` | controller `reasoning` (buffer trimmed to 60,000 when over 80,000) |
| `tool.started` / `tool.progress` / `tool.completed` | controller tool rows, trail, diff segment |
| `todo.updated` | `turn.todos` |
| `status` | status slot; `waiting`/`retrying`/`compressing` also activity; restore 4,000 ms |
| `notice` | held while busy, TTL when visible, key replace/clear |
| `server_request.cancelled` | close the card; sudo/secret `timed_out` → retry hint |
| `turn.done` | controller `done` (outcome) |
| `message.complete` | controller `complete` (final text, dedupe, failure card) |
| `goal.update` | status brief + system line, restore 6,000 ms |
| `loop.update` | activity line (replaced per loop) |
| `heartbeat.fired` | system line |
| `learning.review.done` | one system line per summary line |
| `transport.stderr` | failure-looking lines → one de-duplicated activity warning |
| `transport.protocol_error` | status "protocol warning" 4,000 ms; first time an activity note |
| `transport.start_timeout` | status + system line with the stderr tail |
| `transport.exit` | controller reset, status, system line (recovery is P12-27) |
| `transport.reconnecting` | status |
| later / dropped | voice, moa, connection, billing, reaction, subagent tree, replay gap, skin events: not in our protocol yet (P12 scope: dropped; subagents later with P10 delegation events) |

Inputs → outputs: `ClientEvent` → `uiStore`, `turnStore`, `overlayStore`, `transcriptStore`.

Files: `apps/tui/src/events/{handleEvent,flushScheduler,timing}.ts`, tests `handleEvent.test.ts`,
`flushScheduler.test.ts`.

Edge cases / failure modes:
- events before the session id is known pass (the bootstrap needs `session.info`).
- `seq` restarts when the bridge restarts: the memory is cleared on `gateway.ready` and exit.
- a delta after a local interrupt is ignored (controller).
- an RPC failure (a rejected call) is never a turn end: `reportRpcFailure` writes a system line and
  leaves busy alone. Our protocol has no stamp-less `error` event.

Our design differences / decisions:
- our protocol has no `message.interim`, `rendered`, `request.cancel`, `notification.*`: the closest
  events are mapped (table above); the controller still has an `interim()` entry for later.
- notice TTL: info/warning 8,000 ms from when visible, error sticky until replaced or cleared.

Tests we will write: every fixture of P12-06 replayed with a golden final snapshot; a `rendered`
field never reaches the buffer; foreign-session event dropped; duplicate seq dropped; status
restore; notice hold/TTL/key clear; cancel closes card + hint; stderr de-dup; protocol warning;
flush timings 16/80/96 ms with fake timers; 85,000-char reasoning trimmed to 60,000.

Compare checklist (filled after the rewrite; reopened `createGatewayEventHandler.ts`, `config/timing.ts`,
`config/limits.ts`):
- [x] session filter before the switch; plus a seq filter of ours (tests: `handleEvent.test.ts::drops
  events of a session other than the focused one`, `…::drops a repeated seq…`)
- [x] raw delta text only, never `rendered` (test: `…::appends raw text and never takes a rendered field`)
- [x] flush 16/80/96 ms, typing idle 250 ms (tests: `flushScheduler.test.ts`)
- [x] reasoning 60,000 after 80,000; pulse 700 ms; preview 160 (tests: `…::trims an 85,000-character…`,
  `…::lights the reasoning indicator…`, `controller.test.ts::previews the last 160 characters…`)
- [x] status restore 4,000 / goal 6,000 (tests: `…::shows a transient status and restores it after 4 s`,
  `…::restores a goal status after 6 s…`)
- [x] notices held while busy, TTL from visible, clear by key (tests in `describe("notices")`)
- [x] withdrawn request closes card; sudo/secret timeout hint (tests in `describe("server request withdrawals")`)
- [x] stderr failure lines → one activity warning; protocol warning 4 s, first-time note, preview 120
- [x] every fixture replayed to a golden snapshot (`__snapshots__/handleEvent.test.ts.snap`)
- [x] limits: activity/trail 8, live tail 16,000 chars / 240 lines, interrupt cooldown 1,500 ms
  (`events/timing.ts`); verbose trail caps, long-run charms and delegation caps belong to later tasks.
- Deliberate differences: our protocol's event set (table above); a usage tick replaces the counters
  when one changed instead of a field merge; RPC failures are reported by `reportRpcFailure`.
- Not taken: voice, wake word, moa, connection cards, billing, reactions, subagent tree, replay gap,
  skin change (no such events in our protocol yet); the "no provider configured" setup panel.

## Status

Done (2026-10-19). `apps/tui/src/events/{handleEvent,flushScheduler,timing}.ts`, tests
`handleEvent.test.ts` (27), `flushScheduler.test.ts` (5). Shared: none.
