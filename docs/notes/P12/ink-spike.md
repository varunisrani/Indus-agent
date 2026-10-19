# Task: D-04.2 upstream Ink spike (P12-01) Phase: 12 Owner: wave-7 builder K

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`):
`ui-tui/packages/hermes-ink/src/ink/ink.tsx` (only the alt-screen enter/leave, the editor
hand-off suspend/resume, the "re-assert modes after a gap" path, the exit-time mode reset and the
place where frames are wrapped in synchronized-output markers),
`ui-tui/packages/hermes-ink/src/ink/terminal.ts` (the synchronized-output capability test and the
extended-keys terminal list), `ui-tui/packages/hermes-ink/src/ink/parse-keypress.ts` (only the
CSI-u and modifyOtherKeys decode and the modifier-bit decode). Research `05-tui.md` §3.2, §3.23,
§3.30. Evidence: none.

What it does (own words): a throwaway trial that decides whether the TUI can sit on the published
Ink package (React 19) instead of a forked renderer. The reference forked Ink and grew its own
writer; we want the upstream package plus a few small modules of our own around its stdout. The
spike answers seven questions with numbers, and for each gap names the module of ours that fills
it. Renderer tasks (P12-07 onward) may not commit to Ink APIs until this verdict is written.

What the reference does that matters (facts, not code):
- Alternate screen: `?1049h` on enter, `?1049l` on leave; on any exit path (including signals) the
  leave and every other mode reset are written synchronously so nothing leaks into the shell.
- Extended keys: kitty keyboard is a *stack* (`CSI >1u` push, `CSI <u` pop) — every push needs one
  pop, and a re-assert must pop before it pushes or the depth grows; modifyOtherKeys is a level
  (`CSI >4;2m` on, `CSI >4m` reset). Both are only enabled on an allow-list of terminals; one
  terminal gets modifyOtherKeys only because its kitty mode loses Alt on Backspace. Both are turned
  off before an external editor runs and back on after.
- Decoding: CSI-u is `ESC [ code ; mods u`, modifyOtherKeys is `ESC [ 27 ; mods ; code ~`; the
  modifier number minus one is a bit set (1 shift, 2 alt, 4 ctrl, 8 super). Enter is code 13,
  Backspace 127, so Shift+Enter, Alt+Enter and Ctrl+Backspace become distinguishable.
- Synchronized output (DEC 2026): begin/end markers around each frame only on terminals known to
  implement it (decided from `TERM_PROGRAM`, `TERM`, a few vendor variables, a VTE version floor),
  and never under tmux or zellij, which pass the bytes through but re-chunk them.

Inputs → outputs: `node apps/tui/spike/bench.ts` → a table on stdout: fps and frame cost for a
10,000-token stream with 16 ms batching, frames written during a 200-event resize storm (raw Ink
and with our coalescer), synchronized-output wrap counts inside/outside tmux, key decode results.
`pnpm --filter @indus/tui spike:app` → the interactive app for the manual terminal checklist.

Files: `apps/tui/package.json`, `apps/tui/tsconfig.json`, `apps/tui/spike/inkSpike.tsx`,
`apps/tui/spike/bench.ts`, spike helper modules under `apps/tui/spike/` (sync writer, resize
coalescer, key and mouse decode, mode sequences), this note.

Edge cases / failure modes:
- Not a TTY (CI, pipes): no mode sequences at all; the bench uses a fake TTY stream.
- tmux / zellij: sync markers suppressed even when `TERM_PROGRAM` names a capable outer terminal.
- Exit by signal or uncaught error: alt screen left and modes reset with synchronous writes.
- Kitty stack: pop before every push, one pop at exit; never leave the shell in CSI-u mode.
- Resize storm: first event reflows at once, then at most one reflow per 32 ms, always a final one.
- Paste: a bracketed paste must arrive as one chunk even when the terminal splits it into reads.

Our design differences / decisions:
- Upstream Ink as a dependency, nothing forked; anything missing is our own module wrapped around
  Ink's stdout/stdin (D-16.28). Terminal tables are written from our own reading of the terminals'
  documentation and kept small.
- Spike code is throwaway (deleted at phase end); only this note and the bench stay.

Tests we will write: none for the spike (throwaway by design); the bench is the measurement. The
modules that survive move into `apps/tui/src/terminal/` with tests in P12-07/P12-08.

## Results (2026-10-19)

Setup: shared 4-core VM (load average 2.5–4.4 from other builders), **Node 22.22.0** (the machine
has no Node 24; `.nvmrc` says 24.21.0), ink 8.0.0, react 19.3.0, `NODE_ENV=production` (React's
development build roughly doubles render cost: always set it for the TUI). Bench:
`NODE_ENV=production node apps/tui/spike/bench.ts` (fake TTY, 100×40). Real-terminal smoke: the
spike app inside tmux 3.x driven by `send-keys`/`resize-window`, with tmux's own pane flags read
before and after exit.

| Item | Upstream Ink 8 alone | With our modules | Result | Module (spike file → home in P12-07/08) |
|---|---|---|---|---|
| (a) alt screen 1049 + clean exit | `alternateScreen: true` enters at byte 0, leaves on unmount | tmux: running `alt=1 mouse=1 sgr=1 cursor=0` → after `q` `alt=0 mouse=0 sgr=0 cursor=1`; after SIGTERM the same (exit 143) | **pass** | `terminalModes.fullReset` + exit/signal hook (unmount Ink *first*, else Ink repaints its last frame onto the main screen) → `terminal/modes.ts` |
| (b) kitty push/pop, modifyOtherKeys | kitty push/pop built in (`kittyKeyboard`), CSI-u decoded; modifyOtherKeys neither enabled nor decoded | stdin filter rewrites `ESC[27;m;c~` to CSI-u: Shift+Enter, Alt+Enter, Ctrl+Backspace distinct in both encodings (7/7); in tmux `ESC[27;2;13~` → `shift+return` | **pass with module** | `inputFilter.ts` + `extendedKeyPlan` → `terminal/keys.ts` |
| (c) bracketed paste 2004 | `usePaste` enables 2004 and delivers one string | 5,000 chars over 3 reads → 1 paste event, 0 key events | **pass (native)** | none |
| (d) SGR mouse 1000/1002/1006 + selection | no mouse support | filter takes SGR reports out of stdin: press, drag, drag (split across reads), release, wheel up/down decoded, 0 leaked keys; tmux: 4 reports; selection painted with one `backgroundColor` on whole rows | **pass with module** | `inputFilter.ts` (mouse part) → `terminal/mouse.ts` (P12-08/13) |
| (e) DEC 2026 per frame, never in tmux | wraps **every** TTY frame in 2026 markers, tmux included (11 of 11 frames) | stdout gate drops the markers unless the terminal is known and not multiplexed: tmux 0, WezTerm 11 | **pass with module** (upstream alone fails R05.17) | `outputGate.ts` + `syncOutputWanted` → `terminal/capabilities.ts` |
| (f) 10k-token stream, 16 ms batching, ≥ 30 fps | — | visible rows only, keyed by absolute row: **28.3–30.8 fps over six runs** (render mean 15–18 ms, p95 21–26 ms); same at 1k tok/s 29.5–29.9 fps; `incrementalRendering` no gain; whole 16k-char tail wrapped by Ink: 8–12 fps (render 62–85 ms) | **borderline: below 30 on 5 of 6 runs** on this loaded VM | render only visible rows (own row cache; Ink never wraps a long message) → P12-12/13 |
| (g) 200-event resize storm coalesced (bound = storm ms / 32 + 2 ≈ 43) | renders and writes on every event: 203 frames, 265 renders | 32 ms leading+trailing coalescer on the stdout `resize` listener: 38–40 frames, final width exact (123); tmux 61 resizes → 4 reflows | **pass with module** (upstream alone fails R05.16) | `outputGate.ts` (`coalesce`) → `terminal/resize.ts` |

Cost model: Ink 8's per-frame cost grows with the characters it lays out (≈ 3–4 µs per character
here: 1k chars ≈ 5 ms, 4k ≈ 15 ms, 16k ≈ 48 ms, 64k ≈ 200 ms), mostly per-character ANSI
tokenising and garbage collection. A 100×40 screen of dense text is ≈ 15 ms, which with React and
the 16 ms flush lands right at 30 fps on this machine.

Manual checklist (kitty, iTerm2/WezTerm, Windows Terminal on WSL2): **not run** — headless container.
tmux was run for real (above). Lead: run `pnpm --filter @indus/tui spike:app` in each and tick:
- [ ] kitty: header says `kitty true`; Shift+Enter / Alt+Enter / Ctrl+Backspace show distinct keys;
  `q` leaves a clean shell (no `[13;2u` echoes, no mouse garbage).
- [ ] iTerm2 / WezTerm: same; no flicker while `s` streams; drag-select paints a solid band.
- [ ] Windows Terminal (WSL2): same; resize by dragging the window corner, no torn frames.
- [x] tmux: `sync off`, `kitty false`, `otherKeys true`; modes on while running and all off after
  `q` and after SIGTERM (pane flags); stream completes; resize storm reflows 4 times, final width
  exact.

## Verdict (proposed; the lead signs D-04.2 in `04-architecture.md`)

**Upstream Ink holds for layout, input and the screen lifecycle, but not alone: it needs the
fallback-shaped wrapper of D-04.2** — our own small modules around Ink's stdin/stdout:
a stdout gate (2026 markers only where wanted; resize coalescing), a stdin filter (modifyOtherKeys
→ CSI-u; SGR mouse out of the key stream), and our exit/mode-reset path. No fork, no custom
renderer. (e) passes with the gate. **(f) is borderline**: 28.3–30.8 fps on a loaded 4-core VM
under Node 22, repeated six times — by the letter of the row that is "failed twice", so the
fallback decision goes to the lead. Recommendation: re-run `bench.ts` on an unloaded machine with
Node 24 first; if (f) still misses 30 fps, the next step is the D-04.2 "scroll-region fast path"
in our writer for the live transcript rows (Ink keeps layout of everything else), not a full
custom renderer.

## Decisions for the lead
- D-04.2 verdict as above; (f) needs a re-run on a reference machine before sign-off.
- New deps (exact pins, ≥ 14 days old): `ink` 8.0.0 (2026-10-03, MIT), `react` 19.3.0
  (2026-09-09, MIT; 19.3.1 is too new), dev `@types/react` 19.3.0 (2026-09-09, MIT). Ink 8 needs
  react ≥ 19.3.0. Transitive packages all pass the licence check. `zustand`, `ink-testing-library`,
  `node-pty`, `@xterm/headless` from the files table are left for the tasks that use them.
- Renderer rules taken from the numbers: always `NODE_ENV=production`; the transcript gives Ink
  only visible rows with stable keys (absolute row ids), never a whole long message; `maxFps: 60`.
- `inkSpike.tsx` is run through `vite build --ssr` (JSX) into `node_modules/.spike`; `bench.ts`
  avoids JSX so Node's type stripping runs it directly (`erasableSyntaxOnly` on in `apps/tui`).
- Signal exit: unmount Ink before writing the mode reset (found in the tmux run).
- Follow-ups for P12-07/08: re-assert extended keys after an idle gap with pop-before-push; turn
  extended keys and mouse off around the external editor (P12-17); drain stdin at exit.

Compare checklist (filled after the run; reopened the three files above):
- [x] alt screen enter/leave and exit reset, signal path included (tmux smoke; bench (a))
- [x] kitty push/pop balance (Ink pops on unmount; our reset pops once more, a no-op on an empty
  stack), modifyOtherKeys on/off (`seq.otherKeysOn/Off`)
- [x] DEC 2026 only on known terminals, never in tmux/zellij/screen (bench (e))
- [x] decode: modifier bits (1 shift, 2 alt, 4 ctrl, 8 super) via Ink's CSI-u parser after our
  rewrite (bench (b))
- Deliberate differences: the reference forked Ink and owns its writer; we keep upstream Ink and wrap
  its streams. Our terminal tables are smaller (no Warp/contour/Zed rows yet) and add `screen` to the
  multiplexer list.
- Not taken: the idle-gap re-assert, editor hand-off, stdin drain (follow-ups above).

## Status

Done as a spike (2026-10-19): numbers and verdict above; manual runs outside tmux and the D-04.2
sign-off are the lead's. Files: `apps/tui/package.json`, `apps/tui/tsconfig.json`,
`apps/tui/spike/{inkSpike.tsx,bench.ts,terminalModes.ts,outputGate.ts,inputFilter.ts}`.
`docs/plan/04-architecture.md` not edited (lead signs the row).
