# Task: Learning timeline and map data (P09-23) Phase: 09 Owner: builder slice F

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/learning_graph.py` (which
skills count as learned, memory cards and their ids), `agent/learning_mutations.py` (delete / edit
of a node, stale ids), `hermes_cli/journey.py` (verbs and flags; the colour/palette code skimmed
only). Evidence: none.

What it does (own words): a view of what this profile has learned. Nodes are skills with a learning
signal — made in this profile by the review (`origin: agent`), by the user or `/learn`
(`origin: learn`), or used at least once — and every memory entry. Hand-written skills never used,
bundled skills, external and archived skills are left out. A memory node's id is
`memory:<store>:<index>:<sha8>` (store `memory` or `user`, the entry's position, the first 8 hex of
the SHA-256 of its text); the text part keeps the id meaningful after the list shifts. `indus
journey` (alias `learning`) prints a static timeline, newest first; `--animate` plays it in from
the oldest with `--fps`, `--width`, `--no-color`; `list [--json]`, `delete <id> [-y]` (a skill is
archived and restorable with `indus curator restore`; a memory entry is removed through the memory
store with its lock), `edit <id>` (opens `SKILL.md` in place, or the entry in a temp file, in
`$VISUAL`/`$EDITOR`; the result is validated and a bad skill file is put back as it was).
`journey.nodes(scope)` is the data API for the TUI (P12) and desktop map (P20).

Inputs → outputs: home (usage sidecar, skills tree, memories) → node dicts `{id, kind, label,
category, timestamp, when, use_count, state, origin, pinned, store?}`; CLI lines / JSON
`{nodes: [...], counts: {...}}`.

Files: `src/indus/learning/journey.py`, `src/indus/cli/commands/journey.py`,
`tests/unit/learning/test_journey.py`; `/journey` handler in `cli/slash/learning_cmds.py`.

Edge cases / failure modes:
- a memory id whose text changed → "stale id, list again"; malformed ids → clear error.
- duplicate memory texts are one entry in the store, so the first match is the entry.
- delete of a pinned skill → refused with the unpin hint.
- edit with no editor change → "no changes"; editor exits non-zero → nothing saved; invalid
  frontmatter or a name change → refused and the original bytes written back.
- memory edit to empty text → refused (use delete); over the store limit → the store refuses.
- no nodes → one line saying nothing has been learned yet.

Our design differences / decisions: no graph edges or clusters (the map layout belongs to P12/P20;
`nodes()` returns what they need); the timeline is plain text rows with an optional ANSI colour,
no palette engine; ids use 8 hex chars (the plan's `sha8`).

Tests we will write: node selection on a fixture home (unused hand-written skill excluded, used
hand-written included, agent and learn included, bundled excluded); memory id stable across reload;
delete of a memory node removes exactly that entry; delete of a skill archives it; edit with a fake
`$EDITOR` writing invalid frontmatter is rejected and the file restored; valid edit saved; `--json`
schema; `--animate` frames with an injected sleep.

## Wiring for the lead
- `/journey` handler: `cli/slash/learning_cmds.HANDLERS["journey"]` (see the curator-cli note).

## Decisions for the lead
- Skill edits from the journey write `SKILL.md` directly after validation and record a ledger
  `edit` entry with actor `user` (through the curator ledger port), not through `skill_manage`.

Compare checklist (filled after the rewrite; reopened the three reference files):
- [x] learning signal: agent / learn origin or used; external excluded (test:
  `test_journey.py::test_node_selection`)
- [x] memory id carries a text digest and resolves by it (test: `test_memory_id_stable`)
- [x] delete archives a skill, refuses pinned (tests: `test_delete_skill_archives`,
  `test_delete_pinned_refused`)
- [x] memory delete goes through the store lock (test: `test_delete_memory_entry`)
- [x] edit validates (test: `test_edit_invalid_restores`)
- Deliberate differences: no edges/clusters/density stats; the CLI has no `--reveal`.
- Not taken: legacy ids without a digest (we never minted those).

## Status

Done (2026-10-19). Shared: `cli/app.py` rows.

## Integration (lead, P09 wave 2 landing)
Skill edits are ledgered through `ledger_port` with the scope, so they reach the profile's `SkillLedger`. D-16.158.
