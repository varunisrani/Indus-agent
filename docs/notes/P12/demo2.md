# Task: Demo 2 end to end (P12-33) Phase: 12 Owner: wave-9 builder (demo)

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `ui-tui/README.md` (the "Hotkeys and
interactions" section only: main input keys, prompt and picker keys, interaction rules), and the
file names only (not their contents) of `ui-tui/src/__tests__/gatewayRecovery.test.ts`,
`terminalModes.test.ts`, `useInputHandlers.test.ts`, `approvalAction.test.ts`. Ours: P12 phase file
(Goal, row P12-33, Done when, Acceptance), `15-roadmap.md` (M3), `04-architecture.md` D-04.2,
`docs/notes/P12/{app-bootstrap-recovery,tui-launcher,ink-spike,bridge-stdio,key-routing,
model-picker}.md`, LEAD-NOTES waves 7/8, `apps/tui/src/app/bridgeChild.test.ts`,
`tests/fakes/model/README.md`, `tests/e2e/` (Demo 1 harness). Evidence: none.

What it does (own words): proves Demo 2 on the real pieces. The built renderer
(`apps/tui/dist/entry.js`) runs in a real pseudo-terminal (node-pty); its output is fed to a headless
xterm so the test reads the screen a person would see, including terminal modes. The renderer starts
the real `indus tui-bridge --stdio` child from the worktree's venv; the model is the P03 scripted
fake, served on a loopback port so a separate process can reach it, and the profile in a temporary
home points a `custom` provider at it. Each case gets its own home, model and renderer. A Python
integration test drives the same bridge child over its pipes the way the renderer does.

Inputs → outputs: key bytes written to the pty, resizes, signals, `kill -9` of the bridge pid →
the screen (rows of text), xterm's mode flags, the renderer's exit code, the active-session file,
the stored transcript (`indus sessions export`), and the fake model's request log (one JSON line
per request: kind, script turn, last user text, requested model).

Files: `apps/tui/e2e/{harness.ts,demo2.e2e.test.ts,goldens/demo2-80x40.txt,goldens/demo2-120x40.txt}`,
`apps/tui/vitest.config.ts` (keeps `e2e/` out of `pnpm test`), `apps/tui/vitest.e2e.config.ts`,
`tests/integration/tui_bridge/{__init__.py,test_bridge_child.py}`, `tests/fakes/model/http_serve.py`
(the fake model on a socket), `tests/fixtures/model_scripts/tui_demo2.yaml`, this note.
Shared: `apps/tui/package.json` (dev deps `node-pty`, `@xterm/headless`; scripts `test:e2e`, `e2e`),
`package.json` (`tui:build`, `tui:e2e`), `pnpm-workspace.yaml` (node-pty allowed to build),
`pnpm-lock.yaml`, `apps/tui/tsconfig.json` (`e2e` included), plus the bug fixes listed below.

Scenarios (Goal + row): stream a reply; tool trail; approval card answered with `1`; clarify card
answered with a digit; Ctrl+O model picker switch (next request carries the new model); Ctrl+X
switcher resumes another stored session; queue while busy (default mode) and steer (configured);
Ctrl+C priority (clear draft → interrupt → exit); `kill -9` of the bridge mid-turn → restart line,
same stored session, nothing re-sent, one user row in the store; a 200-event resize storm ends in
one clean frame at the last size; `/quit` and SIGTERM leave every mode reset and no bridge behind;
screen goldens at 80 and 120 columns.

Edge cases / failure modes:
- the fake model's title request (after the first reply) must not use up a script turn: the
  socket server answers it outside the script with `Chat: <first three words>`, which also gives
  each session a distinct title in the switcher.
- "Ask anything" paints before the first session exists: the harness waits for the session id on
  screen and the active-session file before typing.
- machine-specific screen content (temp folder, session id, durations, tool count, rule padding)
  is masked; the work folder lives under `/tmp` to keep paths short; tool results in the golden run
  carry no paths (both tool calls are shell commands).
