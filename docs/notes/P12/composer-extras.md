# Task: Composer extras — history, paste, external editor, clipboard (P12-17) Phase: 12 Owner: wave-8 builder R3

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `ui-tui/src/lib/history.ts` (whole),
`ui-tui/src/app/useComposerState.ts` (paste resolution, image attach, drop detection, collapse,
editor hand-off, stale guards), `ui-tui/src/lib/editor.ts` (whole), `ui-tui/src/lib/clipboard.ts`
(whole), `ui-tui/src/lib/osc52.ts` (whole); also glanced at `ui-tui/src/lib/text.ts` (paste label
helper) and `ui-tui/src/domain/attachments.ts` (token expansion). Research `05-tui.md` §3.8, §3.11,
R05.10, R05.19. Ours: `docs/notes/P06/repl-core.md`, `src/indus/cli/repl/history.py`,
`docs/notes/P12/bridge-methods.md` (`paste.collapse`, `image.attach`, `input.detect_drop`).
Evidence: none.

What it does (own words):
- **History.** Every submitted prompt is remembered in a file in the profile home so Up recalls it
  across runs, and the classic CLI and the TUI share it. Loaded once, lazily; newest 1,000 kept;
  blank input and an exact repeat of the newest entry are not stored; a file that cannot be
  written never disturbs the user.
- **Paste.** Terminal bracketed-paste markers are removed, line endings normalised and trailing
  newlines dropped; the text is inserted where the cursor is, newlines intact. A single-line paste
  that looks like a path is checked: an image path is staged with `image.attach` and becomes an
  image token; any other path-looking text goes to `input.detect_drop`, and a recognised drop is
  inserted as its resolved paths. A long paste (≥ 5 lines or ≥ 2,000 characters, both from config)
  becomes a short label token while `paste.collapse` keeps the full text in a file; the model gets
  the full text on submit. Every await re-checks that the session and the composer are still the
  ones the paste started with: if the session changed the result is dropped, if only the composer
  changed (the user typed) the raw text is inserted as typed text so nothing is lost.
- **External editor.** Ctrl+G / Alt+G writes the continuation lines plus the draft to a temp file,
  hands the terminal to `$VISUAL`/`$EDITOR` (split like a shell would, quotes respected), else the
  first of `editor`, `nano`, `vi` found on PATH, else `vi` (Windows: `notepad.exe`). On exit 0
  with non-empty text the text is submitted and the composer cleared; on any other exit the draft
  is left exactly as it was. The temp folder is always removed.
- **Clipboard.** Ctrl+V / Alt+V reads text: in an SSH session the terminal's OSC 52 answer first,
  then the native tool; locally the native tool first, then OSC 52. Unusable text (empty, NULs,
  mostly control characters) counts as nothing. Writing (cut) tries native tools, then OSC 52.

Inputs → outputs: submitted text → history file; pasted text + session → composer insert / token,
RPCs `image.attach {session_id, path}`, `input.detect_drop {text}`, `paste.collapse {text}`;
Ctrl+G → temp file, child process, submit; Ctrl+V → clipboard text → the paste path.

Files: `apps/tui/src/composer/history.ts`, `paste.ts`, `externalEditor.ts`, `clipboard.ts`, tests
`history.test.ts`, `paste.test.ts`, `externalEditor.test.ts`; golden
`tests/fixtures/tui/input_history.golden` read by `history.test.ts` and by a new Python test
`tests/unit/cli/repl/test_history_golden.py` so both front ends prove they read the same file.

Edge cases / failure modes:
- history file missing, unreadable, or with foreign lines: skipped quietly; folder created on write.
- multi-line history entries survive the round trip; unicode kept as is.
- paste of only newlines: nothing inserted. Paste with `\r\n` or lone `\r`: becomes `\n`.
- `image.attach` failure (not an image, too big) falls back to drop detection, then to plain text.
- user types while `input.detect_drop` is in flight: raw text inserted at the current cursor.
- paste collapse reply arrives after a session switch: the token keeps its text; the path is not
  recorded.
- editor missing from PATH: spawn error → treated like a non-zero exit (draft unchanged), a system
  line says the editor could not start.
