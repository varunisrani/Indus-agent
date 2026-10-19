# Task: Completion — slash and path, fuzzy ranking (P12-18) Phase: 12 Owner: wave-8 builder R3

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `ui-tui/src/hooks/useCompletion.ts`
(whole), `ui-tui/src/app/slash/fuzzyScore.ts` (whole); for the apply rules also
`ui-tui/src/domain/slash.ts` (inline trigger, apply, apply-on-Enter). Research `05-tui.md` §3.9,
§3.31, R05.10, R05.13. Ours: `docs/notes/P12/bridge-methods.md` (`complete.slash`,
`complete.path`). Evidence: none.

What it does (own words): while the user types (and no blocking card is open) the composer asks
the bridge for completions, 60 ms after the last change. Which request depends on the shape of the
text: a leading `/word` asks for slash commands (the model picker owns `/model`, so it gets none);
a `/word` after whitespace at the end of a message asks the slash completer too but keeps only
skills (a skill reference dropped into prose); otherwise a trailing path-looking word asks for
paths, resolved in the session's folder (or the folder `indus` was launched from), never the
renderer's own working directory — on a remote backend the bridge lists the remote tree. A reply
for text that has since changed is thrown away. The list shows up to 16 rows; Up/Down cycle with
wrap; Tab applies (not while the draft has several lines); Enter applies only when that changes
the typed command, else it submits.

A separate fuzzy scorer ranks local command lists (used by the slash dispatcher and pickers):
exact id/label/alias 0, prefix 1, substring 2, description word exact 3, word prefix 4, word
substring 5, otherwise no match; the query is trimmed, stripped of leading slashes, lowercased.

Inputs → outputs: composer text, session id, session cwd, launch cwd →
`complete.slash {text, session_id}` / `complete.path {word, cwd, session_id}` → items
`{text, display, meta}` with a replacement offset → the list state in the composer, and an applied
text on Tab/Enter.

Files: `apps/tui/src/composer/completion.ts` (request planning, debounce controller, apply rules),
`apps/tui/src/composer/fuzzy.ts`, `apps/tui/src/composer/CompletionList.tsx`, tests
`completion.test.ts`, `fuzzy.test.ts`.

Edge cases / failure modes:
- `/` alone → slash request (lists everything); `/usr/lo` (second `/` in the first word) → path.
- `/model`, `/model ` and `/model x` → no request, list cleared; `/models` is an ordinary command.
- inline `/token` only at the caret end and only after whitespace; its replacement starts right
  after its own slash, whatever offset the server reports.
- the bridge's items carry no `kind`: a skill is recognised by `kind: "skill"` when present, else by
  a `meta` starting with "skill" (what the bridge sends for skills and bundles).
- request error → one disabled row "completions unavailable" with the error as meta.
- blocked (card open) → list cleared and the last-seen text forgotten, so it re-asks after.
- debounce and staleness use an injected clock (the `@indus/client` `Clock`).

Our design differences / decisions:
- The bridge reply has no `replace_from`; we read it when present and default to 1 for slash.
- Launch cwd comes from `INDUS_LAUNCH_CWD` (set by the launcher, P12-32), falling back to `PWD`
  at start-up; the session cwd comes from `session.info` (`info.cwd`).
- No widget-app merge (no widget registry in this phase).

Tests we will write:
- fuzzy tier table (0..5 and no match), normalisation, stable order on ties, empty query keeps order.
- `/mo` → slash; `/model `, `/model` → none; `see /rev` → slash with skills only and replacement
  at the token; `~/pro` → path with the session cwd; no session cwd → launch cwd.
- debounce: two quick changes send one request; a reply for an older text is dropped.
- apply rules: slash row on a slash, whitespace-only change does not swallow Enter.

## Decisions for the lead
- `CompletionItem` could gain an optional `kind` (`command|skill|quick|path`) in the protocol; the
  client already honours it when present.
- Launcher should export `INDUS_LAUNCH_CWD` (P12-32).

Compare checklist (filled after the rewrite; reopened `useCompletion.ts`, `fuzzyScore.ts`, `slash.ts`):
- [x] tiers and the description offset (test: `apps/tui/src/composer/fuzzy.test.ts`)
- [x] `/model` none; inline skill only; path regex incl. drive letters and `@` (test: `apps/tui/src/composer/completion.test.ts`)
- [x] stale reply dropped, timer cleared on change (test: `completion.test.ts::debounce and staleness`)
- [x] error row (test: `completion.test.ts::debounce and staleness` "shows an unavailable row")
- [x] gap found: an inline `/token` is checked before the command shape (`/help /cle` completes the
      skill), pinned by `completion.test.ts::which request`
- [x] gap found: the whole description also counts for the description tiers (multi-word queries),
      pinned by `fuzzy.test.ts::fuzzy tiers` ("start a side", "side conversation")
- [x] Enter-apply ignores whitespace-only deltas (test: `completion.test.ts::apply on enter`)
- Deliberate differences: no widget merge; skill detection by meta when `kind` is absent.
- Not taken: local-command override for Enter (our P12-24 owns the local table; the hook takes an
  `isKnownCommand` callback that defaults to "no").

## Status

Done (2026-10-19).
