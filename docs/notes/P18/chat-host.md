# Task: Chat host — xterm over `/api/pty` (P18-26) Phase: 18 Owner: builder P2 (wave 32)

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `web/src/pages/ChatPage.tsx` (module header,
imports, the connect effect: ticket phase, `onopen`, `onmessage`, `onclose`), `web/src/lib/pty-reconnect.ts`,
`web/src/lib/pty-resume-sanitizer.ts`, `web/src/lib/pty-attach-token.ts`, `web/src/lib/pty-close-copy.ts`,
`web/src/lib/pty-composition.ts`, `web/src/lib/chat-channel-id.ts`, `web/src/lib/xterm-webgl-gating.ts` (probe
part). Our side: `src/indus/web/ws/{pty_route,chat_gate,events_fanout}.py`, `src/indus/web/pty/{registry,
resize,channels}.py`, `apps/web/src/lib/wsUrl.ts`, `apps/web/src/shell/ChatHostSlot.tsx`, `docs/notes/P18/
{pty-registry,chat-ws-routes,spa-shell,rest-client}.md`, P18 LEAD-NOTES landing sections.
Evidence: none (prework against an in-memory fake PTY server; no capture of the running reference).

What it does (own words): the Chat tab is the real TUI (`indus --tui`) running in a PTY on the
dashboard host, drawn in the browser by xterm.js over the `/api/pty` WebSocket. The host is mounted
once (P18-17's slot) and only hidden on other tabs. Each mount picks its own channel id
(`chat-<uuid>`), which ties the PTY child to the right rail's event feed. Each browser tab has an
attach token in session storage (`indus.pty.token.chat`) that it claims with a Web Lock
(`indus.pty.attach.<token>`); a duplicated tab finds the lock held and mints its own token, so it
gets its own PTY instead of stealing the first tab's (the server closes a superseded socket 4409).
"Start new session" rotates the token so the old keep-alive PTY is not reattached. Each dial carries
`channel`, `attach`, `resume` (when one is asked for), `profile` (the switcher's selection), `cwd`
(the picked workspace, only for a fresh chat) and `fresh=1` (only on the first dial of a "new chat");
the credential rides only in the subprotocol list (`wsUrl.ts`). The socket has 8 s from the start of
the dial (ticket fetch included) to open, else the attempt counts as failed. On open the client sends
the in-band resize escape at once, again after two animation frames and once more when fonts have
loaded, and then every 20 s of silence as a keep-alive. For 30 s after a resume (explicit, or the
server's `{type:"resume"}` announcement) the output is sanitised: erase-line/erase-char sequences are
dropped and runs of 50+ blank lines become one (R09.8). Close codes decide what the person sees.

Inputs → outputs: window globals (token, base path, gated flag), `?resume=` on `/chat`, the profile
switcher, the rail's workspace / resume / new-chat actions → one PTY socket at a time; terminal bytes
in xterm; overlays and banners; keystrokes and resize frames out.

Files: `apps/web/src/chat/{ChatHost.tsx,ptySocket.ts,attachToken.ts,resumeSanitizer.ts,reconnect.ts,
terminal.ts}`, tests `apps/web/src/chat/{ptySocket,resumeSanitizer,attachToken}.test.ts`,
`apps/web/src/testing/fakePtyServer.ts`; shared: `apps/web/src/main.tsx` (one line: pass the lazy host),
`apps/web/package.json` + `pnpm-lock.yaml` (xterm packages).

Edge cases / failure modes:
- Close codes: 4401 in loopback → reload the page once (same session-storage guard the REST client uses,
  cleared by the next 2xx); 4401 again, or in gated mode → "login expired, reload" copy. 1011 → "could
  not start" overlay (the server printed the reason in red in the terminal) with "Try again". 4410 →
  "session ended" overlay with "Start new session". 4409 → quiet (a short muted line, no overlay, no
  retry). 4403 / 4404 / 4408 → explanatory copy. 1012 / 1013 / 1001 / 1006 / any unclean close →
  reconnect 250, 500, 1,000, 2,000, 3,000 ms, then stop with a "lost connection" overlay and a manual
  Reconnect button. A clean 1000 counts as the session ending.
- Reconnect never runs while the tab is hidden or the host is on another page: the connection parks
  ("paused") and starts one attempt when the page is visible and on `/chat` again, at most once per
  second however many visibility/focus/online events arrive. A stopped ladder only restarts by hand.
- Typing while not connected is refused with one notice per outage (never queued into a socket that
  will replay old keystrokes).
- A ticket fetch that hangs or fails, or a socket stuck in CONNECTING, is caught by the 8 s deadline and
  routed into the ordinary ladder.
- Reconnects reuse the first dial's query minus `fresh` (repeating `fresh` would wipe the channel's
  session file again); a learned implicit resume id is not added, so the keep-alive key stays the same
  and the server reattaches the same PTY (ring replay + Ctrl-L redraw).
- Sanitiser state across frames: a partial CSI at a frame end is held back, as is a trailing newline
  run (and a newline run just before a held partial escape, so erase codes inside a flood do not split
  it); erase codes are removed before blank runs are collapsed. When the window ends the held text is
  released; a held partial escape then goes out with the next frame. On close a held partial escape is
  dropped (it would wedge the parser of the next stream).
- WebGL: off for software rasterisers (llvmpipe, SwiftShader, softpipe, "software") and when no context
  can be made; the addon is disposed while the host is hidden or the tab is in the background and loaded
  again on return (browsers cap live GL contexts); a context loss falls back to the DOM renderer.
- Mobile: the bottom inset follows `visualViewport` so the soft keyboard does not cover the prompt; a
  composition commit that xterm did not deliver through `onData` is forwarded once (duplicates within a
  short window dropped); one-finger touch drags scroll the terminal.
- SGR mouse reports are never forwarded (the server already turns TUI mouse reporting off).
- Web Locks missing → session storage alone; storage blocked → a fresh token per document.

Our design differences / decisions:
- Framework-free `PtyConnection` (ptySocket.ts) owns the socket, ladder, deadline, keep-alive and
  sanitiser; `ChatHost` only wires it to xterm and React state. Every browser seam (socket factory,
  dial target, animation frame, fonts, reload, visibility) is a dependency, so tests run on fake timers.
- Keep-alive fires only after 20 s with no frame in either direction (the row's "while quiet"), not on a
  fixed interval.
- The sanitiser's whole filter (erase codes and blank runs) is bounded to the 30 s window, as our row
  says; after it the stream passes untouched.
- The attach-token keeper releases the lock of a token it rotated away from.
- Copy is ours; close codes never appear in text (they go to the console).

Tests we will write:
- ptySocket: query and subprotocols (no credential in URL, cwd only fresh, fresh only first dial); resize
  before first output, after two frames and after fonts; keep-alive at 20 s only when quiet; ladder
  250/500/1,000/2,000/3,000 then stop; hidden pauses and visible resumes (throttled); input refused with a
  notice while reconnecting; each close code's verdict; 8 s deadline for a stuck socket and a hung ticket;
  implicit resume frame starts the sanitiser; keep-alive registry semantics against the fake server
  (reattach replays the ring + `\x0c`; a second tab with the same token supersedes the first, 4409 quiet;
  child exit 4410).
- resumeSanitizer: 80 blank lines collapse to one within 30 s and not after; erase codes dropped within,
  kept after; split CSI and split CRLF across frames; end drops a partial escape.
- attachToken: stored under `indus.pty.token.chat`; reused in the same document; lock held by another
  document mints a new token; rotate mints and frees the old lock; no Web Locks; storage throwing.

## Decisions for the lead
- 4409 "quiet" is shown as one muted status line ("this terminal moved to another tab") with no overlay
  and no retry. Easy to drop to nothing.
- The 30 s sanitiser window covers blank-run collapsing too (row text), unlike a filter that collapses
  forever; change `ResumeSanitizer.feed` if blank floods outside a resume ever show up.
- Clean 1000 close is treated as "session ended" (start-new overlay), not a reconnect.
- The TUI (TS) side of the event feed — reading `INDUS_TUI_SIDECAR_URL` / `INDUS_TUI_SIDECAR_CREDENTIAL`
  and publishing to `/api/pub` — is not built yet (TUI owner; LEAD-NOTES follow-up). Until it is, the rail
  shows the configured model and no live data.

Compare checklist (filled after the rewrite; reopened `web/src/pages/ChatPage.tsx` connect effect, `onclose`
and input forwarding, `web/src/lib/pty-reconnect.ts`, `pty-resume-sanitizer.ts`, `pty-attach-token.ts`,
`pty-close-copy.ts`, `pty-composition.ts`):
- [x] Ladder 250 → 3,000 ms, 5 attempts, then stop with a manual reconnect (test: `ptySocket.test.ts::retries
  250, 500, 1000, 2000, 3000 ms and then stops`)
- [x] No redial while hidden or off the page; page-return events (visibility, focus, online, pageshow) dial once,
  1 s throttle; a stopped ladder is not restarted by them (test: `ptySocket.test.ts::parks while hidden or on
  another page and dials once when back, throttled to 1 s`)
- [x] The ticket phase is covered by the open budget (tests: `ptySocket.test.ts::gives a stuck handshake 8 s, then
  counts it as a failed attempt`, `::gives a ticket that never arrives 8 s, and a late ticket opens nothing`)
- [x] Resize at once, after two frames, after fonts; keep-alive (tests: `ptySocket.test.ts::sends the size at once,
  after two animation frames and after fonts load, before any output`, `::sends a keep-alive after 20 s of silence
  only`)
- [x] Implicit resume control frame starts the replay handling (test: `ptySocket.test.ts::an announced resume reports
  the id and sanitises for 30 s`)
- [x] Close-code table 4401 (reload once / gated) / 4403 / 4404 / 4408 / 1011 / 4410 / 4409 / 1001 / 1012 / 1013 /
  unclean / clean 1000 (tests: `ptySocket.test.ts` "close codes" group)
- [x] Attach token + Web Lock, duplicate tab mints its own, rotate on a fresh start (tests: `attachToken.test.ts`)
- [x] Sanitiser buffers a partial CSI and newline runs across frames; a partial escape is dropped at close (tests:
  `resumeSanitizer.test.ts`)
- [x] Input refused while not open, one notice per outage; SGR mouse reports never forwarded (test:
  `ptySocket.test.ts::refuses input while reconnecting, with one notice per outage`)
- [x] Composition commit forwarded once when xterm did not deliver it, echoes dropped (`terminal.ts`
  `CompositionGuard`)
- Deliberate differences: (1) the reference gives the ticket phase and the CONNECTING phase 8 s each; we give the
  whole dial one 8 s budget (row: "socket must open within 8,000 ms"). (2) The reference sends its keep-alive on a
  fixed 20 s interval; ours only after 20 s of silence (row: "while quiet"). (3) The reference keeps collapsing
  blank floods after its window and strips erase codes after collapsing; we bound both to the 30 s window (row
  text) and strip first, so floods with erase codes inside still fold. (4) After 4409 the reference may redial on
  the next page-return event and take the terminal back; ours stays put and offers "Use it here" (row: "4409 →
  quiet"). (5) The input-refused notice is a host line, not text written into the terminal. (6) We free the Web
  Lock of a rotated token.
- Not taken: the resume "loading" overlay, the `?learn=` seed, plugin slots, keyboard shortcut remaps, Android
  replacement-input normalisation, font-tier sizing, scroll pinning during replay, Safari/complex-script WebGL
  heuristics beyond the software-renderer gate — not in our row; candidates for the P18-30 parity review.

## Status

Done (2026-10-20) as prework against the fake PTY server; real-server E2E is P18-30. Shared edits:
`apps/web/src/main.tsx` (lazy host), `apps/web/package.json` / `pnpm-lock.yaml` (five xterm packages).

Integration (wave 32, P18c integrator, 2026-10-21): decisions numbered D-16.652 (close codes and timers) and D-16.653 (sanitiser). The browser run (P18-30) showed the sanitiser's "drop every erase code for 30 s" leaving stale text after every resume or reload — Ink repaints each line with `ESC [ 2 K` — so erase codes are now dropped only inside a flood (a run of breaks and erase codes with 50+ breaks), the trailing run held back includes erase codes between its breaks, and the regexes have unambiguous CR/LF alternatives (the first rewrite backtracked exponentially on long runs). Tests changed: `resumeSanitizer.test.ts` (two new, three updated), `ptySocket.test.ts` (the two resume cases). Real-server coverage: `apps/web/e2e/chat.spec.ts`.

## Wave 47 (builder d): light/dark reports to the embedded TUI

Status: done (`src/chat/colourScheme.test.ts`, fail-first: the reporter did not exist; the TUI side
is in P12/themes.md, Wave 47).
Read: none.

- The Chat tab's xterm already re-coloured itself on a `data-theme` change, but the TUI inside kept
  its dark-theme ink. `SchemeReporter` (colourScheme.ts) gives the terminal the colour-scheme
  report protocol: `CSI ? 996 n` is answered with `CSI ? 997 ; 1|2 n` (dark/light) as terminal
  input, `CSI ? 2031 h/l` turns unasked change reports on/off, and a theme switch (the `data-theme`
  observer, or the system scheme when no `data-theme` is set) sends one report when it really
  changed. Hooked in through xterm's parser handlers; other DSRs and modes are left to xterm.