- OSC 52 unanswered within 500 ms: treated as empty.

Our design differences / decisions:
- **History format.** The row names the prompt_toolkit `+`-line shape, but the classic CLI as
  landed in P06 (`src/indus/cli/repl/history.py`) writes one JSON string per line. Sharing the file
  is the point, so the TUI writes the CLI's JSON-lines shape (atomic rewrite, mode 0600) and
  *reads* both shapes, so a file in the `+` shape is still understood.
- History path: `$INDUS_HOME/input_history` (the profile home the launcher exports), else
  `~/.indus/input_history`.
- Our own token labels: `[[paste N · L lines]]`, `[[image N]]` (the bridge's placeholder names a
  long file path, too wide for the composer; we keep its `path` on the token).
- The label is inserted at once; `paste.collapse` runs alongside and only adds the path.
- No `image.detach` exists in our protocol: an image token removed from the text is dropped from
  the composer only (noted for P12-29 follow-up).
- Clipboard image paste (`clipboard.paste`) is not in our protocol; an empty clipboard yields a
  "clipboard is empty" notice.
- OSC 52 reading needs a terminal query channel (R1 owns stdin); `clipboard.ts` takes an injected
  `query(request, timeoutMs)` and builds/parses the sequences itself.
- Editor suspension is injected (`suspend(run)`), wired by the app to Ink's `suspendTerminal`
  plus R1's mode reset/restore.

Tests we will write:
- history golden read; JSON-lines and `+`-shape read; 1,001 appends keep 1,000; exact repeat and
  blank skipped; write into a read-only folder silent.
- paste: markers stripped and newlines kept; 5-line paste collapses, `paste.collapse` called, submit
  expands to the full text; image path → `image.attach` → image token; typing during
  `input.detect_drop` keeps the raw text; session switch drops the result.
- editor: fake editor script edits and exits 0 → submitted; exits 1 → draft unchanged; command
  resolution (`$VISUAL` with quotes, PATH fallback order, Windows).
- clipboard: SSH prefers OSC 52, local prefers native, each falls back; unusable text rejected;
  OSC 52 reply parsing and tmux wrapping.

## Decisions for the lead
- History file shape: JSON lines (the landed classic CLI), reading the `+` shape too — see above.
  The row text ("prompt_toolkit file-history shape") should be corrected, or P06 changed.
- Launch-time env `INDUS_HOME` is assumed to be exported to the renderer by P12-32.

Compare checklist (filled after the rewrite; reopened the five files above):
- [x] lazy load once, keep 1,000, skip blank and repeat (test: `apps/tui/src/composer/history.test.ts`)
- [x] path heuristics: `file://`, `~/`, `./`, `../`, quoted, drive letters; bare `/x` needs a second
      `/` or a dot (test: `apps/tui/src/composer/paste.test.ts::looks like a path`)
- [x] collapse thresholds from config, either one triggers (test: `paste.test.ts::collapse`)
- [x] stale paste result guarded by destination + revision (test: `paste.test.ts::paths` "keeps the raw text when the user types")
- [x] editor: non-zero exit leaves the draft; temp dir removed (test: `apps/tui/src/composer/externalEditor.test.ts`)
- [x] clipboard order by SSH, usability check (test: `apps/tui/src/composer/clipboard.test.ts`)
- [x] gap found: a dropped-file paste with an image among several paths — attach each image, keep
      the other paths as text (pinned by `paste.test.ts::paths` "drop with images")
- [x] gap found: the Ctrl+V path (clipboard → same paste flow, empty clipboard notice, stale
      session drop) — added `pasteFromClipboard`, pinned by `paste.test.ts::clipboard paste`
- [x] gap found: Windows copy must not pipe text through the legacy code page — PowerShell gets the
      text base64 in its arguments, pinned by `clipboard.test.ts::windows`
- Deliberate differences: JSON-lines history; local labels; no detach; no clipboard image.
- Not taken: token count/size trimming (32 tokens / 4 MiB) — kept as a constant cap of 32 tokens;
  canonical-gateway staging; pending-input journal.

## Status

Done (2026-10-19). New Python test file and golden fixture outside `apps/` (new files, not shared).
