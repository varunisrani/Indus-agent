# Task: Key routing — the master key table (P12-20) Phase: 12 Owner: wave-8 builder R3

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `ui-tui/src/app/useInputHandlers.ts`
(whole: Ctrl+C / double-Esc resolution, card pass-through, scroll fall-through, queue/history
cycling, global hotkeys), and from `ui-tui/src/lib/platform.ts` the action-modifier helpers.
Research `05-tui.md` §3.16, §3.20, §3.31, R05.5, R05.11. Evidence: none.

What it does (own words): one global handler sees every key first and decides who owns it: the
open card, the transcript (scrolling), the completion list, the queue/history walk, a global
action, or the editor. The order matters most for Ctrl+C: a non-empty composer (text,
continuation lines or tokens) is cleared first; with an empty composer a running turn is
interrupted; only an idle, empty composer exits. Ctrl+D exits only from a completely empty
composer and is swallowed otherwise. Esc pressed twice within 500 ms discards a draft (into
history) even while a card is open, and otherwise interrupts a running turn — but never while a
blocking card is up; a single Esc never stops work (it cancels a queue edit or clears a
selection). While a card is open only Ctrl+C (the card's cancel/deny) and the scroll keys
(PgUp/PgDn, Shift+Up/Down, wheel) are handled globally; every other key belongs to the card.

Master table (main view): Enter submit/apply; Shift/Alt+Enter newline; Ctrl+C clear → interrupt →
exit; Ctrl+D exit when empty; Esc Esc discard / interrupt; Esc cancel queue edit / clear
selection; Ctrl+L redraw (not a new session, R05.11); Ctrl+O model picker; Ctrl+X cut selection →
delete edited queue item → open session switcher; Shift+Tab toggle yolo (`config.set key=yolo`,
with the session) unless a completion list is open; Tab apply completion; Ctrl+G / Alt+G external
editor; Ctrl+V / Alt+V paste; Ctrl+K send next queued now, else kill to line end; Up/Down
completion cycle → queue → history (edge lines, no continuation); Shift+Up/Down one row;
PgUp/PgDn half a viewport (min 4); Ctrl+Z / Ctrl+Y undo / redo. On macOS the Super bit (Cmd via
kitty keys) also counts as the action modifier; Ctrl keeps working everywhere.

Inputs → outputs: Ink keys (normalised into `KeyPress`), a snapshot of ui/composer/overlay state →
a decision value (pure, testable) → the effect through injected actions (interrupt, exit, cancel
card, scroll, open picker/switcher, redraw, editor, paste, yolo, queue send, editor key).

Files: `apps/tui/src/input/keymap.ts` (key normalisation, modifier helpers, the table as data),
`apps/tui/src/input/routeKeys.ts` (pure `decideKey`, the `useKeyRouter` hook that applies it),
`apps/tui/src/input/routeKeys.test.ts`.

Edge cases / failure modes:
- Esc Esc timing uses an injected clock; after a double the next Esc starts a new pair.
- yolo without a session → a system line, no RPC; reply `value` true/false → "yolo on/off".
- Ctrl+X with a selection but the clipboard write fails → selection kept, nothing removed.
- a card with `ownsInput: true` (R4's cards) gets every key, Ctrl+C included; the router keeps only
  the Esc Esc draft discard and, under a prompt card, the scroll keys (a pager keeps its own paging).
- Up with a multi-line draft moves inside the draft; on the first line it walks queue then history.
- completion cycling only when the composer is non-empty and history is not being browsed.

Our design differences / decisions:
- The decision is a plain value (`{ action: "interrupt" }` …) so the priority table is tested
  without Ink; the hook only executes it.
- Keys owned by later phases (Ctrl+T agents monitor, Ctrl+R/F7 dock, Ctrl+B voice) are not routed
  yet and reach the editor as nothing.
- Cmd+C copy (Super+C) copies the composer selection; terminal-text selection belongs to R2/R1.

Tests we will write:
- Ctrl+C in six states: draft idle, draft busy, continuation only, token only, empty busy, empty idle.
- Ctrl+D with a paste token does nothing; Ctrl+D empty exits.
- Esc Esc with an approval card open and a draft: discards, never interrupts; without a draft and
  busy under a card: nothing; without a card: interrupts.
- scroll keys reach the transcript while a card is open; other keys go to the card.
- Ctrl+K with/without queue; Ctrl+X three-way; Shift+Tab yolo; Tab apply; Up/Down walk order.

## Decisions for the lead
- Scroll actions are emitted as `{ action: "scroll", rows }` / `{ action: "scroll-page", dir }`; the
  app wires them to R2's transcript (P12-27).

Compare checklist (filled after the rewrite; reopened `useInputHandlers.ts`):
- [x] Ctrl+C clear → interrupt → exit (test: `apps/tui/src/input/routeKeys.test.ts::ctrl+c priority`)
- [x] double-Esc discard above the card guard, interrupt below it (test: `routeKeys.test.ts::esc esc`)
- [x] scroll keys fall through a prompt card (test: `routeKeys.test.ts::scroll under a card`)
- [x] Esc cancels a queue edit before clearing a selection (test: `routeKeys.test.ts::single esc`)
- [x] Shift+Tab yields to an open completion list (test: `routeKeys.test.ts::the rest of the table`)
- [x] lead note (wave 8): a card with `ownsInput` handles its own keys, Ctrl+C included; only the
      Esc Esc draft discard and, under prompt cards, the scroll keys stay ours (test:
      `routeKeys.test.ts::scroll under a card`); Ctrl+O opens the picker card like `/model`
      (`openModelPicker`, test: `routeKeys.test.ts::model picker`)
- Deliberate differences: discard-on-Esc-Esc uses the full draft (tokens included); later-phase
  keys not routed.
- Not taken: dashboard-embedded "new session instead of exit", widget apps, pager keys (R4 owns
  the pager card), wheel acceleration (R1/R2 scroll owner).

## Status

Done (2026-10-19).
