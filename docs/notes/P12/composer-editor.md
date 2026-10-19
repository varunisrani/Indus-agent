# Task: Composer editor — text model and Editor component (P12-16) Phase: 12 Owner: wave-8 builder R3

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `ui-tui/src/components/textInput.tsx`
(grapheme stops, word motion, line kill helpers, line up/down, the commit/undo/redo path, the key
handler body, mouse multi-click; fast-echo and right-click helpers skipped). Research `05-tui.md`
§3.8, §3.20, §3.30 (undo 200, multi-click 500 ms, double-Esc 500 ms). Evidence: none.

What it does (own words): the composer is the one place the user types. Its text is a single
string with a cursor that only ever rests on grapheme boundaries, so an emoji with a skin-tone
modifier, a letter with a combining accent or a CJK character is stepped over and deleted as one
unit. Above the live line sits a "continuation" list of lines already finished with a trailing
backslash; inside the text sit token labels (a collapsed paste, an attached image) that stand for
bigger content. Readline-style keys edit it; every change that alters the text is recorded so
Ctrl+Z / Ctrl+Y can walk back and forth, the oldest record falling off after 200. A double Esc
within 500 ms throws the draft away but keeps it in history so Up brings it back.

Inputs → outputs: key presses (Ink `useInput` keys, normalised to our `KeyPress`) and mouse
positions → new text/cursor/selection, or a signal to the router: "submit", "at first line",
"at last line", "not mine". The Ink component draws the continuation lines, the text with an
inverse cursor cell or a highlighted selection, and a muted placeholder when empty.

Files: `apps/tui/src/composer/editorModel.ts` (pure model, key → edit mapping),
`apps/tui/src/composer/Editor.tsx` (thin Ink view), `apps/tui/src/composer/composerStore.ts`
(new: the composer as a whole — model, continuation, tokens, revision, history browsing),
`apps/tui/src/input/keymap.ts` (shared `KeyPress` type, built in P12-20), tests
`editorModel.test.ts`, `Editor.test.tsx`, helper `apps/tui/src/composer/testSupport.ts`.

Key behaviour (ours, from the row and the spec):
- Enter submits; Shift+Enter and Alt+Enter insert a newline; a bare LF (Ctrl+J) also inserts one.
- Left/Right by grapheme; Ctrl/Meta+Left/Right and Meta+B/F by word (whitespace-delimited);
  Shift extends a selection. Left/Right with a selection collapse it to that side.
- Home/Ctrl+A and End/Ctrl+E go to the start/end of the current logical line.
- Backspace/Delete by grapheme; Ctrl+W, Ctrl+Backspace, Meta+Backspace delete the previous word;
  Ctrl+Delete, Meta+Delete, Meta+D delete the next word; Super+Backspace / Super+Delete kill to the
  line edge. Ctrl+U kills to the line start (at a line start it eats the newline before, so a
  repeat walks upward); Ctrl+K kills to the line end (at a line end it joins the next line). Any of
  these with a selection deletes the selection instead.
- Up/Down move between logical lines keeping the column; on the first/last line they report the
  edge so the router can walk the queue and history.
- Undo/redo: a snapshot (text, cursor) is pushed before each text-changing edit; 200 kept; a new
  edit clears redo. External replacement (history recall, clear) resets both stacks.
- Mouse: click places the cursor; a second click on the same offset within 500 ms selects the word,
  a third selects the line; drag from a press extends a selection.
- Multi-character input in one key event (IME commit, unbracketed paste without newlines) is one
  insert, one undo step, never treated as fast typing.

Edge cases / failure modes:
- cursor arithmetic on surrogate pairs and ZWJ sequences: always snap to segmenter boundaries.
- a combining mark typed after a base letter merges with it: cursor still lands at the end.
- deleting inside a token label is allowed; the composer drops tokens whose label is gone.
- empty text: Backspace/Ctrl+U/Ctrl+W are no-ops (no undo record).
- double Esc measured with an injected clock; 500 ms counts as double, 501 ms does not.

Our design differences / decisions:
- Home/End act on the logical line (the spec's wording) rather than the whole buffer.
- No fast-echo bypass of the renderer (D-04.2: Ink renders only visible rows, frames batch).
- The model is a plain class with a version counter; the composer store notifies React.
- Right-click copy/paste is not built (selection copy goes through Ctrl+X cut / clipboard module).

Tests we will write:
- every key against ASCII, emoji (ZWJ family, skin tone), combining marks and CJK;
  word motion and word deletes; line kills incl. repeat across lines; line up/down with column.
- undo: 201 edits keep 200 (the oldest gone); redo cleared by a new edit; undo after redo.
- selection: shift-extend, replace by typing, delete by Backspace; double/triple click.
- Esc Esc at 500 ms (fake clock) discards into history; at 501 ms the draft stays.
- Editor component: typing through a fake stdin renders the text; Enter calls submit;
  Shift-less Alt+Enter inserts a newline; placeholder when empty.

## Decisions for the lead
- Home/Ctrl+A, End/Ctrl+E use the logical line (spec 3.8 wording); change in `editorModel.ts`
  `lineStart`/`lineEnd` callers if the buffer edges are wanted.

Compare checklist (filled after the rewrite; reopened `textInput.tsx`):
- [x] grapheme stops for emoji/combining/CJK (test: `apps/tui/src/composer/editorModel.test.ts::graphemes`)
- [x] word motion stops at whitespace runs both ways (test: `editorModel.test.ts::word motion`)
- [x] Ctrl+U at a line start eats the newline; Ctrl+K at a line end joins (test: `editorModel.test.ts::line kills`)
- [x] line up/down keep the column and report edges (test: `editorModel.test.ts::line navigation`)
- [x] undo cap 200, redo cleared on a new edit, undo pushes onto redo (test: `editorModel.test.ts::undo`)
- [x] selection replaced by typing / removed by delete keys (test: `editorModel.test.ts::selection`)
- [x] Meta+D deletes the next word (gap found in compare; pinned by `editorModel.test.ts::word deletes`)
- [x] Ctrl+Y and Ctrl+Shift+Z redo (gap found in compare; pinned by `editorModel.test.ts::undo`)
- [x] gap found: Cmd+A selects everything on macOS — fixed in `applyKey`, pinned by
      `editorModel.test.ts::plain keys` ("selects everything with Cmd+A")
- [x] gap found: several lines typed in one event (an unbracketed paste) must take the paste path —
      fixed in `routeKeys.ts` (`isUnbracketedPaste`), pinned by `routeKeys.test.ts::effects`
- [x] IME text arriving with a trailing CR is kept before submit (gap found; pinned by `editorModel.test.ts::enter`)
- Deliberate differences: Home/End are line-scoped; no stdout fast path; selection copy on mouse
  release is not automatic.
- Not taken: fast-echo, right-click copy/paste, terminal-specific Ctrl+J heuristics (we always treat
  a bare LF as newline), voice key pass-through (no voice binding in this phase).

## Status

Done (2026-10-19). Shared `KeyPress` type lives in `input/keymap.ts` (P12-20, same builder).
