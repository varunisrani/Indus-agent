# Task: entry and terminal-mode hygiene (P12-07) Phase: 12 Owner: wave-8 builder R1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `ui-tui/src/entry.tsx` (TTY check,
reset at start and on exit, signal and error handling, dead-stream counter, memory monitor hooks),
`ui-tui/src/lib/terminalModes.ts` (the reset sequence, synchronous write). Research `05-tui.md`
§3.23, §3.30, traps R05.7, R05.8; `docs/notes/P12/ink-spike.md`. Evidence: none.

What it does (own words): `entry.tsx` starts the TUI. Without a TTY on stdin it prints one line and
exits 0. Otherwise it writes the full mode reset at once (a previous run may have been killed with
mouse reporting on), then installs one exit path that every way out goes through: normal `exit`,
SIGTERM, SIGHUP, an uncaught error, an unhandled rejection, `/quit`, Ctrl+D and the
memory-critical exit. Each path unmounts Ink first, writes the reset synchronously (`writeSync` on
fd 1), stops the bridge child and exits with its code: 0 normal, 1 dead terminal, 42 update
requested, 130 Ctrl+C, 137 memory critical. The reset turns off DEC locator reporting, mouse modes
9/1000/1001/1002/1003/1005/1006/1015/1016/2029, focus 1004 and bracketed paste 2004, leaves the
alternate screen 1049, pops the kitty keyboard stack, resets modifyOtherKeys and SGR, and shows the
cursor. Dead terminal: five write errors in a row with EIO/EPIPE stop the bridge and exit 1 instead
of spinning. Memory monitor: a warning line on fast heap growth, exit 137 after the reset at a
critical heap level.

Inputs → outputs: process events and app calls (`lifecycle.quit(code)`) → reset bytes on stdout,
bridge closed, exit code.

Files: `apps/tui/src/entry.tsx`, `apps/tui/src/terminal/{modes,deadStream,lifecycle,memoryMonitor}.ts`,
tests `modes.test.ts`, `deadStream.test.ts` (+ child scripts under `apps/tui/src/terminal/testing/`).

Edge cases / failure modes:
- the reset is written even when stdout is gone (errors swallowed) and at most once per exit.
- a second signal during shutdown does not run the shutdown twice.
- stdout not a TTY (piped): no reset bytes at all.
- the bridge kill is best effort and bounded; exit never waits on it forever.

Our design differences / decisions:
- one `Lifecycle` object owns every exit path (the reference spreads it over entry and a graceful
  exit helper); `NODE_ENV=production` is set before React loads (spike finding).
- the app composition is P12-27: until then entry renders a small boot view of our own.

Tests we will write: per exit path a child node process whose captured stdout ends with the reset;
non-TTY exits 0 with one line; fourth EPIPE does not exit, fifth exits 1 and closes the fake
bridge; any other error or a good write resets the count; memory monitor warn/critical with
injected readings.

Compare checklist (filled after the rewrite; reopened `entry.tsx`, `lib/terminalModes.ts`):
- [x] non-TTY → one line, exit 0 (test: `modes.test.ts::without a TTY on stdin prints one line and exits 0`)
- [x] reset at start and synchronously on exit, SIGTERM, SIGHUP, uncaught, rejection, /quit, Ctrl+D,
  Ctrl+C, update, memory-critical, dead terminal; Ink unmounted first, bridge stopped after (tests:
  `modes.test.ts::<path> → exit <code>…`, child processes)
- [x] reset content: locator, mouse 9/1000/1001/1002/1003/1005/1006/1015/1016/2029, 1004, 2004,
  1049, kitty pop, modifyOtherKeys reset, SGR, cursor (test: `…::turns off every mode…`)
- [x] fifth EPIPE exits 1 and stops the bridge, fourth does not (tests: `deadStream.test.ts`)
- [x] memory warn on fast growth, critical exit 137 after the reset (tests: `deadStream.test.ts::memory monitor`)
- [x] real-terminal smoke in tmux with a fake bridge: modes on while running; after Ctrl+D and after
  SIGTERM (exit 143) `alt=0 mouse=0 cursor=1`, bridge child gone, boot cache written.
- Deliberate differences: one `Lifecycle` owns all paths; signal exits use 128+n (143 SIGTERM, 129
  SIGHUP); `NODE_ENV=production` set before React loads; alt screen via Ink's option instead of a
  screen clear.
- Not taken: native/inline mode and Termux, OSC 10/11 colour restore (we never paint defaults),
  heap dumps, the crash breadcrumb log file, dashboard-embedded mode (option `ignoredSignals` exists).

## Status

Done (2026-10-19). `apps/tui/src/entry.tsx`, `apps/tui/src/bootView.tsx` (placeholder view until
P12-27), `apps/tui/src/terminal/{modes,deadStream,lifecycle,memoryMonitor}.ts`,
`terminal/testing/exitChild.ts`, tests `modes.test.ts` (15), `deadStream.test.ts` (7). Shared: none.
