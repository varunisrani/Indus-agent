# Task: heartbeat and reconnect backoff (P12-04) Phase: 12 Owner: wave-7 builder K

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/shared/src/json-rpc-channel.ts`
(the heartbeat part: interval, deadline, outstanding pings, what counts as liveness),
`apps/shared/src/reconnect-backoff.ts` (delay formula, jitter option). Research `05-tui.md` §3.2,
§3.24, §3.30, trap R05.9. Evidence: none.

What it does (own words): a link can die without a close event (sleep, proxy idle cut). When the
server's ready event says it answers pings, the client pings every 15,000 ms; *any* inbound frame
proves the peer alive (a mid-turn stream that never answers pings must not be killed, R05.9); 45,000
ms with no inbound frame closes the transport so the reconnect path runs. At most 8 pings may be
unanswered; a ninth is not sent. Reconnect waits grow 1 s, 2 s, 4 s … capped at 30 s, no jitter for
the TUI (one process has nobody to collide with); desktop can ask for full jitter (a random wait
between 0 and the step). Every scheduled attempt is announced as
`transport.reconnecting { attempt, delay_ms }`, and reaching ready resets the ladder.

Inputs → outputs: `new Heartbeat({clock, sendPing, onDead})`, `.start()`, `.noteInbound()`,
`.stop()`; `backoffDelay(attempt, options)`; `new Reconnector({clock, reconnect, onReconnecting})`,
`.schedule()`, `.markReady()`, `.cancel()`.

Files: `packages/client/src/heartbeat.ts`, `backoff.ts`, `heartbeat.test.ts`, `backoff.test.ts`.

Edge cases / failure modes:
- the deadline is measured from the last inbound frame, so it fires at exactly 45,000 ms of
  silence (a separate deadline timer, not the next ping tick).
- a ping send that throws counts as dead at once.
- an unanswered ping that finally settles (reply or error frame) frees its slot.
- `schedule()` while an attempt is already pending is a no-op (one owner of the timer).
- attempt numbers above 30 do not overflow the doubling.
- stop/cancel clear every timer (no stray wake-ups after close).

Our design differences / decisions:
- the outstanding cap stops sending (the ninth is skipped) instead of forgetting the oldest ping.
- the ping method name and the ready flag are not in the generated protocol yet: stubbed as
  `gateway.ping` and `gateway.ready { heartbeat }` (see protocol gaps in the stdio note).
- delays are 1-based attempts (`attempt 1 → 1,000 ms`), matching the announced attempt number.

Tests we will write: deltas every second keep a pong-less link alive for 10 minutes; silence closes
at 45,000 ms exactly; ninth ping not sent; backoff 1,2,4,8,16,30,30 s; reset after ready; full
jitter stays within the step; schedule twice → one timer.

## Decisions for the lead
- The ninth ping is skipped (row text) rather than forgetting the oldest outstanding ping.
- Backoff attempts are 1-based; the TUI profile is the default (1 s, ×2, cap 30 s, no jitter);
  `{jitter: "full"}` gives desktop a random wait in [0, step).
- `IndusClient` reconnect is off by default: for the stdio child the app's recovery (P12-27, budget
  3 per 60 s) decides restarts; P17's WebSocket transport turns `reconnect` on.
- Follow-up for P17: the reference does not reset the ladder for a link that opens and dies at once
  (a 5 s "stable open" rule); ours resets on ready, which a ready-then-close proxy could loop on.

Compare checklist (filled after the rewrite; reopened the two files above):
- [x] any inbound frame keeps a pong-less link alive for 10 minutes (test:
  `packages/client/src/heartbeat.test.ts::stays alive for 10 minutes on inbound traffic alone…`)
- [x] silence closes at exactly 45,000 ms after the last frame (test: `closes after exactly 45,000 ms
  of silence`)
- [x] ninth ping not sent (test: `does not send a ninth ping while eight are unanswered`)
- [x] pings only after ready advertises them; a stalled peer is closed (test: `client starts pinging
  only when ready advertises it…`)
- [x] backoff 1,2,4,8,16,30,30 s; full jitter inside the step (tests: `backoff.test.ts::doubles from
  1 s and stops at 30 s`, `full jitter stays inside the step`)
- [x] each attempt announced, one timer, reset after ready (tests: `announces each attempt…`,
  `drives client redials along the ladder until ready`)
- Deliberate differences: the reference checks the deadline on the ping tick (so it can fire up to
  one interval late) and can count only answers as liveness; ours fires exactly on silence and always
  counts any frame (R05.9). Reference default backoff (300 ms base, 15 s cap, jitter on) is its
  desktop profile; ours defaults to the TUI profile.
- Not taken: the stable-open rule (see decisions; P17).

## Status

Done (2026-10-19). `packages/client/src/{heartbeat,backoff}.ts` + 2 test files (9 tests).
Shared: none. The ping method and ready flag are stubs (see the stdio note's protocol gaps).
