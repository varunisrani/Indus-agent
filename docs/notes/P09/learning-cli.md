# Task: Learning CLI and slash surface (P09-24) Phase: 09 Owner: P09 wave 2 slice D builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/skills_config.py` (skim),
`hermes_cli/memory_setup.py` (status only; provider setup is P13). Closed before writing.
Evidence: none.

What it does (own words):

`indus skills`:
- `list [--source local|create_dir|external|bundled|hub] [--json]`: every scanned skill with its
  category, where it comes from (bundled and hub from their manifests, otherwise its tier), its
  usage state and description; `--json` prints the rows as a JSON array;
- `view <name>`: the `SKILL.md` text and its support files;
- `check`: validates every skill in every root against P09-05 and runs the linter; prints one line
  per problem and per finding; exit 1 when any skill is invalid or a name is ambiguous (lint alone
  never fails);
- `list-modified`: bundled skills the user edited (P09-21 `list_modified`);
- `diff <name>`: unified diff of the live copy against the shipped one;
- `sync`: P09-21 `sync_bundled`, summary line; exit 1 when something failed;
- `opt-out [--remove] [-y]`: writes the marker; `--remove` also deletes untouched bundled copies
  (asks first; without a terminal needs `-y`, else exit 2);
- `opt-in [--no-sync]`: removes the marker and syncs;
- `reset <name> [--restore] [-y]`: forget the origin row; `--restore` replaces the copy with the
  shipped one (asks first);
- `pending`, `approve <id|all>`, `reject <id|all>`: staged skill writes (P09-11 with the gate on).

`indus memory`:
- `show [memory|user]`: the entries of each enabled store with usage;
- `status`: each store on/off, usage, limit, file, the approval gate and the pending count;
- `edit <memory|user>`: opens the store file's text in `$EDITOR` (a copy), then saves it under the
  store lock only when the file did not change meanwhile; needs a terminal (exit 2 otherwise);
- `pending`, `approve <id|all>`, `reject <id|all>`: P09-03 records, approvals in creation order with
  every replaced entry printed in full;
- `approval on|off`: writes `memory.write_approval`.

Slash (classic CLI handlers; the registry entries are shared by every surface):
- `/memory [pending|approve <id|all>|reject <id|all>|approval [on|off]]` (bare = pending);
- `/skills [list|view <name>|reload|pending|approve <id|all>|reject <id|all>]` (bare = list);
- `/reload-skills` = `/skills reload`: rescans the skill folders, rebuilds the REPL's skill and
  bundle slash table, clears the list cache; the system prompt is not touched (the index delta
  note of P09-07 tells the model at the next turn);
- `/bundles`: lists the skill bundles.

Non-TTY: nothing prompts; a verb that needs a confirmation exits 2 with guidance unless `-y`.

Files: `src/indus/cli/commands/skills.py`, `src/indus/cli/commands/memory.py`,
`src/indus/cli/slash/learning_cmds.py`, `tests/unit/cli/commands/test_skills.py`,
`tests/unit/cli/commands/test_memory.py`, `tests/unit/cli/slash/test_learning_cmds.py`.

Edge cases / failure modes:
- unknown skill for `view`/`diff`/`reset` → exit 1 with a hint; ambiguous name → the choices;
- `diff` of a skill we do not ship → exit 1; no difference → "no changes";
- `approve` of an unknown id → "not found" line, exit 1 when nothing applied;
- `memory edit` with no `$EDITOR`/`$VISUAL` → falls back to `vi`; editor exit non-zero → nothing
  saved; file changed during the edit → nothing saved, exit 1.

Our design differences / decisions:
- exit 2 (not 1) for a needed confirmation without a terminal, as the P09-24 row says (P07 commands
  use 1; noted for the lead);
- `/memory` with no word shows pending (the registry text says "Review memory writes waiting for
  approval");
- skill `pending/approve/reject` added (see skill-manage note).

Tests we will write: CliRunner per verb (fixture homes, fake clock); `check` exits 1 on an invalid
fixture skill; `approve all` applies staged memory writes in creation order; `/reload-skills`
registers a new slash command while prompt bytes stay identical; non-TTY confirmations exit 2.

## Wiring for the lead
- `cli/app.py` REGISTRY gains `skills` and `memory` rows (done here, shared).
- `cli/slash/dispatch.py` HANDLERS merges `learning_cmds.HANDLERS` (done here, shared).
- The REPL should fill `ctx.extensions.skill_commands` with
  `learning_cmds.build_skill_commands(scope, session_id)` at start; `/reload-skills` calls the same.
- Registry entries `reload-skills` and `bundles` added, `skills`/`memory` gain subcommands
  (`service/commands/registry.py` and `locales/en.yaml`, shared).

## Decisions for the lead
- `indus memory edit` copies the file to a temp file for the editor instead of holding the store
  lock during an interactive edit (the lock would make agent writes time out).

Compare checklist (filled after the rewrite; reopened both reference files):
- [x] status shows each store on/off and the memory tool state (test: `test_memory.py::test_status`)
- [x] disabled lists read per platform (ours: resolver; `test_skills.py::test_list_json_and_source`)
- Deliberate differences: no curses checklist to toggle skills (config `skills.disabled` via
  `indus config`); no provider setup (P13).
- Not taken: provider plugins listing (P13).

## Status

Done (2026-10-19). Shared: `src/indus/cli/app.py`, `src/indus/cli/slash/dispatch.py`,
`src/indus/service/commands/registry.py`, `locales/en.yaml`, and the golden lists in
`tests/unit/service/commands/test_registry.py` (help groups, `/re` completion, a `landed_names`
set for the P09 commands that have CLI handlers so far) and `tests/unit/cli/slash/test_dispatch.py`
(`/re` ambiguity line now includes `/reload-skills`). When slices E/F land their handlers, replace
`landed_names` with `"P09"` in the `landed` set.

## Integration (lead, P09 wave 2 landing)
`build_skill_commands` was folded into wave 1's `cli/slash/skill_cmds.skill_commands(scope, session_id)`, the one builder for REPL start and `/reload-skills`; `learning_cmds.py` holds the HANDLERS of D, E and F in one map (in `dispatch.HANDLERS`). `skills`/`memory` rows added to the help order and the non-TTY contract table. D-16.151.
