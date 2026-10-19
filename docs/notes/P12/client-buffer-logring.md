# Task: client event buffer and log ring (P12-03) Phase: 12 Owner: wave-7 builder K

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `ui-tui/src/gatewayClient.ts`
(the pre-subscribe buffers, the subscribe/drain path, what a transport reset clears, the log ring
and its line cut, URL redaction), `ui-tui/README.md` (the stdio framing paragraph and the event
table's synthetic rows). Research `05-tui.md` §3.2, §3.30, trap R05.2. Evidence: none.

What it does (own words): the renderer mounts after the child may already have spoken. Events that
arrive before anyone subscribes are kept (newest 2,000, oldest dropped) and handed over in order to
the first subscriber; from then on events go straight through. The "someone subscribed" flag is
set once and never cleared, so after a reconnect new events still flow without a second
subscribe (clearing it once stranded every later event in the buffer). Server questions that
arrive before the handler table exists are held the same way and answered once it is installed.
The log ring keeps the last 200 diagnostic lines (stderr, lifecycle, protocol noise) for `/logs`
and for the start-timeout payload; each line is cut to 4,096 bytes with a visible marker, and URLs
lose their query string, fragment and user-info before the line is stored, because connection URLs
carry tokens.

Inputs → outputs: `buffer.push(event)`, `buffer.subscribe(listener)` → unsubscribe;
`ring.push(line)`, `ring.tail(n)`, `ring.lines()`;
`redactUrls(text)`, `cutLine(text, maxBytes)`.

Files: `packages/client/src/ring.ts`, `eventBuffer.ts`, `logRing.ts`, `eventBuffer.test.ts`,
`logRing.test.ts`; the early server-request hold lives in `channel.ts` (P12-02).

Edge cases / failure modes:
- 2,001 early events → the first is dropped, the rest drained oldest first.
- listener throws → the others still get the event (error reported to `onListenerError`).
- unsubscribe of the last listener → events are not buffered again (the flag stays set) — they
  are dropped; a later subscriber only sees new events.
- transport reset → buffered-but-undelivered events of the old transport are cleared, flag kept.
- multi-byte UTF-8 near the cut → never split inside a character.
- malformed URL text (no host) → still stripped of `user:pw@` and `?query` by pattern.

Our design differences / decisions:
- one small `Ring<T>` used by both the event buffer and the log ring.
- the stored line including the marker never exceeds the cap (marker says how many bytes went).
- drain is synchronous inside `subscribe` (JS is single-threaded, so no live event can overtake
  the drained ones); the renderer subscribes from an effect, not during render.

Tests we will write: 2,001 events keep the newest 2,000 in order; early server request answered
after install; reconnect then new event delivered without re-subscribe; 5,000-byte line cut with
marker; `?token=x` and `user:pw@` redacted; ring keeps 200; UTF-8 boundary.

## Decisions for the lead
- Events buffered before the first subscriber are kept across a reconnect (not cleared), so a
  `transport.exit` that happened before mount still reaches the renderer; history stays in order
  (old events, exit, reconnecting, new ready). Change: clear on a new transport if stale replays
  confuse the renderer.
- Drain is synchronous inside `subscribe`; the renderer subscribes from an effect. Change: defer to
  a microtask if React complains about updates during commit.
- Every URL in every log line is redacted (not only the connection URL); fragments too.

Compare checklist (filled after the rewrite; reopened the two files above):
- [x] 2,001 early events keep the newest 2,000 in order (test:
  `packages/client/src/eventBuffer.test.ts::keeps the newest 2,000 of 2,001 early events, in order`)
- [x] early server request answered after handler install (test: `eventBuffer.test.ts::answers a
  server request that came before the handlers were installed`)
- [x] reconnect then new event delivered without re-subscribe (test: `eventBuffer.test.ts::delivers
  events after a reconnect without a second subscribe`)
- [x] 5,000-byte line cut with marker; UTF-8 safe (tests: `logRing.test.ts::cuts a 5,000-byte
  line…`, `never splits a multi-byte character`)
- [x] `?token=x` and `user:pw@` redacted, before the cut (tests: `redacts query strings…`,
  `redacts before cutting…`)
- Deliberate differences: the reference appends its marker after 4,096 kept bytes (line can exceed
  the cap) and defers the drain to a microtask; ours keeps line + marker within the cap and drains
  synchronously. The reference clears the early buffer on a transport reset; ours keeps it.
- Not taken: persisting lifecycle lines to a crash log file (the TUI app may add it in P12-27).

## Status

Done (2026-10-19). `packages/client/src/{ring,eventBuffer,logRing}.ts` + 2 test files (12
tests). Shared: none.
