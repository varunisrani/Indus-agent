# Task: `indus import-agent` (P24-18) Phase: 24 Owner: wave-37 builder X1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/agent_import.py`
(all: source files per agent, mapping rules, secret stripping, preview/confirm flow, report),
`hermes_cli/agent_import_sync.py` (all: manifest, digests, managed skills, prompt-free sync);
research 04 §3.3 (`import-agent` row) and §3.28. Ours: `cli/prompts.py` (`io_for`, `confirm`),
`cli/commands/mcp.py` (round-trip writer use, secret-looking values refused),
`safety/approvals/policy.py` + `floors.py` (`command_allowlist`, `approvals.deny`, floor order),
`core/prompt/persona.py`, `core/prompt/context_files.py` (what is read in place),
`learning/memory/store.py` (entries, delimiter, limits), `learning/skills/resolver.py` (skill
roots), `foundation/redact.py`. Closed before writing.
Evidence: none.

What it does (own words): brings another coding agent's setup into the current profile. Two
sources: `claude-code` (default `~/.claude`) and `codex` (default `~/.codex`); `--source` points
at another folder, `--project DIR` adds that project's own files. The command first builds a plan
by reading only what the mappings need, prints a preview with every mapping (what, from where, to
where, and its status), and asks before writing; without a terminal it needs `--yes` (else exit 1
after the preview); `--dry-run` previews and stops. After a real import the sources are recorded
in `<home>/import-sync.json`; `--sync` re-imports, without asking, only the recorded sources whose
bytes changed since the last import.

Mappings:
- Home instruction file (`~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`) → a persona overlay: a
  marked block per agent inside `<home>/PERSONA.md` (the file is seeded with the built-in
  identity when it is missing or empty, so the import never replaces who the agent is). The block
  is replaced on re-import; the text is capped at 20,000 chars.
- Project instruction file (`<project>/CLAUDE.md`, `<project>/AGENTS.md`) → project context:
  Indus already reads these in place, so nothing is copied; the preview says whether discovery
  will pick the file or which file shadows it.
- Permission rules (Claude `settings.json` / project `.claude/settings.local.json`
  `permissions.allow|deny`): `Bash(cmd)` → exact text; `Bash(prefix:*)` → two entries, the
  prefix itself and `prefix *`; bare `Bash`/`Bash(*)` is refused as too broad; other tools have no
  equivalent and are listed as skipped. Allow → `command_allowlist`; deny → `approvals.deny`. The
  approval floors (hardline, interpreter guard, deny globs) still run before the allowlist (R02.4),
  so an imported allow rule never lets a hardline command through.
- MCP servers (Claude `~/.claude.json` next to the source folder and `settings.json`
  `mcpServers`; Codex `config.toml` `[mcp_servers.*]`; project `.mcp.json` and
  `.codex/config.toml`) → `mcp_servers` through the round-trip config writer. Env values are
  imported by name only (`NAME: ${NAME}`; the value is never copied); Codex
  `bearer_token_env_var` / `env_http_headers` become `${NAME}` header references; a literal
  credential header or a secret-looking header value is dropped and reported by name; a server
  whose URL or arguments look like they carry a credential is refused. Project-defined servers are
  imported disabled.
- Skills (`skills/<name>/SKILL.md`) → `<home>/skills/<agent>-imports/<name>/`, regular files only
  (no symlinks), credential-named files left behind.
- Codex memories (`memories/*.md`) → memory store entries (headings as prefixes, bullets and
  paragraphs as entries, code blocks and tables skipped), the incoming set capped at 20,000 chars;
  entries that do not fit the store's own limit are reported, never forced in.
- Claude `commands/` and Codex `prompts/` → listed as skipped (no direct equivalent; make skills).
- Credential files (`.credentials.json`, `auth.json`, `credentials.json`, `.env`, keys) are never
  opened, never digested, never copied. There is no flag that imports credentials.

Inputs → outputs: `indus import-agent [claude-code|codex] [--source DIR] [--project DIR]
[--dry-run] [--overwrite] [-y/--yes] [--sync]` → preview on stdout; writes `config.yaml`
(allowlist, deny, MCP), `PERSONA.md`, `memories/MEMORY.md`, `skills/<agent>-imports/…`,
`import-sync.json`. Exit 0 done / nothing to do / dry run; 1 guidance (no source found, two found,
no terminal and no `--yes`, declined) or any mapping failed; 2 usage.

Files: `src/indus/cli/importers/__init__.py`, `src/indus/cli/importers/agents.py` (plan, mapping
rules, apply, manifest), `src/indus/cli/commands/import_agent.py` (command), fixture homes under
`tests/fixtures/import_agent/`, `tests/unit/cli/commands/test_import_agent.py`; shared rows in
`cli/app.py`, `tests/contract/cli/non_tty_table.yaml`, `tests/contract/cli/test_help_catalogue.py`.

