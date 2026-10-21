# Task: Slash commands (P20-24) Phase: 20 Owner: wave-34 builder C

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/src/lib/desktop-slash-commands.ts`
(structure; registry-derived specs, extension test, catalogue filter). Research 08 §3.13 (slash),
R08.12. Our own `src/indus/service/commands/registry.py` (the `desktop` surface) and
`apps/tui/src/slash/dispatch.ts` (P12-24 dispatch order). Closed before writing.
Evidence: none.

What it does (own words): the desktop curates slash commands by **hiding a known list only**: the
built-ins our Python registry marks as not offered on the `desktop` surface (terminal-only such as
`/clear`, `/quit`; messaging-only such as the chat-platform commands) plus a few terminal display
toggles. Everything the list does not name — skill commands, quick commands, plugin commands, a
built-in added later — passes through both completion paths (`complete.slash` answers and the
`commands.catalog` list) and runs when typed (R08.12). Five built-ins run locally: `/new` (fresh
draft), `/model` (opens the picker), `/details` (tool detail level), `/reasoning` (show/hide or an
effort level, live), `/help` (lists the curated catalogue). Any other command goes to `slash.exec`;
only a `SLASH_NOT_OWNED` refusal falls back to `command.dispatch` (as P12-24). A skill (or `send`)
result is a prompt submitted the normal way (journal, busy rules); `prefill` fills the composer;
`alias` re-routes (bounded hops). A worker failure or timeout (`SLASH_WORKER_FAILED`) is shown as
itself with its message, never as "unknown command". When the daemon is unreachable, completion
uses the offline catalogue `src/chat/slashCatalog.json`, generated at build time by
`scripts/dump-slash-catalog.mjs` from the Python registry (`uv run python -c …`), with `--check`
for CI.

Inputs → outputs: typed `/line` → local action | `slash.exec` → (`command.dispatch`) → output, prompt,
prefill or switch; completion text → curated `CompletionItem`s.

Files: `apps/desktop/src/chat/slash.ts`, `apps/desktop/src/chat/slash.test.ts`,
`apps/desktop/scripts/dump-slash-catalog.mjs`, generated `apps/desktop/src/chat/slashCatalog.json`.

Edge cases / failure modes:
- hidden built-in typed in full: "not available in the desktop app" (never sent);
- typed alias of a desktop-owned command (`/reset` → `/new`) runs locally;
- no session yet: server commands say "send a message first";
- daemon down while typing: offline completion, execution reports the connection error;
- stale result after the person switched session: dropped.

Our design differences / decisions:
- the hidden list is derived from the registry's `desktop` surface (one source of truth), not a
  hand-kept table; the catalogue JSON carries every built-in with a `desktop` flag.

Tests we will write:
- a new skill command (unknown to the catalogue) appears in completion (live and catalogue paths) and
  runs (slash.exec → not owned → command.dispatch → skill → submit);
- a hidden built-in is absent from completion and refused when typed;
- local built-ins never reach the daemon; worker timeout shown as itself; offline fallback;
- dump script `--check` matches the committed JSON.

## Decisions for the lead
- Extra hidden names not in the registry: the TUI's own local commands (`busy`, `copy`, `image`,
  `logs`, `mouse`, `paste`, `prompt`, `redraw`, `reload`, `statusbar`, `theme`, `update`) —
  `EXTRA_HIDDEN` in `slash.ts`. Moving them into the registry with terminal surfaces would make the
  dump the only source.

Compare checklist (filled after the rewrite; reopened the files in `Read:`):
- [x] Unknown commands pass both completion paths and run (tests: `slash.test.ts::a new skill command
  appears in both completion paths`, `::a new skill command runs: slash.exec, not owned,
  command.dispatch, submitted`).
- [x] Hidden built-in absent and refused (tests: `::hides only known built-ins…`, `::live completion
  is curated`, `::a hidden built-in typed in full is refused without a call`).
- [x] Desktop-owned built-ins local (test: `::local built-ins never reach the daemon`).
- [x] Not-owned fallback only (test: `::dispatch only after a not-owned refusal`).
- [x] Worker timeout shown as itself (test: `::a worker timeout is shown as itself`).
- [x] Offline catalogue fallback; dump `--check` (tests: `::falls back to the offline catalogue…`,
  `::the dump script --check agrees with the Python registry`).
- [x] Typed alias of a desktop-owned command runs locally (`/reset` → `/new`); the reference also
  surfaces an exactly typed alias in completion — ours relies on the daemon's completion (it lists
  aliases); offline completion lists canonical names only.
- Deliberate differences: registry-derived curation; the reference's per-command desktop surfaces
  (pickers, settings links) are reduced to the five local built-ins.
- Not taken: subcommand allow-lists per command, skin completions (no skins in 1.0).

## Status

Done (2026-10-21).
