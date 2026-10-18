# Task: Slash-command registry (P06-08) Phase: 06 Owner: P06 slice C builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/commands.py` (whole:
entry shape, lookup table, help grouping, subcommand table, busy policies),
`website/docs/reference/slash-commands.md` (headings and the session table, for the command
list). Research `04-cli-config.md` §3.7.1, `05-tui.md` §3.10 (backend registry list). Closed
before writing.
Evidence: none.

What it does (own words): one ordered table of every slash command the product knows. Every
surface reads it: the classic CLI for dispatch, tab completion and `/help`; later the TUI bridge,
the gateway, ACP and the desktop catalogue. An entry says what the command is called, its aliases,
which category it belongs to, a short argument hint, its tab-completable subcommands, which
surfaces offer it, what happens when it arrives while a turn runs, how a composer should treat its
argument, and which phase implements it. Names and aliases are matched without regard to case and
resolve to the one canonical entry, so adding an alias in the table updates every surface.

**Location (lead ruling):** the definitions and the alias/prefix resolver live in
`indus.service.commands` (service layer, below `cli`, `tui_bridge`, `gateway`, `acp`), so those
surfaces read one table without importing the CLI. Each surface keeps its own handlers; the classic
CLI's are under `src/indus/cli/slash/` (P06-09/10).

Inputs → outputs:
- `CommandDef(name, description, category, aliases, args_hint, subcommands, surfaces, busy_policy,
  argument_mode, owner)`; `description_key` is `slash.<name>` (the English catalogue key) and
  `description` its English fallback. `category` ∈ session|config|tools|info|exit;
  `busy_policy` ∈ dispatch|reject|interrupt_then_dispatch (default reject); `argument_mode`
  ∈ options|text|mixed or inferred (subcommands → options or mixed; a hint alone → text).
- `CommandRegistry(defs)` validates (lowercase names, no name or alias used twice, known
  categories/surfaces/policies) and offers `resolve(word)`, `commands(surface)`,
  `by_category(surface)`, `prefix_matches(word, names)`, `complete(text, names)` and
  `describe(cmd, catalogue)`.
- `REGISTRY`: the built-in table. P06 commands carry `owner="P06"`; commands later phases
  implement (named in their phase files' registry rows) are already listed with their owner phase,
  so a surface can say "arrives with Pnn" instead of "unknown command".

Files: `src/indus/service/commands/__init__.py`, `src/indus/service/commands/registry.py`,
`tests/unit/service/commands/test_registry.py`; shared: `locales/en.yaml` (`slash.*` keys).

Edge cases / failure modes:
- Leading `/` optional in `resolve`; a word with a second `/` (a path such as `/usr/bin`) is not a
  command (`looks_like_command`).
- Completion lists every name and alias once, never twice, sorted; after a space it completes the
  command's subcommands.
- A duplicate name or alias in the table raises at construction (caught by the unit test, so a bad
  table never ships).
- Prefix expansion: an exact name wins; otherwise a single prefix match expands; several → the
  caller lists them; none → unknown.

Our design differences / decisions:
- Field `surfaces: frozenset[cli|tui|gateway|desktop]` instead of the reference's two booleans plus
  a desktop disposition string; `owner` records the implementing phase.
- Categories are five plain ids; the reference's help sub-groups are not kept.
- No vendor-account commands (billing, portal login), no wake word; names and copy are ours.
- `describe` takes an optional catalogue mapping; Python has no catalogue loader yet, so the English
  fallback in code is used and a test keeps `locales/en.yaml` `slash.*` equal to it.

Tests we will write:
- No duplicate names/aliases; every entry has a CLI handler or a declared owner phase other than
  P06; alias resolution is case-insensitive; completion lists aliases once; subcommand completion;
  help grouping golden; argument-mode inference; prefix matches; catalogue parity with en.yaml;
  `indus.service.commands` imports nothing from `indus.cli` (AST check; `lint-imports` enforces the
  layer contract).

## Decisions for the lead
- Planned commands of later phases are listed now with `owner` (P08 `/fast`; P09 `/skills`,
  `/memory`, `/learn`, `/refine`, `/curator`, `/journey`; P10 `/btw`, `/moa`, `/goal`, `/subgoal`,
  `/loop`, `/heartbeat`, `/voice`, `/browser`, `/agents`; P11 `/rollback`, `/worktree`,
  `/checkpoints`; P13 `/reload-mcp`, `/mcp`, `/plugins`; P14 `/cron`; P15 `/approve`, `/deny`).
  Their phases replace `owner` and add handlers. Easy to drop if the lead prefers a P06-only table.
- Tab completion: the registry offers `complete(text, names)` returning plain candidates; the
  prompt_toolkit `Completer` wrapper belongs to the REPL (slice B), which keeps prompt_toolkit out
  of the service layer.

Compare checklist (filled after the rewrite; reopened `hermes_cli/commands.py`):
- [x] entry fields and busy-policy values (test: `test_registry.py::test_busy_policies_are_known`)
- [x] alias resolution through one table, case-insensitive (test: `::test_resolve_aliases_case_insensitive`)
- [x] subcommands complete after a space (test: `::test_complete_subcommands`)
- [x] argument-mode inference (test: `::test_argument_mode_inference`)
- Deliberate differences: surfaces set instead of cli_only/gateway_only/desktop strings; owner
  phase field; five categories without help sub-groups; no lazy module `__getattr__` tables.
- Not taken: config-gated gateway availability (P15 decides), executor keys (P12/P17 decide).

## Status

Done (2026-10-18). Shared: `locales/en.yaml` gains a `slash:` section.
