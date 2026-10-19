# Task: `skill_manage` tool (P09-11) Phase: 09 Owner: P09 wave 2 slice D builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/skill_manager_tool.py`
(limits, actions, side effects), `tools/skill_manager_guards.py` (each guard),
`tools/skill_manager_batch.py` (atomicity). Closed before writing.
Evidence: none.

What it does (own words): the model's one tool for writing skills. A call is either one action at
the top level (`action`, `name`, …) or `operations: [...]`, a list of such actions applied all or
nothing.

Actions:
- `create {name, content, category?}`: a new skill folder holding `SKILL.md`, in `skills.create_dir`
  when set, else `<home>/skills/` (the schema description names the folder in use);
- `patch {name, old_string, new_string, file_path?, replace_all?}`: replace text in `SKILL.md` or a
  support file; the old text must occur exactly once unless `replace_all`; an empty `new_string`
  deletes it. `patch {name, content}` rewrites `SKILL.md` whole (`edit` is an alias). Both shapes in
  one op → error;
- `delete {name, absorbed_into?}`: remove the skill (`absorbed_into` must name another existing
  skill);
- `write_file {name, file_path, file_content}` / `remove_file {name, file_path}`: support files under
  `references/ templates/ scripts/ assets/`.

How a call runs: every op's shape is checked first (pure; an op carrying another action's text key
fails naming the key and where it belongs, and nothing applies). Then the write-approval gate
(P09-03): when `skills.write_approval` is on, the whole call is staged as one pending write and the
result says so. Otherwise the per-skill locks of every named skill are taken in a fixed order
(`<home>/skills/.locks/<sha256(basename)[:16]>.lock`, outside the skill folder), the ops are played
against an in-memory overlay of the touched files (reading disk under the locks), every guard and
format rule runs on the result, and only then the changes are written. A write failure part-way
restores what was already written from the bytes read before. Deletes are done last by renaming
the folder aside (or into the archive), so a failed write can still be undone.

Guards:
- pinned skill: delete refused with an `indus curator unpin` hint; patches allowed;
- review fork (origin `background_review`): a write to existing content needs a fresh
  `skill_view` of that exact file in the same session (P09-08 read evidence, hash-matched);
  otherwise refused with "view it, then retry"; deletes allowed only for curator-managed
  (`origin=agent`, not bundled/hub), unpinned skills outside `external_dirs`, and routed to the
  archive `<home>/skills/.archive/<name>/`;
- paths: support paths must stay under the allowed folders; the real path of the target must stay
  inside the real skill folder, and the skill folder inside a real skills root (symlink redirects
  refused); a symlinked skill folder is never deleted;
- format limits from P09-05 (`check_skill_text`, `check_support_path`, `check_support_size`);
- optional scan of the written skill when `skills.scan_agent_created` is on: uses the scanner P13
  registers in `SKILL_SCANNER`; while none is registered the scan is skipped with a debug log.

After a successful write, each best-effort and never failing the call: a ledger entry (P09-13),
the `skills_list` cache cleared, usage telemetry (create records origin; patch-type actions bump
`patch_count`; delete forgets, archive sets state), lifecycle listeners (`LIFECYCLE` — a no-op list
until P13's hook bus), success listeners (`SUCCESS_LISTENERS`, where P09-14 resets the skill review
counter for foreground calls), and advisory lint findings in the result. Editing a bundled skill
makes its hash differ from the manifest, so P09-21 stops refreshing it; the result says so.

Inputs → outputs: tool args (+ `scope`, `session_id`) → JSON text
`{success, message, results?, lint?, staged?, pending_id?}` or `{error, ...}`.
`apply_pending_skill(scope, record) -> pending.Applied` replays a staged call (with a unified diff).

Files: `src/indus/learning/skills/manage_tool.py`, `tests/unit/learning/skills/test_manage_tool.py`.

Edge cases / failure modes:
- unknown action, missing name, more than 20 ops → error before anything;
- create where a folder already exists (non-empty, or a symlink) → error;
- create of a name another skill already uses (any tier) → error;
- patch with `old_string` not found or found several times (without `replace_all`) → error with a
  short preview of the file;
- a patch that breaks `SKILL.md` frontmatter → error;
- delete in a batch with other ops on the same skill → error (delete is last for its skill).

Our design differences / decisions:
- atomicity by an in-memory overlay validated before any write (the reference snapshots folders
  and rolls back after a failed op);