- a stalled stream at teardown: the socket server cancels open answers when it stops.
- a zombie bridge (container init that never reaps) counts as stopped.
- Windows: the suite skips (no pty driver wired), with the reason in the test name.

Our design differences / decisions:
- the fake model stays the P03 one (YAML/JSON scripts, five wires) behind a small HTTP/1.1 listener
  rather than a second fake written in TypeScript; scripts per case are written as JSON by the
  harness, the main demo uses `tui_demo2.yaml`.
- "no torn frame" is checked three ways: with sync output on (TERM_PROGRAM=WezTerm) every 2026
  begin has its end and never two begins in a row; the final status rule spans exactly the last
  width with one prompt, one rule and no duplicated rows; a Ctrl+L full repaint gives the same
  screen.
- the store check goes through `indus sessions export` (product path) rather than importing Python.

Tests we will write: the e2e suite (12 cases, above) and `test_bridge_child.py` (Demo turn with
approval through the child; kill -9 then resume in a new child with nothing re-sent; the two
settings the renderer reads at start).

## Bugs found and fixed (shared files)
- `src/indus/tui_bridge/methods_config.py`: providers whose key is optional (`custom`, LM Studio)
  were listed as "needs sign-in", so the model picker refused the provider the session was using.
  Now signed in. Pinned by `test_bridge_child.py::test_the_settings_the_tui_reads_at_start` and the
  Ctrl+O e2e case.
- `src/indus/tui_bridge/methods_config.py`: `config.get full` filled `busy` with the classic CLI
  default (`interrupt`), so the TUI never queued (R05.18, lead wave-8 follow-up). Unset now stays
  null and the TUI keeps `queue`. Pinned by the same Python test and the queue e2e case.
