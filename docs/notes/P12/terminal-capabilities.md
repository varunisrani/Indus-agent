# Task: terminal capabilities, extended keys, resize (P12-08) Phase: 12 Owner: wave-8 builder R1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `ui-tui/src/lib/resizeCoalescer.ts`,
`ui-tui/src/lib/platform.ts` (terminal detection only), `ui-tui/packages/hermes-ink/src/ink/terminal.ts`
(the synchronized-output terminal list, the extended-keys list and the kitty exception). Research
`05-tui.md` §3.23; `docs/notes/P12/ink-spike.md` (the spike's gate, filter and coalescer, promoted
here). Evidence: none.

What it does (own words): our wrapper modules around Ink's streams. The output gate wraps each
frame in DEC 2026 begin/end only on terminals known to implement it (kitty, iTerm2, WezTerm,
Ghostty, Zed, VS Code, Windows Terminal — the row's list exactly) and never when `TMUX` (or
zellij/screen) is set (R05.17). Extended keys: kitty keyboard push and xterm modifyOtherKeys are
enabled per a terminal list (Ghostty gets only modifyOtherKeys; tmux only modifyOtherKeys), turned
off around an external editor and restored after (pop before push so the stack never grows). The
input filter rewrites modifyOtherKeys reports into the CSI-u form Ink decodes and takes SGR mouse
reports out of the key stream; a pure key decoder maps both encodings to our key events
(Shift+Enter, Alt+Enter, Ctrl+W, Ctrl+Backspace, …). The resize coalescer reflows at once on the
first event, at most once per 32 ms after, and always once more at the last size (R05.16). Mouse
presets `off`, `wheel` (1000+1006), `buttons` (+1002), `all` (+1003, default); `INDUS_TUI_MOUSE`
forces one.

Inputs → outputs: env → `Capabilities {syncOutput, kitty, otherKeys, mouse}`; Ink's stdout →
gated stream; stdin → filtered stream + mouse events; raw key bytes → `KeyEvent`.

Files: `apps/tui/src/terminal/{capabilities,keys,resize,outputGate,inputFilter}.ts`, tests
`capabilities.test.ts`, `keys.test.ts`, `resize.test.ts` (+ `outputGate.test.ts`).

Edge cases / failure modes:
- tmux with `TERM_PROGRAM=WezTerm` inherited from the outer terminal → still no 2026 bytes.
- a sequence split across reads is held briefly; a lone ESC is released after 15 ms.
- bracketed paste bodies pass through untouched (no rewrite inside a paste).
- unknown terminals get no extended keys (some echo garbage on unknown CSI).
- resize listener removed → pending trailing reflow cancelled.

Our design differences / decisions: our terminal tables are written from terminal documentation;
the gate drops Ink's own 2026 markers (Ink 8 always writes them) instead of owning the writer.

Tests we will write: env matrix for sync output; key table in both encodings; 200 resize events in
1 s → first + one per 32 ms + final, last size exact; mouse preset sequences and env override;
editor hand-off off/on sequence; gate drops/keeps markers.

Compare checklist (filled after the rewrite; reopened `resizeCoalescer.ts`, `platform.ts` detection,
`hermes-ink/src/ink/terminal.ts` lists):
- [x] tmux/zellij/screen never get 2026 bytes, known terminals do (tests: `capabilities.test.ts::
  synchronized output`, `…::under tmux drops every 2026 marker Ink writes`, `…::in kitty keeps the markers…`)
- [x] extended keys list with the Ghostty and tmux exceptions; pop before push; editor hand-off
  (tests in `describe("extended keys")`)
- [x] key table in both encodings: Shift+Enter, Alt+Enter, Ctrl+W, Ctrl+Backspace (tests: `keys.test.ts`)
- [x] 200 resize events in 1 s → first + one per 32 ms + final, last size exact (test: `resize.test.ts`)
- [x] mouse presets and `INDUS_TUI_MOUSE` (tests in `describe("mouse presets")`)
- Deliberate differences: the reference coalescer re-arms its trailing timer on every event; ours
  uses fixed 32 ms windows (same bound, simpler). Sync list kept to the row's terminals (the
  reference also lists Warp, contour, Alacritty, foot and VTE ≥ 0.68).
- Not taken: XTVERSION probing over SSH, progress-report OSC 9;4, Windows conhost quirks.

## Status

Done (2026-10-19). `apps/tui/src/terminal/{capabilities,keys,resize,outputGate,inputFilter}.ts`,
tests `capabilities.test.ts` (22), `keys.test.ts` (29), `resize.test.ts` (4). The spike modules stay
under `apps/tui/spike/` (deleted at phase end per P12-01).