- delete may sit in a batch (applied last, by rename-aside); the reference allows it only alone;
- exact substring matching for `patch` (no fuzzy matcher exists here; the P05 patch tool's matcher
  is not shared) — quote folding is not applied to skills;
- origin comes from `indus.learning.pending.current_origin()` (the one write-origin variable);
- the archive move is a small local function with a hook (`ARCHIVER`) the curator can replace.

Tests we will write: each action's happy path; batch with one bad op leaves the tree byte-identical;
both patch shapes → error; wrong text slot names the key; pinned delete refused; review-fork patch
without a view refused, accepted after a view; review-fork delete of a learn-origin skill refused,
of an agent-origin skill archived; `../` refused; symlinked support folder refused; two threads
patching concurrently lose no edit; lint findings present with success; gate on → staged, approve
replays with a diff; ledger entry written; bundled edit shows in `list_modified`.

## Wiring for the lead
- Register: `indus.learning.skills.manage_tool.register_tools(registry)` next to
  `view_tools.register_tools` (toolset `skills`); add `skill_manage` with `skills_list`/`skill_view`
  to the `skills` toolset in `tools/toolsets.py` when the integrator wires skills.
- P09-14 counter reset: append a callable to `manage_tool.SUCCESS_LISTENERS`; it gets
  `(SkillWriteEvent)` with `origin`, `session_id`, `actions`, `skills`.
- Curator archive: set `manage_tool.ARCHIVER` to slice F's archive function if it has one
  (signature `(scope, record) -> Path`).

## Decisions for the lead
- No skill `pending` verbs were named anywhere, but `pending.staged_message("skills")` points users at
  `indus skills pending` / `/skills pending`; P09-24 adds `pending`, `approve`, `reject` to both.

Compare checklist (filled after the rewrite; reopened the three reference files):
- [x] limits 64 / 1,024 / 100,000 / 1 MiB and the four writable folders (via P09-05 checks;
  `test_manage_tool.py::test_limits_refused`)
- [x] both patch shapes → error; wrong slot names the key (`::test_patch_both_shapes_refused`,
  `::test_wrong_text_slot_names_the_key`)
- [x] batch atomic (`::test_batch_with_bad_op_leaves_tree_identical`,
  `::test_batch_runtime_failure_leaves_tree_identical`)
- [x] pinned delete refused, patch allowed (`::test_pinned_delete_refused_patch_allowed`)
- [x] read-before-write in review (`::test_review_patch_needs_fresh_view`)
- [x] review delete only for curator-managed, archived (`::test_review_delete_*`)
- [x] lock outside the skill folder, concurrent patches (`::test_concurrent_patches_lose_nothing`)
- [x] symlink / traversal refused (`::test_path_escapes_refused`)
- [x] side effects never fail the call (`::test_side_effect_failures_do_not_fail_the_call`)
- Same: 20 operations per call at most (the reference's cap; the row names none).
- Deliberate differences: overlay instead of snapshot/rollback, so no "one destructive op per file"
  clobber rule is needed (ops apply in order on the overlay; a later write_file simply wins);
  delete allowed inside a batch (last op for its skill); exact matching; `write_file` never
  targets `SKILL.md` (use patch); new skills are not held to a 60-char description (P09-05's 1,024
  limit applies; P09-17 asks /learn for 60); no "other profiles" not-found hint; no prompt-preview
  of clipped descriptions (P09-07 clips with a visible mark); lock name uses the first 16 hex chars
  of the digest (phase row).
- Follow-up for P09-19 (slice F): the reference refuses a curator-fork delete without
  `absorbed_into`; here review-fork deletes only need curator ownership. The consolidation fork
  should add that rule (or bind a context the guard can read).
- Not taken: org-sharing notes, frontmatter-name fallback lookup (the P09-06 resolver is the one
  lookup).

## Status

Done (2026-10-19). Shared: `docs/internals/acceptance-map.md` (ACC-F16-1 pinned to tests).

## Integration (lead, P09 wave 2 landing)
Registered by `learning.host.register_tools` next to the view tools (D-16.147 layer rule) and listed in the `skills` toolset and the core list. `ARCHIVER` defaults to the curator's `archive.manage_archiver` (same archive layout, name stamping and usage state; a refusal rolls the call back; the tool writes the one ledger entry). The review fork, `/learn` and the curator's consolidation pass now use this tool (the slices' stand-ins are gone). D-16.149.