Edge cases / failure modes:
- No source folder, both found without a choice, a custom `--source` that is not a folder.
- Unparseable `settings.json` / `config.toml` / `.mcp.json`: that mapping is an error item, the
  others still import; an unreadable or invalid `config.yaml` refuses every config mapping and
  leaves the file untouched (the writer's failed-read refusal); a managed install likewise.
- An MCP name or skill already present: conflict unless `--overwrite`; a skill the last import
  copied and the user has not edited since is refreshed by `--sync` (digest kept in the manifest).
- Memory store unreadable or full: reported, nothing forced.
- `--sync` with no manifest: says so, exit 0; a recorded source that is gone: skipped with a note.
- Re-running an import with nothing new: every item is "already present", exit 0, no writes.

Our design differences / decisions:
- Instruction files go to a persona overlay / project context, not into the memory store (the row
  says so; the store's default limit is 2,200 chars).
- Allow rules from a repository's committed `.claude/settings.json` are not imported (the
  allowlist is profile-wide; a repo must not widen it); its deny rules are.
- Project files are read only with `--project DIR` (import reads only what the user points at).
- `--sync` re-applies only the mappings whose source files changed (per-file digests), not the
  whole agent.

Tests we will write:
- Preview lists every mapping for both fixture homes; dry run writes nothing; non-TTY without
  `--yes` exits 1 after the preview and writes nothing.
- A token in a source MCP env is imported as `${NAME}` only (value absent from every written file
  and from stdout); credential headers dropped; secret-looking args refused.
- Allow/deny mapping table, deny kept as deny globs and floors still block a hardline command.
- Persona overlay block seeded/replaced; memories capped; skills copied without credential files.
- Conflicts and `--overwrite`; `--sync` touches only changed sources.

## Decisions for the lead
- Home instruction files become a marked block in `PERSONA.md` seeded with the built-in identity;
  change: point the mapping at another target in `agents.py` (`_persona_item`).
- Project files only with `--project`; committed project allow rules are never imported; project
  MCP servers arrive disabled.
- Every MCP env value is imported as a `${NAME}` reference, even non-secret ones (the row says "by
  name only"); the preview lists the names to set.
- Claude `:*` prefix rules map to the exact prefix plus `prefix *`.

Compare checklist (filled after the rewrite; reopened `hermes_cli/agent_import.py`,
`hermes_cli/agent_import_sync.py`):
- [x] Every source file per agent (instructions, settings permissions, MCP servers from the home
  config file and the settings file, skills, Codex memories, commands/prompts listed as skipped)
  (test: `test_import_agent.py::test_the_claude_code_preview_lists_every_mapping`,
  `::test_the_codex_preview_lists_every_mapping`)
- [x] Preview always first; dry run stops; no terminal → preview only (test:
  `::test_without_a_terminal_the_import_needs_yes`, `::test_a_terminal_answer_of_no_changes_nothing`)
- [x] Credential files never read; secrets never imported (test:
  `::test_credential_files_are_never_opened`, `::test_a_token_in_an_mcp_env_is_imported_by_name_only`,
  `::test_servers_that_would_carry_a_credential_are_refused`)
- [x] Unreadable settings file refused and left byte-identical, also in a dry run (test:
  `::test_an_unreadable_settings_file_is_left_alone`)
- [x] Conflicts unless `--overwrite`; a skill edited since its import is never clobbered (test:
  `::test_conflicts_and_overwrite`, `::test_sync_refreshes_an_untouched_skill_copy_and_spares_an_edited_one`)
- [x] Memory merge: dedupe, cap, overflow reported (test:
  `::test_memories_become_entries_and_respect_both_caps`,
  `::test_memories_that_do_not_fit_the_store_are_reported`)
- [x] Sync: prompt-free, only registered sources, failed items keep their old digest, gone
  sources skipped, dry run leaves the manifest (test: `::test_sync_touches_only_changed_sources`,
  `::test_sync_without_records_and_dry_run`)
- [x] Gap found: a server defined in both Claude files must come from the home config file —
  fixed by `servers_seen` in the planner, pinned by
  `::test_a_server_in_both_claude_files_comes_from_claude_json`
- [x] Gap found: a source file absent at import time and created later must trigger `--sync` —
  fixed by `Plan.watched`, pinned by `::test_sync_sees_a_source_file_that_appeared_after_the_import`
- [x] OpenClaw migration absent (D-16.50).
- Deliberate differences: the reference merges instruction files into the memory store; we
  write a persona overlay (home) or read them in place (project), per the row. The reference keeps
  non-secret env values; we import every env value by name (row: "by name only"). The reference
  maps `Bash(x:*)` to `x*`; we map it to `x` and `x *` (no match on `xfoo`). The reference digests
  the whole agent; we digest per source file and re-apply only the changed mappings. The
  reference's confirmation defaults to yes; ours defaults to no (P07 `confirm`). Without a
  terminal the reference exits 0 after the preview; we exit 1 with guidance (R04.19).
- Not taken: a pre-write `.bak.<time>` copy of the memory file (our store only appends, under its
  lock, atomically); dropping file-name headings (`CLAUDE.md`…) from memory prefixes.

## Status

Done (2026-10-21). Shared files touched: `src/indus/cli/app.py` (`import-agent` row),
`tests/contract/cli/non_tty_table.yaml`, `tests/contract/cli/test_help_catalogue.py`,
`docs/internals/traps-ledger.md` (R02.4, R04.19 lines). No ACC row exists for I46 yet (P3, not in
the acceptance map).