- `apps/tui/src/cards/serverRequests.ts`: when the clarify tool finished before the reply to the
  last `clarify.lock` arrived, the card printed "timed out" and then "answered". The verdict now
  waits for the lock's reply. Pinned by `serverRequests.test.ts::the tool completing while the
  last lock is in flight counts as answered` and the clarify e2e case.
- `apps/tui/src/entry.tsx`: Ctrl+L (and the return from the external editor) wiped the screen and
  left it blank until the next state change; it now repaints at once (`rerender` after `clear`).
  Pinned by the resize-storm e2e case.
- `apps/tui/src/turn/controller.ts`: finished tools were attached to the text *before* them while
  the transcript draws a message's tools *above* its text, so the screen read
  tool → its announcement. Finished tools now head the text that follows them (or stand alone), so
  the transcript reads in event order. `controller.test.ts` (two cases) and the `tools-and-diff`
  snapshot in `events/__snapshots__/handleEvent.test.ts.snap` were updated (diff reviewed: only the
  tool placement moved).

## D-04.2 (f) re-bench (2026-10-19)
Machine: the shared 4-core VM, load average 1.0–1.6, **Node 22.22.0** (Node 24 is not installed
here; `.nvmrc` says 24.21.0), `NODE_ENV=production`, ink 8.0.0, react 19.3.0.
- Spike bench (`node apps/tui/spike/bench.ts`, three runs), (f) 10k tokens at 5k/s, visible rows:
  29.9 / 31.0 / 29.2 fps (render mean 12–15 ms, p95 16–29 ms); incremental rendering 29.9 / 30.6 /
  31.5; at 1k tok/s 30.1 / 30.7 / 30.6; whole 16k tail wrapped by Ink 12.3–12.6 fps.
- Real renderer end to end (opt-in case `INDUS_E2E_BENCH=1`, pty + bridge + fake model, 100x40,
  frames counted as 2026 begin markers with the WezTerm profile, three runs): 5k tok/s 27.7 / 26.1 /
  27.5 fps; 1k tok/s 27.9 / 27.2 / 28.0 fps.
- Reading: with the 16 ms flush plus a 12–15 ms render the loop settles near 30 fps; the full app
  (markdown layout, status rule, composer, the bridge on the same machine) lands at 26–28. Still
  below 30 on this machine; the D-04.2 next step (scroll-region fast path for live rows) or a
  re-run on an unloaded Node 24 machine is the lead's call.

## Decisions for the lead
- `node-pty` 1.1.0 (2025-12-22, MIT) and `@xterm/headless` 6.0.0 (2025-12-22, MIT), dev only.
  node-pty compiles a native addon on install (no linux-x64 prebuild); it is the only entry in
  `onlyBuiltDependencies`/`allowBuilds`. pnpm 12 here does not bundle `node-gyp`: the install needed
  a `node-gyp` on PATH (npm's copy, `--nodedir` pointing at the local Node). CI needs the same
  (e.g. `npm i -g node-gyp` or the runner's toolchain) before `pnpm install`.
- E2E is opt-in: `pnpm --filter @indus/tui run e2e` (build + suite) / `pnpm tui:e2e`; `pnpm test`
  excludes `apps/tui/e2e`. About 40 s on this VM.
- P12-06 fixtures were **not** regenerated from recorded runs: their session ids, tool names and
  timings are pinned by the P12-10/11 unit tests and snapshots; a regeneration rewrites those
  tests too. Proposed follow-up: a recording bridge wrapper (tee of the child's stdout with
  timestamps → fixture lines) and one reviewed regeneration task.
- Ctrl+C on an empty idle composer exits 0 (the key path), not 130; 130 stays for SIGINT. The test
  pins 0. Say if the key path should return 130.
- In "interrupt" busy mode the bridge redirects inside the same turn id without a segment boundary,
  so the cut reply and the new reply join on one row ("Slow startqueued answer"). Not fixed here
  (bridge/core event shape); a null-text `message.delta` at the redirect would fix the renderer side.
- The model picker opened (Ctrl+O) before the first session exists keeps `sessionId: null` and later
  says "No session yet" even once the session is up; reading the id at apply time would fix it.
- `docs/internals/traps-ledger.md` / `acceptance-map.md` swaps (R05.*, J* → tests) are left to the
  lead: the e2e case names above are the test references for ACC-J1x/J8/J10/J11/J12/J13/J16/J17.
- Goldens: generated on Linux; the 80-column status rule keeps the folder at the end, so a much
  longer macOS `/private/tmp/...` path may drop a segment there. If macOS CI differs, mask or
  regenerate per platform.

Compare checklist (filled after the rewrite; reopened the README hotkey section and the four test
file names):
- [x] approval answered by digit (ours: `1-4`; the reference README lists `o/s/a/d` quick keys) —
  test `demo2.e2e.test.ts::streams, shows the tool trail and takes the approval card's 1`.
- [x] clarify quick-pick by number (test `::answers a clarify card with a digit`).
- [x] Ctrl+C order: draft → interrupt → exit (test `::Ctrl+C clears the draft, then interrupts …`).
- [x] plain text while busy is queued and drains after the reply (test `::queues a prompt typed
  while busy …`); steer mode (test `::steers a running turn …`).
- [x] recovery after the child dies (reference test name `gatewayRecovery`; ours `::kill -9 of the
  bridge mid-turn …` and `test_bridge_child.py::test_killed_child_mid_turn_…`).
- [x] terminal modes reset on each exit path (reference test name `terminalModes`; ours `/quit`,
  SIGTERM and the Ctrl+C exit cases).
- [x] model picker and session resume picker reachable by keys (Ctrl+O, Ctrl+X cases).
- Deliberate differences: Ctrl+L redraws instead of starting a new session (R05.11, D-16.24); the
  session switcher has no 1-9 quick pick (arrows + Enter); approval uses digits, not letters.
- Not taken: external-editor and paste flows in the pty (covered by unit tests of P12-17),
  sudo/secret cards (no fake tool asks for them yet).

## Status

Done (2026-10-19) except the fixture regeneration (above). Checks: see the builder report.
