# Task: Slash dispatch (P12-24) Phase: 12 Owner: wave-8 builder R4

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `ui-tui/src/app/createSlashHandler.ts`
(whole), `ui-tui/src/app/slash/commands/core.ts`, `.../session.ts`, `.../ops.ts` (command names,
aliases, arguments and the RPCs each uses; `/quit`, `/update`, `/clear` bodies), test names of
`ui-tui/src/__tests__/createSlashHandler.test.ts`. Research `05-tui.md` §3.9–3.12, R05.10, R05.12.
Ours: `docs/notes/P12/bridge-methods.md` (`commands.catalog`, `slash.exec`, `command.dispatch`,
error codes), `docs/notes/P06/slash-dispatch.md`. Evidence: none.

What it does (own words): a line starting with `/` is a command. The renderer first looks in its own
registry (name or alias, any case). If not found it consults the catalogue from `commands.catalog`:
an exact alias runs the command it stands for; otherwise one best fuzzy match on names (prefix beats
substring) runs that command, several equally good matches print "ambiguous" with up to six names,
and descriptions never run anything. What is left goes to the server with `slash.exec`. Only the
"not owned by this executor" refusal falls through to `command.dispatch`; a crash, a timeout or an
unknown command is shown as what it is (R05.12). Results: plain output becomes a system line, or the
pager when long; `prefill` fills the composer; `submit`/`send`/`skill` send a prompt (a skill sends
the expanded body while the transcript shows the short display text); `alias` runs the target.
Each run takes a flight number and the destination (session + revision); any result that arrives
after a newer command or after a session switch is dropped (R05.10). Slash commands never wait in
the busy queue. `/clear` and `/new` ask a confirm card first unless `INDUS_TUI_NO_CONFIRM` is set.

Local commands: help, quit/exit, update (exit 42 after 100 ms), clear/new, redraw, status, title,
details, copy, paste, prompt, logs, history, save, statusbar, queue, steer, undo, retry, bg, btw,
model, sessions, image, compress, branch, theme (alias skin), yolo, reasoning, fast, busy, verbose,
usage, stop, reload-mcp, reload, tools, skills, mouse.

Inputs → outputs: command line → registry hit / RPCs (`slash.exec`, `command.dispatch`,
`session.*`, `config.*`, `prompt.background`, `image.attach`, `reload.mcp`) → system lines, pager,
prompt submission, composer prefill, confirm card, exit code.

Files: `apps/tui/src/slash/dispatch.ts`, `apps/tui/src/slash/localCommands.ts`,
`apps/tui/src/slash/types.ts`, tests `dispatch.test.ts`, `localCommands.test.ts`.

Edge cases / failure modes:
- no session yet → server commands answer "no session yet" without a call.
- alias chains are bounded (4 hops) so a catalogue loop cannot spin.
- catalogue missing → straight to the server.
- `UNSUPPORTED_COMMAND` → "unknown command"; `SLASH_WORKER_FAILED` / client timeout → shown as a
  failure of that command, never as unknown, never followed by `command.dispatch`.
- empty skill/send body → a system line saying the command produced nothing to send.
- `/clear` while a turn runs → "stop the turn first".

Our design differences / decisions:
- hooks into other slices (composer, clipboard, editor, sessions, logs, transcript) are injected and
  optional; a missing hook prints that the feature is not available yet.
- commands the bridge owns that need no client state (`stop`, `tools`, `reload`, `skills`) forward
  to `slash.exec` through the same path, so their errors read the same.
- `btw` runs as `prompt.background` (our protocol has no side-question method).
- name scoring for the catalogue is our own two tiers (prefix, then substring).

Tests we will write: fake server: exec failure shown not unknown; not-mine falls through to
dispatch; skill shows display text and submits the body; ambiguous lists 6; late result after `/new`
dropped; local alias case-insensitive; `/update` exits 42 after 100 ms; confirm before `/clear`;
`INDUS_TUI_NO_CONFIRM`; long output pages.

## Decisions for the lead
- `CommandDispatchResult` has no `display` / `notice`; dispatch reads them when present and falls
  back to `/<name> <arg>` as the shown text.
- `!cmd` (`shell.exec`) is routed by R3's submission (P12-19); this dispatcher handles `/` lines and,
  like `!cmd`, never goes through the busy queue.
- Hooks owned by other slices (`newSession`, `switchSession`, `openSessions`, `copy`, `paste`,
  `openEditor`, `logLines`, `transcriptText`, `enqueue`, `queued`, `redraw`) are optional fields of
  `SlashContext` for P12-27 to wire.

Compare checklist (filled after the rewrite; reopened `createSlashHandler.ts`, command files):
- [x] registry first, case-insensitive (test: `apps/tui/src/slash/dispatch.test.ts::local names and aliases match in any case`)
- [x] catalogue exact alias, one best name match, ambiguous lists 6, descriptions never run (test:
  `dispatch.test.ts::catalogue matching`, `::an ambiguous word lists six names`, `::an exact alias and a unique prefix run the canonical command`)
- [x] only not-owned falls through; worker failure and timeout shown as themselves (test:
  `dispatch.test.ts::a worker failure is shown as itself, never as unknown, and never falls through`,
  `::a client-side timeout is shown as a timeout`, `::only a not-owned refusal falls through to command.dispatch`)
- [x] result kinds exec/plugin/alias/skill/send/prefill and exec fields (test:
  `dispatch.test.ts::a skill submits the body while the transcript shows the invocation`, `::a send result shows its notice, then submits`,
  `::an alias result runs its target`, `::exec results: long output pages, prefill fills the composer, submit sends`)
- [x] late results dropped after a session switch and after a newer command (test:
  `dispatch.test.ts::drops a late result after /new changed the session`, `::drops a late result once a newer command ran`)
- [x] `/update` exits 42 after 100 ms; `/clear` confirm, `INDUS_TUI_NO_CONFIRM`, busy refusal (test:
  `apps/tui/src/slash/localCommands.test.ts::leaving`, `::destructive commands`)
- [x] a later-phase command (not owned, then not dispatchable) shows the bridge's own refusal, not
  "unknown" (test: `dispatch.test.ts::not owned and not dispatchable shows the first refusal`)
- Deliberate differences: dispatch-kind output from `command.dispatch` pages when long (the
  reference keeps it a line); no `warning` prefix (not in our result).
- Not taken: shared metrics / command observation, widget apps, hosted-dashboard refusals, the
  destructive-confirm setting in `approvals` (our switch is the env var named in the row).

## Status

Done (2026-10-19). `types.ts` and the test helper `testContext.ts` added beside the row's files.
